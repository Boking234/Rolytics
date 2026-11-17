"""Local SQLite storage. Everything stays on your computer.

Tables
------
pull_runs      one row per time you run a pull (when, how it went)
raw_responses  the exact JSON Roblox sent back, kept so nothing is lost
data_points    one row per metric / breakdown / time bucket (the clean data)
pull_errors    metrics that failed on a run, with a plain-language reason
settings       small key/value store (schema version etc.)
findings       what the check-ups found over time, and what you said about it
checkups       one row per data day: the status and the top 3 that were shown
changes        game changes you shipped, scored with the change-weight rubric
feedback       every "fixed" / "snooze" / "false alarm" click, so the checks can adapt
inbox          reports and alerts the desktop app showed you (its in-app inbox)
change_effects how each judged change moved each number (to learn which changes help or hurt)
fix_tries      fixes you marked done, and whether the numbers really recovered afterwards
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_id  INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
    points_saved INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS raw_responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pull_runs(id),
    universe_id  INTEGER NOT NULL,
    metric       TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_points (
    universe_id   INTEGER NOT NULL,
    metric        TEXT NOT NULL,
    granularity   TEXT NOT NULL,
    breakdown_key TEXT NOT NULL,  -- '' for no breakdown, else e.g. 'Platform=Phone'
    bucket_time   TEXT NOT NULL,  -- UTC start of the time bucket
    value         REAL,
    string_values TEXT,           -- JSON list, only for text-valued metrics
    status        TEXT,           -- Valid | Projected | NotStatisticallySignificant | NULL
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    run_id        INTEGER NOT NULL REFERENCES pull_runs(id),
    PRIMARY KEY (universe_id, metric, granularity, breakdown_key, bucket_time)
);

CREATE INDEX IF NOT EXISTS idx_points_metric_time
    ON data_points (universe_id, metric, bucket_time);

CREATE TABLE IF NOT EXISTS pull_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES pull_runs(id),
    metric      TEXT NOT NULL,
    breakdown   TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    universe_id   INTEGER NOT NULL,
    key           TEXT NOT NULL,      -- metric|split|direction[|trend|budget]
    metric        TEXT NOT NULL,
    split         TEXT NOT NULL,
    good          INTEGER NOT NULL,
    first_day     TEXT NOT NULL,      -- first data day it was seen
    last_day      TEXT NOT NULL,      -- latest data day it was seen
    days_shown    INTEGER NOT NULL DEFAULT 0,
    last_shown    TEXT,
    state         TEXT NOT NULL DEFAULT 'open',  -- open | done | snoozed | wrong
    state_until   TEXT,
    peak_rel      REAL,
    last_line     TEXT,
    updated_at    TEXT NOT NULL,
    fix           TEXT,               -- id of the fix suggested for it (see fixes.py)
    PRIMARY KEY (universe_id, key)
);

CREATE TABLE IF NOT EXISTS checkups (
    universe_id INTEGER NOT NULL,
    data_day    TEXT NOT NULL,
    status      TEXT NOT NULL,
    top_keys    TEXT NOT NULL,        -- JSON list, in the order shown (for "mark 1 done")
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (universe_id, data_day)
);

CREATE TABLE IF NOT EXISTS changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_id  INTEGER NOT NULL,
    title        TEXT NOT NULL,
    shipped_on   TEXT NOT NULL,
    scores       TEXT NOT NULL,       -- JSON {factor: 0-3}
    weight       INTEGER NOT NULL,
    class        TEXT NOT NULL,       -- Light | Medium | Heavy | Major
    judge_on     TEXT NOT NULL,
    target       TEXT,                -- main metric it should move
    outcome      TEXT,                -- good | bad | inconclusive (NULL = waiting)
    outcome_note TEXT,
    announced    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_id INTEGER NOT NULL,
    metric      TEXT NOT NULL,
    split       TEXT NOT NULL,
    action      TEXT NOT NULL,        -- done | wrong | snooze | worked | failed
    day         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT ''  -- sudden | trend | budget ('' = before v0.5)
);

CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_id INTEGER NOT NULL,
    kind        TEXT NOT NULL,        -- daily | alert | note
    status      TEXT,                 -- All good | Watch | Act now (daily reports)
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    data_day    TEXT,
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_effects (
    change_id   INTEGER NOT NULL REFERENCES changes(id),
    metric      TEXT NOT NULL,
    rel         REAL NOT NULL,        -- after vs before, as a share
    z           REAL NOT NULL,
    verdict     TEXT NOT NULL,        -- better | worse | same | unknown
    PRIMARY KEY (change_id, metric)
);

CREATE TABLE IF NOT EXISTS fix_tries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_id INTEGER NOT NULL,
    key         TEXT NOT NULL,        -- the finding it was meant to fix
    metric      TEXT NOT NULL,
    split       TEXT NOT NULL,
    fix         TEXT NOT NULL,        -- id of the suggestion shown then ('' = none)
    marked_day  TEXT NOT NULL,        -- data day it was marked fixed
    check_on    TEXT NOT NULL,        -- data day to check it
    usual       REAL,                 -- normal level before the problem
    bad         REAL,                 -- level while the problem lasted
    after       REAL,
    result      TEXT,                 -- worked | partly | failed | unknown (NULL = waiting)
    announced   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""

# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = (
    ("feedback", "kind", "TEXT NOT NULL DEFAULT ''"),
    ("findings", "fix", "TEXT"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)  # wait if a pull is writing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read a small JSON setting (sensitivity, your own limits...)."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()


def breakdown_key(breakdowns: Iterable[dict[str, Any]]) -> str:
    """Turn [{'dimension':'Platform','value':'Phone'}] into 'Platform=Phone'."""
    parts = [f"{b.get('dimension', '?')}={b.get('value', '')}" for b in breakdowns or []]
    return "|".join(sorted(parts))


def start_run(conn: sqlite3.Connection, universe_id: int) -> int:
    cur = conn.execute("INSERT INTO pull_runs (universe_id, started_at) VALUES (?, ?)",
                       (universe_id, utcnow()))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, points: int,
               note: str | None = None) -> None:
    conn.execute("UPDATE pull_runs SET finished_at=?, status=?, points_saved=?, note=? WHERE id=?",
                 (utcnow(), status, points, note, run_id))
    conn.commit()


def save_raw(conn: sqlite3.Connection, run_id: int, universe_id: int, metric: str,
             request: dict[str, Any], response: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO raw_responses (run_id, universe_id, metric, request_json, response_json, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, universe_id, metric, json.dumps(request, sort_keys=True),
         json.dumps(response, sort_keys=True), utcnow()))


def save_points(conn: sqlite3.Connection, run_id: int, universe_id: int, metric: str,
                granularity: str, operation: dict[str, Any]) -> int:
    """Store every data point from a finished operation. Returns how many."""
    now = utcnow()
    count = 0
    values = ((operation or {}).get("response") or {}).get("values") or []
    for series in values:
        key = breakdown_key(series.get("breakdowns") or [])
        for dp in series.get("dataPoints") or []:
            t = dp.get("time")
            if not t:
                continue
            sv = dp.get("stringValues")
            conn.execute(
                """
                INSERT INTO data_points (universe_id, metric, granularity, breakdown_key, bucket_time,
                                         value, string_values, status, first_seen_at, updated_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (universe_id, metric, granularity, breakdown_key, bucket_time) DO UPDATE SET
                    value=excluded.value, string_values=excluded.string_values,
                    status=excluded.status, updated_at=excluded.updated_at, run_id=excluded.run_id
                """,
                (universe_id, metric, granularity, key, t, dp.get("value"),
                 json.dumps(sv) if sv is not None else None, dp.get("status"), now, now, run_id))
            count += 1
    return count


def save_error(conn: sqlite3.Connection, run_id: int, metric: str, breakdown: str, message: str) -> None:
    conn.execute("INSERT INTO pull_errors (run_id, metric, breakdown, message, created_at) VALUES (?, ?, ?, ?, ?)",
                 (run_id, metric, breakdown, message, utcnow()))


def earliest_point_time(conn: sqlite3.Connection, universe_id: int, metric: str,
                        granularity: str, dims: list[str] | None = None) -> str | None:
    """Oldest bucket stored for a metric, for one breakdown choice (same rules as latest_point_time)."""
    sql = ("SELECT MIN(bucket_time) AS t FROM data_points WHERE universe_id=? AND metric=?"
           " AND granularity=?")
    args: list[Any] = [universe_id, metric, granularity]
    if dims:
        sql += " AND breakdown_key LIKE ?"
        args.append(min(dims) + "=%")
    else:
        sql += " AND breakdown_key = ''"
    row = conn.execute(sql, args).fetchone()
    return row["t"] if row and row["t"] else None


def latest_point_time(conn: sqlite3.Connection, universe_id: int, metric: str,
                      granularity: str, dims: list[str] | None = None) -> str | None:
    """Most recent bucket stored for a metric, for one breakdown choice.

    ``dims=None`` means the plain (no breakdown) series; ``['Platform']`` means
    any 'Platform=...' series.
    """
    sql = ("SELECT MAX(bucket_time) AS t FROM data_points WHERE universe_id=? AND metric=?"
           " AND granularity=?")
    args: list[Any] = [universe_id, metric, granularity]
    if dims:
        sql += " AND breakdown_key LIKE ?"
        args.append(min(dims) + "=%")
    else:
        sql += " AND breakdown_key = ''"
    row = conn.execute(sql, args).fetchone()
    return row["t"] if row and row["t"] else None
