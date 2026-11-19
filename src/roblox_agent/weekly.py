"""The weekly check-up (plan Part 4d), added to the first check-up of each week.

1. Health scorecard: each area vs last week and vs the 4 weeks before.
   Whole weeks are compared, so every weekday counts once and busy weekends
   can't fake a change.
2. Issues: new this week, still going, and fixed.
3. Changes you shipped: good, harmful, no clear result, or still waiting,
   plus what your judged changes taught so far ("2 of your 3 economy
   changes lowered paying conversion").
4. Next week: the top 3 things to work on.

(Lessons from other games come later, with the community database.)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import catalog, changes, decline, detect, priors
from .checkup import _day, _where, fmt_change

# One headline metric per area, in scorecard order.
AREAS = (
    ("Stability", "ClientCrashRate15m"),
    ("Performance", "ClientFpsP10"),
    ("Players", "DailyActiveUsers"),
    ("Engagement", "AverageSessionLengthMinutes"),
    ("Retention", "ForwardD1Retention"),
    ("Monetization", "DailyRevenue"),
)
STEADY = 0.03


@dataclass
class Area:
    name: str
    metric: str
    verdict: str          # better | worse | steady | no data
    vs_last: float | None
    vs_month: float | None
    detail: str = ""


@dataclass
class Weekly:
    week_end: date
    areas: list[Area]
    new: list[str] = field(default_factory=list)
    ongoing: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    false_alarms: int = 0
    change_lines: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    lifecycle: str = ""

    def text(self) -> str:
        out = [f"Weekly check-up, week to {_day(self.week_end)}:"]
        out.append("Health vs last week: " + "; ".join(
            f"{a.name} {a.verdict}" + (f" ({a.detail})" if a.detail else "") for a in self.areas))
        parts = []
        for label, items in (("new", self.new), ("still going", self.ongoing), ("fixed", self.fixed)):
            if items:
                parts.append(f"{len(items)} {label} ({', '.join(items[:3])})")
        out.append("Issues: " + ("; ".join(parts) if parts else "none this week") + "."
                   + (f" {self.false_alarms} marked false alarm." if self.false_alarms else ""))
        if self.lifecycle:
            out.append(self.lifecycle)
        if self.change_lines:
            out.append("Changes: " + " ".join(self.change_lines))
        if self.lessons:
            out.append("Learned from your changes: " + "; ".join(self.lessons) + ".")
        if self.next_steps:
            out.append("Next week: " + " ".join(f"{i}) {s}" for i, s in enumerate(self.next_steps, 1)))
        return "\n".join(out)


def _mean(pts: list[detect.Point], lo: date, hi: date) -> float | None:
    vals = [p.value for p in pts if lo <= p.day <= hi]
    return sum(vals) / len(vals) if len(vals) >= 5 else None


def _size(conn: sqlite3.Connection, uid: int) -> float | None:
    """Typical players a day over the last 4 weeks (how many people are behind the numbers)."""
    pts = detect.load_series(conn, uid, "DailyActiveUsers", "")
    if not pts:
        return None
    vals = [p.value for p in pts if p.day > pts[-1].day - timedelta(days=28)]
    return sum(vals) / len(vals) if vals else None


def _weekly_noise(metric: str, size: float | None, level: float | None) -> float:
    """Expected wobble of a 7-day average vs the previous 7-day average, as a share.

    Day-to-day wobble for games this size (public data, see priors.py), shrunk for
    averaging 7 days, and doubled so only a change well past chance counts. A game
    with 4 players a day moves 30-40% week to week by chance alone.
    """
    day = priors.noise(metric, size, level)
    return 2 * day * (2 / 7) ** 0.5 if day else 0.0


def _area(conn: sqlite3.Connection, uid: int, name: str, metric: str,
          size: float | None = None) -> Area:
    spec = catalog.get(metric)
    pts = detect.load_series(conn, uid, metric, "")
    if not pts:
        return Area(name, metric, "no data", None, None)
    end = pts[-1].day  # each metric's own latest day (retention lags a little)
    this = _mean(pts, end - timedelta(days=6), end)
    last = _mean(pts, end - timedelta(days=13), end - timedelta(days=7))
    month = _mean(pts, end - timedelta(days=34), end - timedelta(days=7))
    if this is None or last is None:
        return Area(name, metric, "no data", None, None)
    vs_last = (this - last) / abs(last) if last else 0.0
    vs_month = (this - month) / abs(month) if month else None
    floor = max(STEADY, spec.min_rel / 2, _weekly_noise(metric, size, last))
    if abs(vs_last) < floor:
        verdict = "steady"
    else:
        verdict = "better" if (vs_last > 0) == spec.higher_is_better else "worse"
    detail = ""
    if verdict != "steady":
        detail = f"{spec.label.lower()} {fmt_change(spec, this, last, vs_last)}"
        if month and vs_month is not None:
            detail += f", {fmt_change(spec, this, month, vs_month)} vs 4-week avg"
    return Area(name, metric, verdict, vs_last, vs_month, detail)


def _name(row: sqlite3.Row) -> str:
    return catalog.get(row["metric"]).label + _where(row["split"])


def build(conn: sqlite3.Connection, uid: int, latest: date) -> Weekly:
    size = _size(conn, uid)
    w = Weekly(latest, [_area(conn, uid, n, m, size) for n, m in AREAS])
    week_start = latest - timedelta(days=6)
    rows = conn.execute("SELECT * FROM findings WHERE universe_id=? AND good=0", (uid,)).fetchall()
    open_rows = []
    for r in rows:
        first, last = date.fromisoformat(r["first_day"]), date.fromisoformat(r["last_day"])
        if r["state"] == "wrong":
            if last >= week_start:
                w.false_alarms += 1
            continue
        if last >= latest - timedelta(days=1):
            (w.new if first >= week_start else w.ongoing).append(_name(r))
            if r["state"] == "open" or (r["state_until"] or "") < latest.isoformat():
                open_rows.append(r)  # not marked fixed / snoozed (or that has run out)
        elif last >= week_start - timedelta(days=3) and (r["last_shown"] or ""):
            w.fixed.append(_name(r))

    for c in changes.recent(conn, uid, week_start):
        judge_on = date.fromisoformat(c["judge_on"])
        head = f"#{c['id']} '{c['title']}' {c['class']} ({c['weight']}/21):"
        if c["outcome"]:
            word = {"good": "good", "bad": "harmful", "inconclusive": "no clear result"}[c["outcome"]]
            w.change_lines.append(f"{head} {word}, {c['outcome_note']}.")
        else:
            w.change_lines.append(f"{head} waiting, judge on {_day(judge_on)}.")

    w.lessons = changes.lessons(conn, uid)[:2]

    open_rows.sort(key=lambda r: -catalog.get(r["metric"]).weight * min(abs(r["peak_rel"] or 0), 2))
    seen: set[str] = set()
    for r in open_rows:  # one step per story: "players" and "visits" down is one thing to fix
        fam = catalog.get(r["metric"]).family
        if fam not in seen and len(w.next_steps) < 3:
            seen.add(fam)
            w.next_steps.append(f"Fix: {_name(r)}.")
    if len(w.next_steps) < 3:
        for c in changes.recent(conn, uid, week_start):
            if not c["outcome"] and len(w.next_steps) < 3:
                judge_on = date.fromisoformat(c["judge_on"])
                w.next_steps.append(f"Judge change #{c['id']} on {_day(judge_on)}.")
    life = decline.lifecycle(conn, uid)
    if life:
        w.lifecycle = life.text()
        if life.stage in ("declining", "fading") and len(w.next_steps) < 3:
            w.next_steps.append("Find out why players are leaving (roblox-agent why).")
    worse = [a for a in w.areas if a.verdict == "worse"]
    for a in worse:
        if len(w.next_steps) >= 3:
            break
        step = f"Look into {a.name.lower()} ({a.detail.split(',')[0]})."
        if step not in w.next_steps:
            w.next_steps.append(step)
    return w
