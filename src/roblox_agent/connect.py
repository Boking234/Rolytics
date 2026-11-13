"""Connect this tool to the AI agent you use.

Each agent gets the same local MCP server: ``<python> -m roblox_agent mcp``.
We use the full path to Python so it works even when ``roblox-agent`` isn't
on PATH. The API key is NEVER written into any agent's settings: the server
reads it from your local secrets file when it starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NAME = "roblox-agent"


def server_command() -> tuple[str, list[str]]:
    return sys.executable, ["-m", "roblox_agent", "mcp"]


@dataclass(frozen=True)
class Agent:
    key: str
    label: str
    kind: str                 # "cli" (run its own add command) or "json" (edit its config file)
    cli: str = ""             # program name for kind=cli
    json_key: str = "mcpServers"


AGENTS: tuple[Agent, ...] = (
    Agent("claude-code", "Claude Code", "cli", cli="claude"),
    Agent("claude-desktop", "Claude Desktop app", "json"),
    Agent("codex", "OpenAI Codex CLI", "cli", cli="codex"),
    Agent("cursor", "Cursor", "json"),
    Agent("vscode", "VS Code (GitHub Copilot)", "cli", cli="code"),
    Agent("gemini", "Gemini CLI", "cli", cli="gemini"),
    Agent("windsurf", "Windsurf", "json"),
    Agent("other", "Other / show me the settings", "manual"),
)
BY_KEY = {a.key: a for a in AGENTS}


def json_config_path(agent: Agent) -> Path:
    home = Path.home()
    if agent.key == "claude-desktop":
        if sys.platform == "win32":
            return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    if agent.key == "cursor":
        return home / ".cursor" / "mcp.json"
    if agent.key == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    raise ValueError(agent.key)


def cli_add_command(agent: Agent) -> list[str]:
    cmd, args = server_command()
    if agent.key == "claude-code":
        return ["claude", "mcp", "add", "--scope", "user", NAME, "--", cmd, *args]
    if agent.key == "codex":
        return ["codex", "mcp", "add", NAME, "--", cmd, *args]
    if agent.key == "gemini":
        return ["gemini", "mcp", "add", "--scope", "user", NAME, cmd, *args]
    if agent.key == "vscode":
        return ["code", "--add-mcp", json.dumps({"name": NAME, "type": "stdio", "command": cmd, "args": args})]
    raise ValueError(agent.key)


def merge_json_config(path: Path, json_key: str = "mcpServers", extra: dict | None = None) -> Path | None:
    """Add our server to an agent's JSON config. Backs up the old file. Returns backup path."""
    cmd, args = server_command()
    data: dict = {}
    backup = None
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            data = json.loads(text)  # raises ValueError on a broken file: we don't overwrite it
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
    data.setdefault(json_key, {})[NAME] = {**(extra or {}), "command": cmd, "args": args}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return backup


def manual_instructions() -> str:
    cmd, args = server_command()
    snippet = json.dumps({"mcpServers": {NAME: {"command": cmd, "args": args}}}, indent=2)
    return ("Add this MCP server to your agent's settings (most agents use this JSON shape):\n"
            f"{snippet}\n\nFor Codex's ~/.codex/config.toml:\n"
            f"[mcp_servers.{NAME}]\ncommand = {json.dumps(cmd)}\nargs = {json.dumps(args)}")


def connect(agent: Agent, run=subprocess.run, which=shutil.which) -> tuple[bool, str]:
    """Set up the agent. Returns (ok, message for the user)."""
    if agent.kind == "manual":
        return True, manual_instructions()
    if agent.kind == "json":
        path = json_config_path(agent)
        try:
            extra = {"type": "stdio"} if agent.key == "cursor" else None
            backup = merge_json_config(path, agent.json_key, extra)
        except ValueError:
            return False, (f"{path} has a formatting error, so I didn't touch it. "
                           f"Fix it or add this by hand:\n{manual_instructions()}")
        msg = f"Added to {path}."
        if backup:
            msg += f" (Old version saved as {backup.name}.)"
        return True, msg + f" Restart {agent.label} to load it."
    # kind == "cli"
    cmd = cli_add_command(agent)
    if not which(agent.cli):
        return False, (f"I couldn't find the '{agent.cli}' command. Install {agent.label} first, "
                       f"or run this yourself later:\n  " + " ".join(_quote(c) for c in cmd))
    result = run(cmd, capture_output=True, text=True)
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0 and "already exists" not in out.lower():
        return False, f"{agent.label} reported a problem:\n{out}"
    return True, f"Connected to {agent.label}." + (" (It was already connected.)" if "already" in out.lower() else "")


def _quote(s: str) -> str:
    return f'"{s}"' if (" " in s or "{" in s) else s
