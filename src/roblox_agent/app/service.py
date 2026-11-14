"""Everything the app's screens can ask for, in plain Python (no web code here).

The web server (server.py) turns page requests into calls on ``Service``. Tests
call ``Service`` directly. Rules this file keeps:

* The Roblox API key is never returned, logged, or put in an error message.
  A key typed in setup is kept in memory only until it has worked with the
  game, then saved to the same private file the command line uses.
* Cloud sharing is off unless turned on, and can't be turned on under 13.
* Every database call opens its own connection (pages call from many threads).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import __version__, catalog, checkup, config, connect, db, funnels, ingest
from ..api import AnalyticsClient, ApiError, AuthError
from ..demo import FakeRoblox
from . import discord_link

TERMS_VERSION = "2026-11-13"
AGES = ("under13", "13to17", "18plus")
GENRES = ("Action", "Adventure", "Education", "Entertainment", "Obby & Platformer", "Party & Casual",
          "Puzzle", "Roleplay & Avatar Sim", "Shooter", "Shopping", "Simulation", "Social",
          "Sports & Racing", "Strategy", "Survival", "Utility & Other")
DEFAULTS: dict[str, Any] = {
    "report_time": "09:00", "quiet_start": "22:00", "quiet_end": "08:00", "paused": False,
    "share_lessons": False, "discord": "unset",
}
STALE_HOURS = 20
DEMO_UNIVERSE = 1
# Only these pages can be opened from the app (in the normal browser).
OPEN_OK = ("https://create.roblox.com/", "https://www.roblox.com/", "https://claude.ai/",
           "https://claude.com/", "https://github.com/Boking234/Rolytics")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _local_now() -> datetime:
    return datetime.now()  # report times are in the user's own clock


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """True when ``now`` is inside the quiet window (which may wrap past midnight)."""
    t = now.strftime("%H:%M")
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


@dataclass
class Job:
    running: bool = False
    phase: str = ""          # what's happening, in words
    done_steps: int = 0
    total_steps: int = 0
    error: str = ""
    finished_at: float = 0.0


@dataclass
class Service:
    demo: bool = False
    transport: Any = None                       # tests and demo swap Roblox for a fake
    lookup: Callable[[int], dict] | None = None  # game name/icon lookup (public Roblox pages)
    notifier: Callable[[str, str], None] = lambda title, body: None
    opener: Callable[[str], Any] = webbrowser.open
    now: Callable[[], datetime] = _local_now
    job: Job = field(default_factory=Job)
    _pending_key: str | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _today_cache: dict | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    funnel_runner: Callable | None = None       # tests swap the agent run (subprocess.run) for a fake
    funnel_note: str = ""

    def __post_init__(self) -> None:
        if self.demo and self.transport is None:
            fake = FakeRoblox(incident=True)
            # Practice mode accepts any key text, so people can try the setup screens.
            self.transport = lambda m, u, h, b: fake(m, u, {**h, "x-api-key": "demo-key"}, b)
        if self.lookup is None:
            self.lookup = _demo_lookup if self.demo else public_game_info
        s = self.settings()
        if not s.get("install_id"):
            self._save("install_id", uuid.uuid4().hex)

    def __repr__(self) -> str:  # never show a key that is waiting to be tested
        return f"Service(demo={self.demo})"

    # ------------------------------------------------------------ settings ---
    def settings(self) -> dict:
        data = dict(DEFAULTS)
        data.update(config._read_config())
        return data

    def _save(self, name: str, value: Any) -> None:
        with self._lock:
            config.save_setting(name, value)

    def _cfg(self) -> config.Config:
        return config.load()

    def _conn(self):
        return db.connect(config.db_path())

    def _client(self, key: str, uid: int) -> AnalyticsClient:
        kw = {"transport": self.transport, "sleep": (lambda _s: None) if self.demo else time.sleep}
        return AnalyticsClient(api_key=key, universe_id=uid, **{k: v for k, v in kw.items() if v is not None})

    # --------------------------------------------------------------- state ---
    def state(self) -> dict:
        """What the page needs to decide which screen to show. Never includes the key."""
        s = self.settings()
        cfg = self._cfg()
        terms_ok = s.get("terms_version") == TERMS_VERSION and s.get("age") in AGES
        return {
            "version": __version__,
            "demo": self.demo,
            "setup_done": bool(s.get("setup_done")) and terms_ok and bool(cfg.api_key and cfg.universe_id),
            "terms_ok": terms_ok,
            "age": s.get("age"),
            "guardian_ok": bool(s.get("guardian_ok")),
            "share_lessons": bool(s.get("share_lessons")) and s.get("age") != "under13",
            "key_saved": bool(cfg.api_key),
            "key_pending": self._pending_key is not None,
            "game": self._game(s, cfg),
            "agent": s.get("agent"),
            "discord": discord_link.status(s),
            "discord_available": discord_link.AVAILABLE,
            "report_time": s["report_time"], "quiet_start": s["quiet_start"], "quiet_end": s["quiet_end"],
            "paused": bool(s.get("paused")),
            "start_with_windows": bool(s.get("start_with_windows")),
            "can_start_with_windows": startup_supported(),
            "unread": self.unread(),
            "job": self.job.__dict__.copy(),
            "genres": GENRES,
            "data_folder": str(config.home_dir()),
        }

    @staticmethod
    def _game(s: dict, cfg: config.Config) -> dict | None:
        if not cfg.universe_id:
            return None
        return {"id": cfg.universe_id, "name": s.get("game_name") or f"Game {cfg.universe_id}",
                "icon": s.get("game_icon") or "", "genre": s.get("genre") or "",
                "systems": s.get("systems") or ""}

    # --------------------------------------------------------- setup steps ---
    def accept_terms(self, agree: bool, age: str, guardian_ok: bool = False) -> dict:
        if age not in AGES:
            return _fail("Pick your age group first.")
        if age == "13to17" and not guardian_ok:
            return _fail("Ask a parent or guardian to read the terms with you, then tick the box.")
        if not agree:
            return _fail("You need to agree to the terms to use the app.")
        self._save("terms_version", TERMS_VERSION)
        self._save("terms_agreed_at", db.utcnow())
        self._save("age", age)
        self._save("guardian_ok", bool(guardian_ok) if age == "13to17" else False)
        if age == "under13":  # local-only mode
            self._save("share_lessons", False)
            self._save("discord", "blocked")
        elif self.settings().get("discord") == "blocked":
            self._save("discord", "unset")
        return _ok("Thanks.")

    def set_sharing(self, on: bool) -> dict:
        if on and self.settings().get("age") == "under13":
            return _fail("Sharing stays off for people under 13.")
        self._save("share_lessons", bool(on))
        self._save("share_lessons_changed_at", db.utcnow())
        return _ok("Sharing is on. Nothing is uploaded until the community feature launches."
                   if on else "Sharing is off. Nothing leaves this computer.")

    def check_key(self, key: str) -> dict:
        """Check the key looks right and hold it in memory until it works with the game."""
        key = (key or "").strip()
        if not key:
            return _fail("Paste the key first.")
        if any(c.isspace() for c in key):
            return _fail("The key has spaces or line breaks in it. Use Roblox's 'Copy Key to Clipboard' and paste again.")
        if len(key) < 20:
            return _fail("That's too short to be a Roblox API key. Use 'Copy Key to Clipboard' and paste again.")
        self._pending_key = key
        return _ok("That looks like a Roblox key. Next, pick your game and we'll test it with Roblox.")

    def keep_saved_key(self) -> dict:
        if not self._cfg().api_key:
            return _fail("No key is saved yet.")
        self._pending_key = None
        return _ok("Using the key already saved on this computer.")

    def connect_game(self, link: str) -> dict:
        """Test the key with this game. Saves both only if Roblox answers."""
        try:
            uid = config.parse_universe_id(link)
        except ValueError as e:
            return _fail(str(e))
        key = self._pending_key or self._cfg().api_key
        if not key:
            return _fail("Add your API key first (previous step).")
        if self.demo:
            uid = DEMO_UNIVERSE
        client = self._client(key, uid)
        end = ingest.today_utc()
        try:
            op = client.query_metric("DailyActiveUsers", "OneDay", ingest._iso(end - timedelta(days=7)),
                                     ingest._iso(end))
        except AuthError:
            return _fail("Roblox didn't accept the key for this game. Check that the key includes this game "
                         "and 'universe.analytics:read', and that its IP list has 0.0.0.0/0.")
        except ApiError as e:
            return _fail(config.redact(str(e), key))
        days = sum(len(s.get("dataPoints", [])) for s in op.get("response", {}).get("values", [])[:1])
        info = {}
        try:
            info = self.lookup(uid) or {}
        except Exception:  # the name and picture are nice to have, never required
            info = {}
        if key != self._cfg().api_key:
            config.save_api_key(key)
        self._pending_key = None
        config.save_universe_id(uid)
        self._save("game_name", info.get("name") or "")
        self._save("game_icon", info.get("icon") or "")
        genre = info.get("genre") if info.get("genre") in GENRES else self.settings().get("genre", "")
        self._save("genre", genre or "")
        self._today_cache = None
        return _ok(f"Connected. Roblox sent {days} days of player counts.",
                   game=self._game(self.settings(), self._cfg()))

    def save_details(self, genre: str = "", systems: str = "", report_time: str | None = None,
                     quiet_start: str | None = None, quiet_end: str | None = None) -> dict:
        if genre and genre not in GENRES:
            return _fail("Pick a genre from the list.")
        for t in (report_time, quiet_start, quiet_end):
            if t is not None and not _TIME.match(t):
                return _fail("Times look like 09:00.")
        self._save("genre", genre)
        self._save("systems", (systems or "").strip()[:200])
        for name, val in (("report_time", report_time), ("quiet_start", quiet_start), ("quiet_end", quiet_end)):
            if val is not None:
                self._save(name, val)
        return _ok("Saved.")

    def agents(self) -> list[dict]:
        found = connect.detect()
        out = [{"key": a.key, "label": a.label, "found": found.get(a.key, False), "manual": a.kind == "manual"}
               for a in connect.AGENTS]
        out.sort(key=lambda a: (not a["found"], a["manual"]))
        return out

    def connect_agent(self, key: str) -> dict:
        if key == "none":
            self._save("agent", None)
            return _ok("No AI agent for now. You still get the daily report and alerts here.")
        agent = connect.BY_KEY.get(key)
        if agent is None:
            return _fail("Pick an agent from the list.")
        if self.demo:
            ok, msg = True, f"(Practice mode: nothing was changed.) {agent.label} would be connected here."
        else:
            ok, msg = connect.connect(agent)
        if ok and agent.kind != "manual":
            self._save("agent", agent.key)
        return {"ok": ok, "message": msg, "manual": agent.kind == "manual"}

    def set_discord(self, choice: str) -> dict:
        if self.settings().get("age") == "under13":
            return _fail("Discord needs you to be 13 or older, so this stays off.")
        if choice not in ("remind", "skipped"):
            return _fail("Unknown choice.")
        self._save("discord", choice)
        return _ok("We'll remind you when Discord DMs are ready." if choice == "remind"
                   else "Skipped. You can turn it on later in Settings.")

    def finish_setup(self, start_with_windows: bool | None = None) -> dict:
        st = self.state()
        if not (st["terms_ok"] and st["key_saved"] and st["game"]):
            return _fail("A required step isn't finished yet.")
        self._save("setup_done", True)
        if start_with_windows is not None:
            self.set_startup(start_with_windows)
        self.run_now(kind="first")
        return _ok("Getting your first report.")

    # ------------------------------------------------------------- reports ---
    def run_now(self, kind: str = "manual") -> dict:
        """Start a pull + check-up in the background (one at a time)."""
        with self._lock:
            if self.job.running:
                return _ok("Already working on it.")
            self.job = Job(running=True, phase="Starting")
        threading.Thread(target=self._run_job, args=(kind,), daemon=True).start()
        return _ok("Started.")

    def _run_job(self, kind: str) -> None:
        try:
            cfg = self._cfg()
            if not (cfg.api_key and cfg.universe_id):
                raise ApiError("Setup isn't finished.")
            conn = self._conn()
            try:
                if self._stale(conn, cfg.universe_id):
                    self._pull(conn, cfg)
                self.job.phase = "Checking the numbers"
                result = checkup.run(conn, cfg.universe_id, record=True, weekly=None)
                self._today_cache = self._to_today(result)
                self._add_report(conn, cfg.universe_id, result, kind)
            finally:
                conn.close()
            self._funnels_if_changed()
            self.job.phase = "Done"
        except ApiError as e:
            self.job.error = config.redact(str(e), self._cfg().api_key)
            self.job.phase = "Stopped"
        except Exception as e:  # keep the app alive; show something useful
            self.job.error = config.redact(f"{type(e).__name__}: {e}", self._cfg().api_key)
            self.job.phase = "Stopped"
        finally:
            self.job.running = False
            self.job.finished_at = time.time()

    @staticmethod
    def _stale(conn, uid: int) -> bool:
        row = conn.execute("SELECT MAX(started_at) AS t FROM pull_runs WHERE universe_id=? AND status!='failed'",
                           (uid,)).fetchone()
        if not row or not row["t"]:
            return True
        last = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last > timedelta(hours=STALE_HOURS)

    def _pull(self, conn, cfg: config.Config) -> None:
        specs = list(catalog.DEFAULT_METRICS)
        self.job.total_steps = sum(len(s.breakdowns) for s in specs)
        first = conn.execute("SELECT COUNT(*) FROM data_points WHERE universe_id=?",
                             (cfg.universe_id,)).fetchone()[0] == 0
        self.job.phase = ("Downloading your game's history (first time only, a few minutes)" if first
                          else "Getting the latest numbers from Roblox")

        def progress(msg: str) -> None:
            if not msg.startswith("  "):
                self.job.done_steps += 1

        days = 120 if self.demo else ingest.DEFAULT_BACKFILL_DAYS
        result = ingest.pull(self._client(cfg.api_key, cfg.universe_id), conn, specs, days, progress=progress)
        if result.status == "failed":
            raise ApiError("Roblox didn't send any data this time. I'll try again later.")

    def _add_report(self, conn, uid: int, result: checkup.Checkup, kind: str) -> None:
        n_issues = sum(1 for f in result.top if not f.good)
        n_good = len(result.top) - n_issues
        if result.status == "No data":
            title = "No data yet"
        elif not result.top:
            title = f"{result.status}: no unusual changes"
        else:
            bits = []
            if n_issues:
                bits.append(f"{n_issues} to look at")
            if n_good:
                bits.append(f"{n_good} good")
            title = f"{result.status}: " + ", ".join(bits)
        body = "\n".join(f"{i}. {f.line()}" for i, f in enumerate(result.top, 1)) or (
            f"No unusual changes across {result.checked} metrics." if result.checked else "No data yet.")
        if result.notes:
            body += "\n" + "\n".join(result.notes)
        if result.extra:
            body += "\n\n" + result.extra
        cur = conn.execute("INSERT INTO inbox (universe_id, kind, status, title, body, data_day, created_at)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (uid, "daily", result.status, title, body, result.day, db.utcnow()))
        conn.commit()
        item = {"id": cur.lastrowid, "kind": "daily", "status": result.status, "title": title, "body": body}
        self._save("last_report_day", self.now().date().isoformat())
        s = self.settings()
        discord_link.mirror(item, s)
        if in_quiet_hours(self.now(), s["quiet_start"], s["quiet_end"]):
            self._save("pending_notice", item["id"])  # shown when quiet hours end
        else:
            self._notify(item)

    def _notify(self, item: dict) -> None:
        top = item["body"].split("\n", 1)[0]
        try:
            self.notifier("Roblox check-up: " + item["title"], re.sub(r"^\d+\. (ISSUE|GOOD): ", "", top)[:200])
        except Exception:
            pass

    def today(self) -> dict:
        cfg = self._cfg()
        if not cfg.universe_id:
            return {"status": "No data", "items": []}
        if self._today_cache is None:
            conn = self._conn()
            try:
                has = conn.execute("SELECT 1 FROM checkups WHERE universe_id=? LIMIT 1",
                                   (cfg.universe_id,)).fetchone()
                if has:  # numbers must match the stored list, so "Fixed" marks the right item
                    result = checkup.run(conn, cfg.universe_id, record=True, weekly=False)
                    self._today_cache = self._to_today(result)
                else:
                    return {"status": "No data", "items": [], "waiting": True}
            finally:
                conn.close()
        return self._today_cache

    def _to_today(self, result: checkup.Checkup) -> dict:
        items = [{"n": i, "key": f.key, "good": f.good, "severity": f.severity, "title": f.short(),
                  "detail": f.headline(), "marked": None, "fix": f.fix,
                  "cause": f.cause.text.removeprefix("Likely cause: ") if f.cause else ""}
                 for i, f in enumerate(result.top, 1)]
        return {"status": result.status, "day": result.day, "items": items, "checked": result.checked,
                "notes": result.notes, "weekly": result.extra}

    def mark(self, key: str, action: str) -> dict:
        """Mark an item by its key (stable even if the list was re-run elsewhere, e.g. by the agent)."""
        uid = self._cfg().universe_id
        conn = self._conn()
        try:
            n = self._position(conn, uid, key)
            if n is None:  # the stored list moved on: refresh it once and look again
                checkup.run(conn, uid, record=True, weekly=False)
                n = self._position(conn, uid, key)
            if n is None:
                return _fail("That item isn't on today's list any more. Refresh to see the latest.")
            msg = checkup.mark(conn, uid, n, action)
        finally:
            conn.close()
        ok = not msg.startswith(("Unknown", "Pick", "No check-up", "The latest"))
        if ok and self._today_cache:
            for it in self._today_cache["items"]:
                if it["key"] == key:
                    it["marked"] = None if action == "open" else action
        return {"ok": ok, "message": msg}

    @staticmethod
    def _position(conn, uid: int, key: str) -> int | None:
        row = conn.execute("SELECT top_keys FROM checkups WHERE universe_id=? ORDER BY data_day DESC LIMIT 1",
                           (uid,)).fetchone()
        keys = json.loads(row["top_keys"]) if row else []
        return keys.index(key) + 1 if key in keys else None

    # --------------------------------------------------------------- inbox ---
    def inbox(self, limit: int = 60) -> list[dict]:
        uid = self._cfg().universe_id
        if not uid:
            return []
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM inbox WHERE universe_id=? ORDER BY id DESC LIMIT ?",
                                (uid, limit)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def unread(self) -> int:
        uid = self._cfg().universe_id
        if not uid or not config.db_path().exists():
            return 0
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM inbox WHERE universe_id=? AND read=0", (uid,)).fetchone()[0]
        finally:
            conn.close()

    def mark_read(self, item_id: int | None = None) -> dict:
        uid = self._cfg().universe_id
        conn = self._conn()
        try:
            if item_id is None:
                conn.execute("UPDATE inbox SET read=1 WHERE universe_id=?", (uid,))
            else:
                conn.execute("UPDATE inbox SET read=1 WHERE universe_id=? AND id=?", (uid, int(item_id)))
            conn.commit()
        finally:
            conn.close()
        return _ok("")

    # ------------------------------------------------------------ settings ---
    def update_settings(self, **changes: Any) -> dict:
        if "paused" in changes:
            self._save("paused", bool(changes.pop("paused")))
        if "share_lessons" in changes:
            r = self.set_sharing(bool(changes.pop("share_lessons")))
            if not r["ok"]:
                return r
        if "start_with_windows" in changes:
            self.set_startup(bool(changes.pop("start_with_windows")))
        if "discord" in changes:
            r = self.set_discord(changes.pop("discord"))
            if not r["ok"]:
                return r
        times = {k: changes.pop(k) for k in ("report_time", "quiet_start", "quiet_end") if k in changes}
        for t in times.values():
            if not _TIME.match(str(t)):
                return _fail("Times look like 09:00.")
        for k, v in times.items():
            self._save(k, v)
        if "genre" in changes or "systems" in changes:
            s = self.settings()
            r = self.save_details(changes.pop("genre", s.get("genre", "")), changes.pop("systems", s.get("systems", "")))
            if not r["ok"]:
                return r
        if changes:
            return _fail("Unknown setting: " + ", ".join(changes))
        return _ok("Saved.")

    def forget_key(self) -> dict:
        self._pending_key = None
        config.forget_api_key()
        self._save("setup_done", False)
        return _ok("The key was deleted from this computer. Also delete it on Roblox's API Keys page.")

    def set_startup(self, on: bool) -> None:
        self._save("start_with_windows", bool(on))
        set_windows_startup(bool(on))

    # ------------------------------------------------------------- funnels ---
    def _funnels_if_changed(self, force: bool = False) -> None:
        """Refresh the game context file; if it changed, ask the agent for missing funnels."""
        try:
            self.job.phase = "Checking your game's funnels"
            run = self.funnel_runner or funnels.subprocess.run
            if self.demo:  # practice mode never runs a real agent or touches the internet
                funnels.refresh_context(self.settings(), offline=True)
                self.funnel_note = "Practice mode: your AI agent would plan funnels here."
                return
            self.funnel_note = funnels.plan(self.settings(), force=force, run=run,
                                            get=lambda url: _get_json(url)).split("\n", 1)[0]
        except Exception as e:  # funnels are extra: never break the daily report
            self.funnel_note = f"Couldn't plan funnels: {type(e).__name__}"

    def funnels(self) -> dict:
        items = funnels.load()
        return {"items": items, "note": self.funnel_note,
                "agent_can_run": funnels.agent_command(self.settings().get("agent")) is not None,
                "prompt": funnels.manual_prompt(), "luau": str(funnels.luau_path()),
                "context": str(funnels.context_path())}

    def plan_funnels(self) -> dict:
        """'Plan again' in Settings: runs in the background like a report."""
        with self._lock:
            if self.job.running:
                return _ok("Busy with a report; try again in a minute.")
            self.job = Job(running=True, phase="Asking your AI agent for funnels")

        def work() -> None:
            try:
                self._funnels_if_changed(force=True)
            finally:
                self.job.running = False
                self.job.phase = "Done"
                self.job.finished_at = time.time()
        threading.Thread(target=work, daemon=True).start()
        return _ok("Asking your AI agent. This takes a minute or two.")

    def learned(self) -> dict:
        """What the checks have learned about this game (clicks, changes, fixes), as plain text."""
        uid = self._cfg().universe_id
        if not uid:
            return {"text": ""}
        conn = self._conn()
        try:
            from .. import learn
            return {"text": learn.report(conn, uid)}
        finally:
            conn.close()

    def export_settings(self) -> dict:
        """Settings to move to a new PC. No key, no data, no install ID."""
        s = self.settings()
        keep = ("universe_id", "game_name", "genre", "systems", "code_folder", "report_time", "quiet_start", "quiet_end",
                "agent", "share_lessons", "discord")
        return {"roblox_analytics_agent_settings": 1, **{k: s[k] for k in keep if k in s}}

    def open_url(self, url: str) -> dict:
        if not any(url.startswith(p) for p in OPEN_OK):
            return _fail("That link isn't allowed.")
        self.opener(url)
        return _ok("Opened in your browser.")

    # ----------------------------------------------------------- scheduler ---
    def tick(self) -> None:
        """Called every minute: run the daily report when it's due, deliver held notices."""
        s = self.settings()
        now = self.now()
        if s.get("pending_notice") and not in_quiet_hours(now, s["quiet_start"], s["quiet_end"]):
            item_id = s.get("pending_notice")
            self._save("pending_notice", None)
            for it in self.inbox(10):
                if it["id"] == item_id and not it["read"]:
                    self._notify(it)
        if not self.state()["setup_done"] or s.get("paused") or self.job.running:
            return
        if s.get("last_report_day") == now.date().isoformat():
            return
        if now.strftime("%H:%M") >= s["report_time"]:
            self.run_now(kind="daily")

    def start_scheduler(self, every: float = 60.0) -> threading.Thread:
        def loop() -> None:
            while not self._stop.wait(every):
                try:
                    self.tick()
                except Exception:
                    pass

        t = threading.Thread(target=loop, daemon=True, name="scheduler")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------ helpers ---

