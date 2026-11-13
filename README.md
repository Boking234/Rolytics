# Roblox Analytics Agent

![tests](https://github.com/Boking234/roblox-analytics-agent/actions/workflows/tests.yml/badge.svg)

A free, open-source, **local-first** tool that pulls your own Roblox game's analytics from the
official [Analytics Query API](https://create.roblox.com/docs/cloud/guides/analytics) into a
SQLite database on your computer, and shows it as charts, a table, or a spreadsheet.

- No cloud server, no paid APIs, no account. Your data and API key never leave your computer.
- Needs only Python 3.10+ (standard library only, nothing else to install).
- Keeps performance data past Roblox's 28-day limit by snapshotting it on every pull.

**Not a coder? Follow the [setup guide](docs/SETUP_GUIDE.md).**

## Quick start

```bash
pip install https://github.com/Boking234/roblox-analytics-agent/archive/refs/heads/main.zip
roblox-agent demo     # try it with pretend data, no key needed
roblox-agent setup    # Universe ID + API key (universe.analytics:read)
roblox-agent pull     # first run backfills 365 days
roblox-agent view     # charts in your browser
```

Other commands: `show` (terminal table), `export` (CSV), `check` (test the key),
`where` (data location), `forget-key`.

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
                               report.py ──► view (HTML) / show (text) / export (CSV)
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

1. **Data pull foundation** (this release)
2. Checks and a daily top 3 (anomaly detection, drop-off map)
3. MCP server so Claude Code can query your game
4. App screens and one-step installer
5. Discord DM notifications
6. Opt-in community lessons database

## License

MIT
