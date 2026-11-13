"""Local SQLite storage. Everything stays on your computer.

Tables
------
pull_runs      one row per time you run a pull (when, how it went)
raw_responses  the exact JSON Roblox sent back, kept so nothing is lost
data_points    one row per metric / breakdown / time bucket (the clean data)
pull_errors    metrics that failed on a run, with a plain-language reason
settings       small key/value store (schema version etc.)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

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
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)  # wait if a pull is writing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


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
