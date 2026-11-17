"""A tiny MCP server so AI agents (Claude Code, Claude Desktop, Codex, Cursor,
VS Code, Gemini CLI...) can read your game's check-up.

Speaks the Model Context Protocol over stdio (one JSON message per line),
using only the standard library. Start it with ``roblox-agent mcp``; agents
start it for you once connected with ``roblox-agent connect``.

Token budget: the tool list is ~600 tokens and a check-up answer is ~100-350
tokens (up to ~600 on the first check-up of a week, which adds the weekly summary). The agent never sees raw data unless it asks for a capped history.
The API key is read from your local settings and is never sent to the agent.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, BinaryIO

from . import __version__, catalog, changes, checkup, config, db, funnels, ingest, why
from .api import AnalyticsClient, ApiError

_funnels = funnels  # add_funnels takes an argument called 'funnels'

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Newer stateless protocol: no initialize; each request names its version in _meta.
STATELESS_VERSIONS = ("2026-07-28",)
META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
MAX_HISTORY_DAYS = 90
STALE_HOURS = 20

TOOLS = [
    {
        "name": "daily_checkup",
        "description": "Health of the user's Roblox game: status, top 3 changes with size and confidence; "
                       "weekly summary on a week's first check-up. Short text.",
        "inputSchema": {"type": "object", "properties": {
            "refresh": {"type": "boolean", "description": "Pull data if stale (default true)"},
            "weekly": {"type": "boolean", "description": "Force the weekly summary"}}},
    },
    {
        "name": "mark_item",
        "description": "User's verdict on check-up item N: done (fixed), snooze (7 days), wrong (false alarm), open.",
        "inputSchema": {"type": "object", "properties": {
            "item": {"type": "integer"},
            "action": {"type": "string", "enum": ["done", "snooze", "wrong", "open"]}},
            "required": ["item", "action"]},
    },
    {
        "name": "log_change",
        "description": "Log a shipped game change to judge later. Call without scores to get the rubric.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"},
            "scores": {"type": "object", "description": "0-3 each: reach systems spread economy paid progress undo"},
            "shipped_on": {"type": "string", "description": "YYYY-MM-DD"},
            "target": {"type": "string", "description": "Metric it should move"}},
            "required": ["title"]},
    },
    {
        "name": "player_trend",
        "description": "Why player numbers rise or fall: new vs returning players, clues, device, lifecycle.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "metric_history",
        "description": "Daily values of one metric, max 90 days, as 'date value' lines.",
        "inputSchema": {"type": "object", "properties": {
            "metric": {"type": "string"},
            "days": {"type": "integer", "description": "1-90, default 14"},
            "split": {"type": "string", "description": "e.g. Platform=Phone"}},
            "required": ["metric"]},
    },
    {
        "name": "game_context",
        "description": "The game's genre, description, systems and existing player funnels.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_funnels",
        "description": "Create missing drop-off funnels. Item: {name, kind: onboarding|recurring, steps: [...], "
                       "why, trigger_hints}.",
        "inputSchema": {"type": "object", "properties": {"funnels": {"type": "array", "items": {"type": "object"}}},
                        "required": ["funnels"]},
    },
    {
        "name": "list_metrics",
        "description": "Stored metric names and splits.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

PROMPTS = [{
    "name": "daily-checkup",
    "description": "Explain today's Roblox game check-up in plain language",
}, {
    "name": "plan-funnels",
    "description": "Plan player drop-off funnels for this game and create the missing ones",
}]
PROMPT_TEXT = ("Call the daily_checkup tool once. Explain the result to a game developer in plain language, "
               "under 120 words: what changed, the likely cause (use its 'Likely cause' when given), and one "
               "next step for each ISSUE, starting from its 'Try' line but made specific to this game. "
               "If I say an item is fixed, wrong or should wait, use mark_item (it learns from this and checks "
               "fixes later). If players are falling and I ask why, use player_trend. "
               "Don't call other tools unless I ask.")


class Server:
    def __init__(self, conn=None, universe_id: int | None = None, client: AnalyticsClient | None = None):
        cfg = config.load()
        self.universe_id = universe_id or cfg.universe_id
        self.conn = conn or db.connect(config.db_path())
        self.client = client
        if self.client is None and cfg.api_key and self.universe_id:
            self.client = AnalyticsClient(api_key=cfg.api_key, universe_id=self.universe_id)

    # ---- tools ---------------------------------------------------------
    def _stale(self) -> bool:
        row = self.conn.execute("SELECT MAX(started_at) AS t FROM pull_runs WHERE universe_id=? AND status!='failed'",
                                (self.universe_id,)).fetchone()
        if not row or not row["t"]:
            return True
        last = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last > timedelta(hours=STALE_HOURS)

    def daily_checkup(self, refresh: bool = True, weekly: bool | None = None) -> str:
        if not self.universe_id:
            return "Not set up. Ask the user to run 'roblox-agent setup' in a terminal."
        note = ""
        if refresh and self.client and self._stale():
            try:
                ingest.pull(self.client, self.conn, progress=lambda m: print(m, file=sys.stderr))
            except ApiError as e:
                note = f"\n(Refresh failed, showing saved data: {e})"
        weekly = True if weekly else None  # None = only when a new week starts
        return checkup.run(self.conn, self.universe_id, weekly=weekly).text() + note

    def mark_item(self, item: int, action: str) -> str:
        return checkup.mark(self.conn, self.universe_id, item, action)

    def log_change(self, title: str, scores: dict | None = None, shipped_on: str | None = None,
                   target: str | None = None) -> str:
        if not scores:
            return ("Score each factor 0-3 from what you know about the change (ask the user if unsure), "
                    "then call log_change again with scores.\n" + changes.questions())
        try:
            day = date.fromisoformat(shipped_on) if shipped_on else None
            return changes.log(self.conn, self.universe_id, title, scores, day, target)
        except (ValueError, TypeError) as e:
            return f"Not logged: {e}"

    def player_trend(self) -> str:
        return why.report(self.conn, self.universe_id)

    def metric_history(self, metric: str, days: int = 14, split: str = "") -> str:
        days = max(1, min(int(days or 14), MAX_HISTORY_DAYS))
        rows = self.conn.execute(
            "SELECT substr(bucket_time,1,10) AS d, value, status FROM data_points WHERE universe_id=? AND metric=?"
            " AND granularity='OneDay' AND breakdown_key=? ORDER BY bucket_time DESC LIMIT ?",
            (self.universe_id, metric, split or "", days)).fetchall()
        if not rows:
            return f"No data for {metric} {split}".strip() + ". Use list_metrics for valid names."
        flag = {"Projected": " est", checkup.NSS: " lowsample"}
        lines = [f"{metric}{' ' + split if split else ''} (date value):"]
        lines += [f"{r['d']} {checkup._num(r['value'])}{flag.get(r['status'] or '', '')}"
                  for r in reversed(rows) if r["value"] is not None]
        return "\n".join(lines)

    def game_context(self) -> str:
        ctx = funnels.load_context()
        if not ctx:
            ctx, _ = funnels.refresh_context()
        return funnels.context_text(ctx)

    def add_funnels(self, funnels: list | None = None) -> str:
        return _funnels.add_text(_funnels.add(funnels or []))

    def list_metrics(self) -> str:
        rows = self.conn.execute(
            "SELECT metric, group_concat(DISTINCT breakdown_key) AS keys FROM data_points WHERE universe_id=?"
            " GROUP BY metric", (self.universe_id,)).fetchall()
        if not rows:
            return "No data stored yet."
        out = []
        for r in rows:
            splits = [k for k in (r["keys"] or "").split(",") if k]
            label = catalog.get(r["metric"]).label
            out.append(f"{r['metric']} ({label})" + (f" splits: {', '.join(splits)}" if splits else ""))
        return "\n".join(out)

    # ---- protocol ------------------------------------------------------
    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:  # notification (e.g. notifications/initialized): no reply
            return None
        params = msg.get("params") or {}
        meta_version = (params.get("_meta") or {}).get(META_VERSION_KEY) if isinstance(params, dict) else None
        try:
            result = self._dispatch(method, params)
            if meta_version in STATELESS_VERSIONS:
                result = {**result, "resultType": "complete"}
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:  # noqa: BLE001 - never crash the agent's session
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"Internal error: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": version,
                    "capabilities": {"tools": {}, "prompts": {}},
                    "serverInfo": {"name": "roblox-agent", "version": __version__},
                    "instructions": "Roblox game analytics from a local database. Start with daily_checkup."}
        if method == "server/discover":
            return {"supportedVersions": list(STATELESS_VERSIONS + PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {}, "prompts": {}},
                    "serverInfo": {"name": "roblox-agent", "version": __version__},
                    "instructions": "Roblox game analytics from a local database. Start with daily_checkup."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "prompts/list":
            return {"prompts": PROMPTS}
        if method == "prompts/get":
            texts = {"daily-checkup": PROMPT_TEXT, "plan-funnels": funnels.PLAN_PROMPT}
            name = params.get("name")
            if name not in texts:
                raise _RpcError(-32602, "Unknown prompt")
            desc = next(p["description"] for p in PROMPTS if p["name"] == name)
            return {"description": desc,
                    "messages": [{"role": "user", "content": {"type": "text", "text": texts[name]}}]}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = {"daily_checkup": self.daily_checkup, "metric_history": self.metric_history,
                  "list_metrics": self.list_metrics, "mark_item": self.mark_item,
                  "log_change": self.log_change, "player_trend": self.player_trend,
                  "game_context": self.game_context, "add_funnels": self.add_funnels}.get(name)
            if not fn:
                raise _RpcError(-32602, f"Unknown tool: {name}")
            try:
                text = fn(**args)
                is_error = False
            except TypeError:
                text, is_error = f"Bad arguments for {name}.", True
            return {"content": [{"type": "text", "text": text}], "isError": is_error}
        raise _RpcError(-32601, f"Method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None, server: Server | None = None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = server or Server()
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
            stdout.flush()
