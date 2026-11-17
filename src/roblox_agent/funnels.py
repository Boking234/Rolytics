"""Game context file + funnels the connected AI agent plans for your game.

The action, in order:

1. ``refresh_context`` works out what the game is (name, genre and description
   from Roblox's public pages, the systems you typed in setup, and, if you
   point it at your game's code folder, which systems the scripts mention and
   which funnels are already logged there). It saves this to
   ``~/.roblox-agent/game_context.json``.
2. ``ask_agent`` runs the agent you connected (Claude Code, Codex, Gemini CLI)
   once, without a window, with a short prompt. The agent reads the context
   through the ``game_context`` MCP tool and sends back funnel ideas with
   ``add_funnels``. Agents that can't run without a window (Claude Desktop,
   Cursor, VS Code, Windsurf) get the same job through the ``plan-funnels``
   prompt instead.
3. ``add`` keeps only funnels that don't exist yet (same name, or nearly the
   same steps, as one already planned or already in your code), saves them to
   ``~/.roblox-agent/funnels.json`` and writes a ready-to-paste Luau module,
   ``~/.roblox-agent/funnels/FunnelTracker.lua``, that logs them through
   Roblox's own AnalyticsService funnel events. Results then show in Creator
   Hub > Analytics > Funnels.

Token use: the context the agent reads is capped at ~1,200 characters
(~300 tokens) and the planning run only happens when the context changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import config

CONTEXT_FILE = "game_context.json"
FUNNELS_FILE = "funnels.json"
LUAU_FILE = "FunnelTracker.lua"
MAX_DESCRIPTION = 300
MAX_CONTEXT_CHARS = 1200
MAX_FUNNELS = 8           # in the file; Roblox charts get hard to read past this
MAX_NEW_PER_RUN = 4
MIN_STEPS, MAX_STEPS = 2, 10
SAME_STEPS = 0.8          # share of steps in common that makes two funnels "the same"
AGENT_TIMEOUT = 300
SCAN_LIMIT = 2000         # files; keeps a scan of a huge repo quick
SCAN_BYTES = 400_000      # per file

# Words in a game's scripts that tell us which systems it has. Cheap and local.
SYSTEM_WORDS: dict[str, tuple[str, ...]] = {
    "tutorial": ("tutorial", "onboarding", "firsttime", "newplayer"),
    "shop": ("shop", "store", "marketplaceservice", "promptproductpurchase", "devproduct", "gamepass"),
    "quests": ("quest", "mission", "objective", "task"),
    "daily rewards": ("dailyreward", "daily_reward", "streak", "login reward", "loginreward"),
    "rebirth": ("rebirth", "prestige", "ascend"),
    "pets/eggs": ("pet", "hatch", "egg"),
    "levels/stages": ("checkpoint", "stage", "levelup", "level_up"),
    "trading": ("trade", "trading"),
    "battle pass": ("battlepass", "battle_pass", "season pass"),
    "rounds/matches": ("round", "intermission", "lobby", "matchmaking"),
    "crafting": ("craft", "recipe"),
    "inventory": ("inventory", "backpack"),
}

_FUNNEL_CALL = re.compile(
    r"Log(Onboarding)?FunnelStepEvent\s*\(\s*[^,()]+(?:,\s*[\"']([^\"']{1,60})[\"'])?")
_OUR_CALL = re.compile(r"FunnelTracker\.step\s*\(\s*[^,()]+,\s*[\"']([^\"']{1,60})[\"']")


# ------------------------------------------------------------------ files ---
def context_path() -> Path:
    return config.home_dir() / CONTEXT_FILE


def funnels_path() -> Path:
    return config.home_dir() / FUNNELS_FILE


def luau_path() -> Path:
    return config.home_dir() / "funnels" / LUAU_FILE


def _read_json(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- context ---
def _default_get(url: str) -> dict:
    from .app.service import _get_json  # same polite, timed request the app uses
    return _get_json(url)


def public_details(uid: int, get: Callable[[str], dict] | None = None) -> dict:
    """Name, genre, description and max players from Roblox's public page. Best effort, no key."""
    get = get or _default_get
    try:
        data = (get(f"https://games.roblox.com/v1/games?universeIds={int(uid)}").get("data") or [])
    except Exception:  # noqa: BLE001 - best effort, like the app
        return {}
    if not data:
        return {}
    d = data[0]
    desc = re.sub(r"\s+", " ", str(d.get("description") or "")).strip()
    return {"name": str(d.get("name") or "")[:80],
            "genre": str(d.get("genre_l1") or d.get("genre") or ""),
            "subgenre": str(d.get("genre_l2") or ""),
            "description": desc[:MAX_DESCRIPTION],
            "max_players": d.get("maxPlayers")}