def _ok(message: str, **extra: Any) -> dict:
    return {"ok": True, "message": message, **extra}


def _fail(message: str) -> dict:
    return {"ok": False, "message": message}


def _get_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "roblox-analytics-agent (local app)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def public_game_info(uid: int, get: Callable[[str], dict] = _get_json) -> dict:
    """Name, genre and icon from Roblox's public game pages (no key needed). Best effort."""
    info: dict[str, str] = {}
    try:
        data = get(f"https://games.roblox.com/v1/games?universeIds={int(uid)}").get("data") or []
        if data:
            info["name"] = str(data[0].get("name") or "")[:80]
            info["genre"] = str(data[0].get("genre_l1") or data[0].get("genre") or "")
    except Exception:
        pass
    try:
        data = get(f"https://thumbnails.roblox.com/v1/games/icons?universeIds={int(uid)}"
                   "&returnPolicy=PlaceHolder&size=150x150&format=Png&isCircular=false").get("data") or []
        url = str(data[0].get("imageUrl") or "") if data else ""
        if url.startswith("https://") and ".rbxcdn.com/" in url:
            info["icon"] = url
    except Exception:
        pass
    return info


def _demo_lookup(uid: int) -> dict:
    return {"name": "Practice Obby (made-up numbers)", "genre": "Obby & Platformer", "icon": ""}


def startup_supported() -> bool:
    import sys
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def set_windows_startup(on: bool) -> bool:
    """Start the app (in the tray, no window) when you sign in to Windows."""
    if not startup_supported():
        return False
    import sys
    import winreg  # type: ignore[import-not-found]
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "RobloxAnalyticsAgent", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
            else:
                try:
                    winreg.DeleteValue(k, "RobloxAnalyticsAgent")
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def today_local() -> date:
    return _local_now().date()
