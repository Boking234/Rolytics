"""Pull metrics from Roblox into the local database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import catalog, db
from .api import AnalyticsClient, ApiError, AuthError, BudgetError

GRANULARITY = "OneDay"
# How much history to keep for players/retention/money metrics. Doubled from 365 to 730 days
# (2026-11-17): Roblox keeps ~4 years, but only serves daily values for ranges up to 2 years.
DEFAULT_BACKFILL_DAYS = 730
# Never ask Roblox for more than this many days in one request (keeps each query small).
MAX_DAYS_PER_QUERY = 365
# Re-pull this many recent days each run, because Roblox revises recent
# "Projected" values and D7 retention needs 7 days to settle.
REFRESH_DAYS = 10


@dataclass
class PullResult:
    run_id: int
    points: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "ok"


def _iso(d: date) -> str:
    return f"{d.isoformat()}T00:00:00Z"


def _parse_day(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def plan_window(spec: catalog.MetricSpec, last_stored: str | None, end: date,
                backfill_days: int) -> tuple[date, date]:
    """Choose [start, end) for one metric. End is exclusive (today = through yesterday)."""
    if spec.kind == catalog.PERFORMANCE:
        earliest = end - timedelta(days=catalog.PERFORMANCE_LOOKBACK_DAYS)
    else:
        earliest = end - timedelta(days=backfill_days)
    if last_stored:
        start = _parse_day(last_stored) - timedelta(days=REFRESH_DAYS)
        start = max(start, earliest)
    else:
        start = earliest
    if start >= end:
        start = end - timedelta(days=1)
    return start, end


def _fetch_range(client: AnalyticsClient, conn: sqlite3.Connection, run_id: int,
                 spec: catalog.MetricSpec, dims: tuple[str, ...], start: date, end: date) -> int:
    """Query one date range; on 'too big' errors split it in half and try again."""
    if (end - start).days > MAX_DAYS_PER_QUERY:
        mid = end - timedelta(days=MAX_DAYS_PER_QUERY)
        return (_fetch_range(client, conn, run_id, spec, dims, start, mid)
                + _fetch_range(client, conn, run_id, spec, dims, mid, end))
    try:
        op = client.query_metric(spec.name, GRANULARITY, _iso(start), _iso(end),
                                 breakdown=list(dims) or None)
    except BudgetError:
        days = (end - start).days
        if days <= 1:
            raise
        mid = start + timedelta(days=days // 2)
        return (_fetch_range(client, conn, run_id, spec, dims, start, mid)
                + _fetch_range(client, conn, run_id, spec, dims, mid, end))
    request = {"metric": spec.name, "granularity": GRANULARITY, "startTime": _iso(start),
               "endTime": _iso(end), "breakdown": list(dims)}
    db.save_raw(conn, run_id, client.universe_id, spec.name, request, op)
    n = db.save_points(conn, run_id, client.universe_id, spec.name, GRANULARITY, op)
    conn.commit()
    return n


def _deepen(client: AnalyticsClient, conn: sqlite3.Connection, run_id: int, spec: catalog.MetricSpec,
            dims: tuple[str, ...], end: date, backfill_days: int, last: str | None,
            progress: Callable[[str], None]) -> int:
    """Fill in older history once, for databases started with a shorter backfill.

    Only standard metrics (performance data is gone from Roblox after 28 days).
    Remembers how far back it has asked, so empty older ranges (a game that is
    younger than the window) are never requested twice.
    """
    if spec.kind == catalog.PERFORMANCE or not last:
        return 0
    want = end - timedelta(days=backfill_days)
    key = f"history_from:{client.universe_id}:{spec.name}:{','.join(dims)}"
    asked = db.get_setting(conn, key)
    first = db.earliest_point_time(conn, client.universe_id, spec.name, GRANULARITY, list(dims))
    have_from = _parse_day(first) if first else end
    if asked:
        have_from = min(have_from, date.fromisoformat(asked))
    if have_from <= want:
        return 0
    progress(f"  older history: {want} to {have_from - timedelta(days=1)}")
    n = _fetch_range(client, conn, run_id, spec, dims, want, have_from)
    db.set_setting(conn, key, want.isoformat())
    conn.commit()
    return n


def pull(client: AnalyticsClient, conn: sqlite3.Connection,
         metrics: Iterable[catalog.MetricSpec] = catalog.DEFAULT_METRICS,
         backfill_days: int = DEFAULT_BACKFILL_DAYS, end: date | None = None,
         progress: Callable[[str], None] = lambda _msg: None) -> PullResult:
    """Pull every metric (and each of its breakdowns) and store it.

    One failing metric doesn't stop the others. A bad API key stops the run.
    """
    end = end or today_utc()
    run_id = db.start_run(conn, client.universe_id)
    result = PullResult(run_id=run_id)
    jobs = [(spec, dims) for spec in metrics for dims in spec.breakdowns]
    try:
        for spec, dims in jobs:
            label = spec.name + (f" by {', '.join(dims)}" if dims else "")
            last = db.latest_point_time(conn, client.universe_id, spec.name, GRANULARITY, list(dims))
            start, stop = plan_window(spec, last, end, backfill_days)
            progress(f"{label}: {start} to {stop - timedelta(days=1)}")
            try:
                n = _fetch_range(client, conn, run_id, spec, dims, start, stop)
                n += _deepen(client, conn, run_id, spec, dims, end, backfill_days, last, progress)
                result.points += n
                progress(f"  saved {n} data points")
            except AuthError:
                raise
            except ApiError as e:
                msg = str(e)
                db.save_error(conn, run_id, spec.name, ",".join(dims), msg)
                conn.commit()
                result.errors.append(f"{label}: {msg}")
                progress(f"  skipped: {msg}")
    except AuthError as e:
        result.errors.append(str(e))
        result.status = "failed"
        db.finish_run(conn, run_id, result.status, result.points, str(e))
        raise
    if result.errors:
        result.status = "partial" if result.points else "failed"
    db.finish_run(conn, run_id, result.status, result.points,
                  f"{len(result.errors)} metric(s) skipped" if result.errors else None)
    return result
