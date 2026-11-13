"""The daily check-up: find the 3 things that matter most today.

All the math runs here, on your computer, for free. An AI agent only ever
receives the short text this produces (a few hundred characters), never the
raw data. That keeps each check-up to well under 1,000 tokens.

How a metric gets flagged
-------------------------
For the latest trustworthy day, compare it with the *same weekday* over the
previous 4 weeks (so weekends aren't confused with problems). A change is
flagged only if it is both:
  * big:    at least ``min_change`` (10%) away from that usual value, and
  * unusual: outside the normal day-to-day wobble of the last 28 days.
Days Roblox marks "too few players to trust" are skipped.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median

from . import catalog

# True = higher is better. Missing metrics default to True.
HIGHER_IS_BETTER = {
    "ClientCrashRate15m": False, "ClientMemoryUsageP90": False, "OomUnexpectedExits": False,
    "ServerCrashCount": False,
}
# How much each metric matters when ranking (crashes and retention first).
WEIGHT = {
    "ClientCrashRate15m": 3.0, "ServerCrashCount": 2.0, "OomUnexpectedExits": 2.0,
    "ForwardD1Retention": 3.0, "ForwardD7Retention": 2.0, "DailyActiveUsers": 2.0,
    "DailyRevenue": 2.0, "PayingUsersCVR": 1.5,
}
MIN_CHANGE = 0.10
NSS = "NotStatisticallySignificant"


@dataclass
class Finding:
    metric: str
    label: str
    split: str          # '' for whole game, else e.g. 'Platform=Phone'
    day: str
    value: float
    usual: float
    change: float       # fraction, +0.25 = 25% higher than usual
    good: bool          # True if the change is in the good direction
    provisional: bool   # Roblox marked the day as an estimate
    score: float

    def line(self) -> str:
        where = f" on {self.split.split('=', 1)[1]}" if self.split else ""
        word = "up" if self.change > 0 else "down"
        tag = "GOOD" if self.good else "ISSUE"
        est = " (estimate)" if self.provisional else ""
        return (f"{tag}: {self.label}{where} {word} {abs(self.change) * 100:.0f}% "
                f"({_num(self.value)} vs usual {_num(self.usual)}, {self.day}){est}")


@dataclass
class Checkup:
    universe_id: int
    status: str              # "All good" | "Watch" | "Act now" | "No data"
    day: str | None
    top: list[Finding]
    checked: int
    notes: list[str]

    def text(self) -> str:
        """Compact plain text. This is all an AI agent needs to see."""
        head = f"Roblox game check-up, universe {self.universe_id}, data through {self.day or '-'}: {self.status}"
        lines = [head]
        if self.top:
            lines += [f"{i}. {f.line()}" for i, f in enumerate(self.top, 1)]
        elif self.checked:
            lines.append(f"No unusual changes across {self.checked} metrics.")
        lines += self.notes
        return "\n".join(lines)


def _num(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.3g}"


def _rows(conn: sqlite3.Connection, uid: int, metric: str, key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bucket_time, value, status FROM data_points WHERE universe_id=? AND metric=?"
        " AND granularity='OneDay' AND breakdown_key=? AND value IS NOT NULL ORDER BY bucket_time",
        (uid, metric, key)).fetchall()


def _day(ts: str) -> date:
    return date.fromisoformat(ts[:10])


def evaluate(rows: list[sqlite3.Row], higher_is_better: bool = True,
             min_change: float = MIN_CHANGE) -> tuple[str, float, float, float, bool] | None:
    """Return (day, value, usual, change, provisional) if the latest day is unusual, else None."""
    good_rows = [r for r in rows if r["status"] != NSS]
    if len(good_rows) < 8:
        return None
    latest = good_rows[-1]
    d = _day(latest["bucket_time"])
    by_day = {_day(r["bucket_time"]): r["value"] for r in good_rows}
    same_weekday = [by_day[d - timedelta(weeks=k)] for k in range(1, 5) if d - timedelta(weeks=k) in by_day]
    window = [r["value"] for r in good_rows[-29:-1]]
    if len(same_weekday) >= 2:
        usual = median(same_weekday)
    elif len(window) >= 7:
        usual = median(window)
    else:
        return None
    value = latest["value"]
    if usual == 0:
        if value == 0:
            return None
        change = 1.0 if value > 0 else -1.0
    else:
        change = (value - usual) / abs(usual)
    if abs(change) < min_change:
        return None
    # "Unusual": bigger than 2.5x the typical day-to-day wobble.
    mid = median(window)
    wobble = median(abs(v - mid) for v in window) * 1.4826
    if wobble and abs(value - usual) < 2.5 * wobble:
        return None
    return latest["bucket_time"][:10], value, usual, change, latest["status"] == "Projected"


def run(conn: sqlite3.Connection, universe_id: int, today: date | None = None) -> Checkup:
    today = today or datetime.now(timezone.utc).date()
    findings: list[Finding] = []
    checked = 0
    latest_day: str | None = None
    for spec in catalog.DEFAULT_METRICS:
        hib = HIGHER_IS_BETTER.get(spec.name, True)
        keys = [""]
        for dims in spec.breakdowns:
            if dims:
                keys += [r[0] for r in conn.execute(
                    "SELECT DISTINCT breakdown_key FROM data_points WHERE universe_id=? AND metric=?"
                    " AND breakdown_key LIKE ?", (universe_id, spec.name, dims[0] + "=%"))]
        whole_change = None
        for key in keys:
            rows = _rows(conn, universe_id, spec.name, key)
            if not rows:
                continue
            if key == "":
                checked += 1
                last = rows[-1]["bucket_time"][:10]
                latest_day = max(latest_day or last, last)
            res = evaluate(rows, hib)
            if not res:
                continue
            day, value, usual, change, prov = res
            if key == "":
                whole_change = change
            elif whole_change is not None and abs(change - whole_change) < 0.10:
                continue  # the split just mirrors the whole game; no extra news
            good = (change > 0) == hib
            score = abs(change) * WEIGHT.get(spec.name, 1.0) * (1.0 if key == "" else 0.8)
            findings.append(Finding(spec.name, spec.label, key, day, value, usual, change, good, prov, score))

    issues = sorted((f for f in findings if not f.good), key=lambda f: -f.score)
    wins = sorted((f for f in findings if f.good), key=lambda f: -f.score)
    top = (issues + wins)[:3]
    notes: list[str] = []
    if not checked:
        status = "No data"
        notes.append("No data yet: run 'roblox-agent pull'.")
    elif any(f.score >= 0.75 for f in issues):
        status = "Act now"
    elif issues:
        status = "Watch"
    else:
        status = "All good"
    if latest_day and (today - date.fromisoformat(latest_day)).days > 3:
        notes.append(f"Data is {(today - date.fromisoformat(latest_day)).days} days old: run 'roblox-agent pull'.")
    if checked and len(findings) > len(top):
        notes.append(f"{len(findings) - len(top)} smaller change(s) not shown.")
    return Checkup(universe_id, status, latest_day, top, checked, notes)