def scan_code(folder: str | Path) -> dict:
    """Which systems the game's scripts mention, and which funnels they already log."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        return {}
    hits: dict[str, int] = {}
    found: dict[str, str] = {}   # funnel name -> where
    onboarding = False
    n = 0
    for p in root.rglob("*"):
        if p.suffix.lower() not in (".lua", ".luau") or any(part in (".git", "node_modules", "Packages")
                                                             for part in p.parts):
            continue
        n += 1
        if n > SCAN_LIMIT:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:SCAN_BYTES]
        except OSError:
            continue
        if text.startswith("-- FunnelTracker: made by roblox-agent"):
            continue  # our own module: its calls are templates, not funnels
        low = text.lower()
        for system, words in SYSTEM_WORDS.items():
            if any(w in low for w in words):
                hits[system] = hits.get(system, 0) + 1
        rel = str(p.relative_to(root))
        for m in _FUNNEL_CALL.finditer(text):
            if m.group(1):
                onboarding = True
                found.setdefault("Onboarding", rel)
            elif m.group(2):
                found.setdefault(m.group(2), rel)
        for m in _OUR_CALL.finditer(text):
            found.setdefault(m.group(1), rel)
    systems = [s for s, c in sorted(hits.items(), key=lambda kv: -kv[1])][:8]
    return {"scripts": min(n, SCAN_LIMIT), "systems_in_code": systems,
            "funnels_in_code": found, "has_onboarding": onboarding}


def refresh_context(settings: dict | None = None, uid: int | None = None,
                    get: Callable[[str], dict] | None = None, offline: bool = False) -> tuple[dict, bool]:
    """Work out the game's context and save it. Returns (context, changed_since_last_time)."""
    s = settings if settings is not None else config._read_config()
    uid = uid or (int(s["universe_id"]) if str(s.get("universe_id", "")).isdigit() else None)
    old = _read_json(context_path())
    pub = {} if offline or not uid else public_details(uid, get)
    ctx = {
        "universe_id": uid,
        "name": pub.get("name") or s.get("game_name") or old.get("name") or "",
        "genre": s.get("genre") or pub.get("genre") or old.get("genre") or "",
        "subgenre": pub.get("subgenre") or old.get("subgenre") or "",
        "description": pub.get("description") or old.get("description") or "",
        "max_players": pub.get("max_players") or old.get("max_players"),
        "systems": (s.get("systems") or "").strip()[:200],
    }
    folder = s.get("code_folder")
    code = scan_code(folder) if folder else {}
    if code:
        ctx["code_folder"] = str(folder)
        ctx.update(code)
    ctx["fingerprint"] = _fingerprint(ctx)
    changed = ctx["fingerprint"] != old.get("fingerprint")
    ctx["updated"] = _now() if changed or not old.get("updated") else old["updated"]
    _write_json(context_path(), ctx)
    if code.get("funnels_in_code"):
        _mark_in_game(code["funnels_in_code"])
    return ctx, changed


