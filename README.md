# Roblox Analytics Agent

![tests](https://github.com/Boking234/roblox-analytics-agent/actions/workflows/tests.yml/badge.svg)

A free, open-source, **local-first** daily check-up for your Roblox game. It pulls your
analytics from the official [Analytics Query API](https://create.roblox.com/docs/cloud/guides/analytics)
into a SQLite database on your computer. It then tells you the **top 3 things that changed today**
in your terminal, in a browser page, or through your AI agent (Claude Code, Claude Desktop, Codex,
Cursor, VS Code, Gemini CLI, Windsurf) via a built-in MCP server.

- No cloud server, no paid APIs, no account. Your data and API key never leave your computer.
- Needs only Python 3.10+ (standard library only, nothing else to install).
- Keeps performance data past Roblox's 28-day limit by snapshotting it on every pull.
- Cheap for AI: all math runs locally, and the agent gets ~300-400 tokens per check-up
  (tests enforce the limit). See [design notes](docs/DESIGN_NOTES.md).

**Not a coder? Follow the [setup guide](docs/SETUP_GUIDE.md).**

## Quick start

```bash
pip install https://github.com/Boking234/roblox-analytics-agent/archive/refs/heads/main.zip
roblox-agent demo      # see it with made-up numbers, no key needed
roblox-agent           # first time: 3-step setup; after that: today's top 3
roblox-agent view      # the same as a page, with all charts folded below
roblox-agent connect   # pick your AI agent (claude-code, claude-desktop, codex, cursor, vscode, gemini, windsurf)
```

Advanced: `pull`, `show`, `export`, `check`, `where`, `forget-key`, `mcp`.

## AI agents (MCP)

`roblox-agent connect` registers `python -m roblox_agent mcp` with your agent. It uses the full
Python path, so PATH problems don't matter. The server speaks MCP over stdio using only the
standard library and supports protocol versions 2024-11-05 through 2025-11-25 plus the stateless
2026-07-28. Tools:

| Tool | Returns |
| --- | --- |
| `daily_checkup` | Status plus the top 3 changes. Refreshes data if older than 20h. |
| `metric_history` | One metric, at most 90 compact lines |
| `list_metrics` | Stored metric names and splits |

There's also a `daily-checkup` prompt. Verified with Claude Code: `claude mcp list` shows it
connected, and `claude -p "Run my Roblox daily check-up"` answered from the tool.

## What it pulls

Whole game, plus splits by platform / new vs returning where useful:

| Group | Metrics |
| --- | --- |
| Engagement | DailyActiveUsers, Visits, AverageSessionLengthMinutes |
| Retention | ForwardD1Retention, ForwardD7Retention |
| Money | DailyRevenue, PayingUsersCVR |
| Performance (28-day retention on Roblox) | ClientCrashRate15m, ClientFpsP10, ClientMemoryUsageP90, OomUnexpectedExits, PeakConcurrentPlayers, ServerFrameRateAvg, ServerCrashCount |

Add others with `roblox-agent pull --metrics Name1,Name2` (names from the
[metrics list](https://create.roblox.com/docs/cloud/guides/analytics/metrics)).

## How it works

```
Roblox Analytics Query API ──► api.py (retries, waits on slow 202 queries, splits too-big ranges)
                               ingest.py (picks date ranges; re-pulls last 10 days to refresh estimates)
                               db.py ──► ~/.roblox-agent/analytics.db (SQLite)
                               checkup.py ──► status + top 3 (same weekday baseline, skips low-sample days)
                               report.py ──► view (HTML) / show (text) / export (CSV)
                               mcp_server.py ──► Claude / Codex / Cursor / ... (small text only)
```

Database tables: `data_points` (one row per metric, split, and day, with Roblox's status:
Valid / Projected / NotStatisticallySignificant), `raw_responses` (exact JSON from Roblox),
`pull_runs`, `pull_errors`.

## Privacy and safety

See [SECURITY.md](SECURITY.md). The key lives in `~/.roblox-agent/secrets.env`, outside the repo,
and `.gitignore` also blocks `.env` and database files.

## Development

```bash
pip install -e ".[dev]"
pytest          # uses a pretend Roblox server: no internet or key needed
ruff check .
```

Tests run on Windows, macOS, and Linux on every push (GitHub Actions).

## Roadmap

1. **Data pull, daily top 3, and MCP server for AI agents** (this release)
2. Smarter checks (drop-off map, release/PlaceVersion links), mark issues fixed or ignored
3. Check-up streaks and a "send to my agent" button
4. App screens and one-step installer
5. Discord DM notifications
6. Opt-in community lessons database

## License

MIT