def _fingerprint(ctx: dict) -> str:
    keep = {k: ctx.get(k) for k in ("name", "genre", "subgenre", "description", "systems",
                                    "systems_in_code", "funnels_in_code")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def load_context() -> dict:
    return _read_json(context_path())


def context_text(ctx: dict | None = None) -> str:
    """The short text the agent reads (capped, ~300 tokens)."""
    ctx = ctx if ctx is not None else load_context()
    if not ctx:
        return "No game context yet. Ask the user to run 'roblox-agent funnels'."
    lines = [f"Game: {ctx.get('name') or 'unnamed'} | genre: {ctx.get('genre') or '?'}"
             + (f" / {ctx['subgenre']}" if ctx.get("subgenre") else "")
             + (f" | max players {ctx['max_players']}" if ctx.get("max_players") else "")]
    if ctx.get("description"):
        lines.append(f"Description: {ctx['description']}")
    if ctx.get("systems"):
        lines.append(f"Systems (from developer): {ctx['systems']}")
    if ctx.get("systems_in_code"):
        lines.append(f"Systems seen in code: {', '.join(ctx['systems_in_code'])}")
    have = existing_names()
    if have:
        lines.append(f"Funnels already there (don't repeat): {', '.join(have)}")
    else:
        lines.append("Funnels already there: none")
    text = "\n".join(lines)
    return text[:MAX_CONTEXT_CHARS]


# ---------------------------------------------------------------- funnels ---
def load() -> list[dict]:
    data = _read_json(funnels_path())
    items = data.get("funnels")
    return items if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    _write_json(funnels_path(), {"funnels": items})
    write_luau(items)


def existing_names() -> list[str]:
    return [f["name"] for f in load() if f.get("name")]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _clean_name(text: str, limit: int = 40) -> str:
    """Funnel and step names Roblox will chart nicely: letters, digits, spaces."""
    text = re.sub(r"[^A-Za-z0-9 _-]", "", str(text or "")).strip()
    return re.sub(r"\s+", " ", text)[:limit]


def _same_steps(a: list[str], b: list[str]) -> bool:
    ka, kb = {_key(x) for x in a}, {_key(x) for x in b}
    if not ka or not kb:
        return False
    return len(ka & kb) / max(len(ka), len(kb)) >= SAME_STEPS


def _mark_in_game(found: dict[str, str]) -> None:
    items = load()
    keys = {_key(n): where for n, where in found.items()}
    changed = False
    for f in items:
        where = keys.pop(_key(f.get("name", "")), None)
        if where and f.get("status") != "in_game":
            f["status"], f["where"] = "in_game", where
            changed = True
    for k, where in keys.items():  # funnels the developer already logs that we didn't plan
        name = next(n for n in found if _key(n) == k)
        items.append({"name": name, "kind": "onboarding" if name == "Onboarding" else "recurring",
                      "steps": [], "why": "Already logged in your code.", "status": "in_game",
                      "where": where, "source": "code", "added": _now()})
        changed = True
    if changed:
        _save(items)


def add(proposed: list[dict], source: str = "agent") -> dict:
    """Create the proposed funnels that aren't there yet. Returns what happened."""
    items = load()
    created, skipped, rejected = [], [], []
    for raw in proposed or []:
        if not isinstance(raw, dict):
            rejected.append("(not a funnel)")
            continue
        name = _clean_name(raw.get("name", ""))
        steps = [s for s in (_clean_name(x, 30) for x in (raw.get("steps") or [])) if s]
        kind = "onboarding" if str(raw.get("kind", "")).lower() == "onboarding" else "recurring"
        if kind == "onboarding":
            name = "Onboarding"
        if not name or not (MIN_STEPS <= len(steps) <= MAX_STEPS):
            rejected.append(f"{name or '(no name)'}: needs a name and {MIN_STEPS}-{MAX_STEPS} steps")
            continue
        twin = next((f for f in items if _key(f["name"]) == _key(name)
                     or (f.get("steps") and _same_steps(f["steps"], steps))), None)
        if twin:
            skipped.append(f"{name} (same as {twin['name']})" if twin["name"] != name else name)
            continue
        if len(created) >= MAX_NEW_PER_RUN or len(items) >= MAX_FUNNELS:
            rejected.append(f"{name}: limit of {MAX_FUNNELS} funnels reached")
            continue
        f = {"name": name, "kind": kind, "steps": steps,
             "why": str(raw.get("why") or "")[:160], "status": "planned",
             "source": source, "added": _now()}
        hints = raw.get("trigger_hints")
        if isinstance(hints, dict):  # agents often send {step: where}
            f["where"] = {_clean_name(k, 30): str(v)[:120] for k, v in list(hints.items())[:MAX_STEPS]}
        elif isinstance(hints, list):
            f["where"] = [str(v)[:120] for v in hints[:MAX_STEPS]]
        elif hints:
            f["where"] = str(hints)[:400]
        items.append(f)
        created.append(name)
    if created:
        _save(items)
    return {"created": created, "skipped": skipped, "rejected": rejected}


def add_text(result: dict) -> str:
    out = []
    if result["created"]:
        out.append(f"Created: {', '.join(result['created'])}.")
        out.append(f"Luau module: {luau_path()} (server script; call FunnelTracker.step(player, funnel, step)).")
    if result["skipped"]:
        out.append(f"Already there: {', '.join(result['skipped'])}.")
    if result["rejected"]:
        out.append(f"Not added: {'; '.join(result['rejected'])}.")
    return " ".join(out) or "Nothing to add."


def summary() -> str:
    items = load()
    if not items:
        return "No funnels yet."
    lines = []
    for f in items:
        tag = "in your game" if f.get("status") == "in_game" else "planned, add to game"
        steps = " > ".join(f.get("steps") or []) or "(steps in your code)"
        lines.append(f"{f['name']} [{tag}]: {steps}")
    return "\n".join(lines)


# ------------------------------------------------------------------- Luau ---
def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_luau(items: list[dict] | None = None) -> Path:
    """One ModuleScript the developer drops into ServerScriptService."""
    items = load() if items is None else items
    rows = []
    for f in items:
        if not f.get("steps"):
            continue
        steps = ", ".join(_lua_str(s) for s in f["steps"])
        rows.append(f"\t[{_lua_str(f['name'])}] = {{ kind = {_lua_str(f['kind'])}, steps = {{ {steps} }} }},")
    body = "\n".join(rows) or "\t-- no funnels yet"
    text = f"""-- FunnelTracker: made by roblox-agent ({_now()[:10]}). Server-side ModuleScript.
-- Put it in ServerScriptService, then call FunnelTracker.step(player, "<Funnel>", "<Step>")
-- at each step. Results: Creator Hub > Analytics > Funnels (needs enough players per day).
local AnalyticsService = game:GetService("AnalyticsService")
local HttpService = game:GetService("HttpService")

local FUNNELS = {{
{body}
}}

local FunnelTracker = {{}}
local sessions = {{}} -- [player][funnel] = funnel session id (recurring funnels)

local function stepIndex(funnel, stepName)
	for i, s in ipairs(funnel.steps) do
		if s == stepName then return i end
	end
	return nil
end

-- Log that `player` reached `stepName` of `funnelName`. Unknown names are ignored.
function FunnelTracker.step(player, funnelName, stepName)
	local funnel = FUNNELS[funnelName]
	local index = funnel and stepIndex(funnel, stepName)
	if not index then
		warn("FunnelTracker: unknown funnel/step", funnelName, stepName)
		return
	end
	if funnel.kind == "onboarding" then
		AnalyticsService:LogOnboardingFunnelStepEvent(player, index, stepName)
		return
	end
	sessions[player] = sessions[player] or {{}}
	if index == 1 or not sessions[player][funnelName] then
		sessions[player][funnelName] = HttpService:GenerateGUID(false)
	end
	AnalyticsService:LogFunnelStepEvent(player, funnelName, sessions[player][funnelName], index, stepName)
end

game:GetService("Players").PlayerRemoving:Connect(function(player)
	sessions[player] = nil
end)

return FunnelTracker
"""
    p = luau_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------ the agent ---
PLAN_PROMPT = (
    "Use the roblox-agent MCP tools. Call game_context once. From it, propose up to 3 player funnels "
    "that would show where players drop off in THIS game (e.g. an Onboarding funnel if there is none, "
    "then the main loop and the shop or progression). Each funnel: name, kind (onboarding or recurring), "
    "3-7 short step names in order, a one-line why, and trigger_hints saying where in the game each step "
    "happens. Skip any funnel listed as already there. Then call add_funnels once with the list and "
    "reply with its answer only.")
EDIT_PROMPT = (" This folder has the game's Luau code: also copy the FunnelTracker module the tool wrote "
               "into ServerScriptService and add FunnelTracker.step calls at each step's place in the "
               "server scripts. Change nothing else.")
TOOLS_ALLOWED = "mcp__roblox-agent__game_context mcp__roblox-agent__add_funnels"


def agent_command(agent: str | None, edit_code: bool = False,
                  which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """How to run the connected agent once without a window, or None if it can't."""
    prompt = PLAN_PROMPT + (EDIT_PROMPT if edit_code else "")
    if agent == "claude-code" and which("claude"):
        tools = TOOLS_ALLOWED + (" Read Edit Write Glob Grep" if edit_code else "")
        return ["claude", "-p", prompt, "--allowedTools", tools]
    if agent == "codex" and which("codex"):
        return ["codex", "exec", prompt] if edit_code else ["codex", "exec", "--sandbox", "read-only", prompt]
    if agent == "gemini" and which("gemini"):
        return ["gemini", "-p", prompt]
    return None


def manual_prompt() -> str:
    return ("In your AI agent, use the 'plan-funnels' prompt from roblox-agent (or paste: "
            f"\"{PLAN_PROMPT}\").")


def ask_agent(agent: str | None, edit_code: bool = False, cwd: str | None = None,
              run: Callable = subprocess.run, which: Callable[[str], str | None] = shutil.which) -> tuple[bool, str]:
    """Run the agent once to plan funnels. Returns (ran_ok, message)."""
    cmd = agent_command(agent, edit_code, which)
    if cmd is None:
        return False, manual_prompt()
    before = set(existing_names())
    try:
        r = run(cmd, capture_output=True, text=True, timeout=AGENT_TIMEOUT,
                cwd=cwd if cwd and Path(cwd).is_dir() else None)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"The agent didn't finish ({type(e).__name__}). {manual_prompt()}"
    new = [n for n in existing_names() if n not in before]
    if new:
        return True, f"Created: {', '.join(new)}."
    tail = (r.stdout or r.stderr or "").strip()[-300:]
    if r.returncode != 0:
        return False, f"The agent stopped with an error: {tail or r.returncode}"
    return True, tail or "No new funnels: the ones your game needs are already there."


def plan(settings: dict | None = None, force: bool = False, edit_code: bool = False,
         run: Callable = subprocess.run, which: Callable[[str], str | None] = shutil.which,
         get: Callable[[str], dict] | None = None, offline: bool = False) -> str:
    """The whole action: refresh the context file, then ask the agent if anything changed."""
    s = settings if settings is not None else config._read_config()
    _ctx, changed = refresh_context(s, get=get, offline=offline)
    if not (changed or force or not load()):
        return "Game context unchanged; funnels are up to date.\n" + summary()
    _ok, msg = ask_agent(s.get("agent"), edit_code=edit_code, cwd=s.get("code_folder"), run=run, which=which)
    return msg + "\n" + summary()
