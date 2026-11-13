"""Changes you ship, scored with the change-weight rubric, and judged later.

The rubric (plan Part 4d) scores how far a gameplay/system change spreads,
on 7 factors from 0 to 3. The total (0-21) sets how long to wait before
judging it and how carefully to roll it out:

    0-5   Light    judge after 3 days
    6-10  Medium   judge after 7 days (an A/B test is better)
    11-15 Heavy    judge after 14 days, small group first
    16-21 Major    judge after 21 days, staged rollout, one at a time

A 3 in "Paid items" or "Undo" makes a change at least Heavy.

Judging compares the days after the change with the 14 days before it,
using whole weeks so busy weekends can't fake a result. This is a
before/after comparison, not an A/B test: something else that happened at
the same time (a holiday, a YouTuber) can still be the real cause, so the
verdict says when another change overlaps.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median

from . import catalog, db, detect
from .stats import welch_z

FACTORS: tuple[tuple[str, str, tuple[str, str, str, str]], ...] = (
    ("reach", "Reach: how many players touch it",
     ("rare edge case", "some players", "most players", "everyone, every session")),
    ("systems", "Systems it changes directly",
     ("cosmetic only", "1 system", "2-3 systems", "4 or more systems")),
    ("spread", "Knock-on spread to other systems",
     ("none", "1 step away", "2 steps away", "touches the core loop or main currency")),
    ("economy", "Economy (where currency comes from / goes)",
     ("no change", "small tuning (<10%)", "big tuning or a new source/sink", "new currency, core prices, trading")),
    ("paid", "Paid items",
     ("not related", "indirect", "changes the value of things people buy", "changes or nerfs bought items")),
    ("progress", "Player progress",
     ("not related", "speeds or slows a little", "changes pacing of a main path", "resets or removes progress")),
    ("undo", "Undo",
     ("behind a switch, instant rollback", "easy to revert", "revert is noticeable or messy",
      "can't really be undone")),
)
FACTOR_KEYS = tuple(k for k, _, _ in FACTORS)
CLASSES = ((5, "Light", 3), (10, "Medium", 7), (15, "Heavy", 14), (21, "Major", 21))
WATCH = ("DailyActiveUsers", "AverageSessionLengthMinutes", "ForwardD1Retention", "ForwardD7Retention",
         "DailyRevenue", "PayingUsersCVR", "ClientCrashRate15m", "ClientFpsP10")
BEFORE_DAYS = 14


def weigh(scores: dict[str, int]) -> tuple[int, str, int]:
    """Return (weight 0-21, class, days to wait before judging)."""
    missing = [k for k in FACTOR_KEYS if k not in scores]
    if missing:
        raise ValueError("Missing scores for: " + ", ".join(missing))
    vals = {k: int(scores[k]) for k in FACTOR_KEYS}
    if any(v < 0 or v > 3 for v in vals.values()):
        raise ValueError("Each score must be 0, 1, 2 or 3.")
    weight = sum(vals.values())
    idx = next(i for i, (top, _, _) in enumerate(CLASSES) if weight <= top)
    if (vals["paid"] == 3 or vals["undo"] == 3) and idx < 2:
        idx = 2  # one-way doors are at least Heavy
    _, name, days = CLASSES[idx]
    return weight, name, days


def log(conn: sqlite3.Connection, uid: int, title: str, scores: dict[str, int],
        shipped_on: date | None = None, target: str | None = None) -> str:
    title = " ".join(str(title).split())[:120]
    if not title:
        raise ValueError("Give the change a short name.")
    if target and target not in catalog.BY_NAME:
        raise ValueError(f"Unknown metric '{target}'.")
    weight, cls, days = weigh(scores)
    shipped_on = shipped_on or datetime.now(timezone.utc).date()
    judge_on = shipped_on + timedelta(days=days)
    cur = conn.execute(
        "INSERT INTO changes (universe_id, title, shipped_on, scores, weight, class, judge_on, target, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, title, shipped_on.isoformat(), json.dumps({k: int(scores[k]) for k in FACTOR_KEYS}), weight, cls,
         judge_on.isoformat(), target, db.utcnow()))
    conn.commit()
    advice = {"Light": "Ship normally.", "Medium": "An A/B test is better if you can.",
              "Heavy": "Try it on a small group first and watch linked systems.",
              "Major": "Roll it out in stages, tell players, and ship it on its own."}[cls]
    return (f"Logged change #{cur.lastrowid} '{title}': {cls} ({weight}/21). I'll judge it on "
            f"{judge_on:%b} {judge_on.day}. {advice}")


@dataclass
class MetricResult:
    metric: str
    rel: float
    z: float
    verdict: str   # better | worse | same | unknown
    text: str


def _compare(conn: sqlite3.Connection, uid: int, metric: str, shipped: date, days: int) -> MetricResult:
    spec = catalog.get(metric)
    pts = detect.load_series(conn, uid, metric, "")
    hist = [p for p in pts if shipped - timedelta(days=56) <= p.day < shipped]
    after = [p for p in pts if shipped <= p.day < shipped + timedelta(days=max(days, 7))]
    before = [p for p in hist if p.day >= shipped - timedelta(days=BEFORE_DAYS)]
    if len(before) < 7 or len(after) < 3:
        return MetricResult(metric, 0.0, 0.0, "unknown", f"{spec.label}: not enough data")
    f, g = detect._transform(spec, [p.value for p in hist + after])
    week = detect._weekday_effects([p.day for p in hist], [f(p.value) for p in hist])
    b = [f(p.value) - week.get(p.day.weekday(), 0.0) for p in before]
    a = [f(p.value) - week.get(p.day.weekday(), 0.0) for p in after]
    z = welch_z(b, a)
    mb, ma = g(median(b)), g(median(a))
    rel = (ma - mb) / abs(mb) if mb else 0.0
    big_enough = abs(rel) >= spec.min_rel / 2 and abs(ma - mb) >= spec.min_abs / 2
    if abs(z) >= 2.5 and big_enough:
        verdict = "better" if (rel > 0) == spec.higher_is_better else "worse"
    else:
        verdict = "same"
    from .checkup import fmt_change
    return MetricResult(metric, rel, z, verdict, f"{spec.label} {fmt_change(spec, ma, mb, rel)}")


def judge(conn: sqlite3.Connection, uid: int, row: sqlite3.Row) -> tuple[str, str]:
    """Return (outcome, short explanation) for a change that is due."""
    shipped = date.fromisoformat(row["shipped_on"])
    days = (date.fromisoformat(row["judge_on"]) - shipped).days
    metrics = list(WATCH)
    if row["target"] and row["target"] not in metrics:
        metrics.insert(0, row["target"])
    results = {m: _compare(conn, uid, m, shipped, days) for m in metrics}
    important_worse = [r for r in results.values() if r.verdict == "worse" and catalog.get(r.metric).weight >= 2]
    better = [r for r in results.values() if r.verdict == "better"]
    target = results.get(row["target"]) if row["target"] else None
    if target and target.verdict == "worse" or important_worse:
        outcome = "bad"
    elif (target and target.verdict == "better") or (not target and better):
        outcome = "good"
    else:
        outcome = "inconclusive"
    moved = [r for r in results.values() if r.verdict in ("better", "worse")]
    moved.sort(key=lambda r: (r.verdict != "worse", -abs(r.z)))
    parts = [r.text + (" (worse)" if r.verdict == "worse" else "") for r in moved[:3]]
    if target and target.verdict == "same":
        parts.insert(0, f"{catalog.get(target.metric).label}: no clear change")
    explain = "; ".join(parts) if parts else "nothing moved clearly"
    lo, hi = shipped - timedelta(days=3), date.fromisoformat(row["judge_on"])
    overlaps = [r for r in conn.execute(
        "SELECT id, class FROM changes WHERE universe_id=? AND id!=? AND shipped_on BETWEEN ? AND ?",
        (uid, row["id"], lo.isoformat(), hi.isoformat()))]
    if overlaps:
        explain += ". Overlaps with " + ", ".join(f"#{o['id']} ({o['class']})" for o in overlaps)
        heavy = {"Heavy", "Major"}
        if row["class"] in heavy and any(o["class"] in heavy for o in overlaps):
            explain += ", so low confidence"
    return outcome, explain


def due_notes(conn: sqlite3.Connection, uid: int, latest: date | None, record: bool = True) -> list[str]:
    """Judge changes whose wait is over, once. Returns one short line per newly judged change."""
    if not latest:
        return []
    notes = []
    rows = conn.execute("SELECT * FROM changes WHERE universe_id=? AND announced=0 AND judge_on<=?"
                        " ORDER BY id", (uid, latest.isoformat())).fetchall()
    for row in rows:
        outcome, explain = row["outcome"], row["outcome_note"]
        if outcome is None:
            outcome, explain = judge(conn, uid, row)
        word = {"good": "looks good", "bad": "looks harmful", "inconclusive": "no clear result yet"}[outcome]
        notes.append(f"Change #{row['id']} '{row['title']}' ({row['class']}): {word}. {explain}.")
        if record:
            conn.execute("UPDATE changes SET outcome=?, outcome_note=?, announced=1 WHERE id=?",
                         (outcome, explain, row["id"]))
    if record and rows:
        conn.commit()
    return notes


def recent(conn: sqlite3.Connection, uid: int, since: date) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM changes WHERE universe_id=? AND (judge_on>=? OR outcome IS NULL)"
                        " ORDER BY id", (uid, since.isoformat())).fetchall()


def questions() -> str:
    """The rubric as text, for people and agents."""
    lines = []
    for key, q, opts in FACTORS:
        lines.append(f"{key}: {q}. " + ", ".join(f"{i}={o}" for i, o in enumerate(opts)))
    return "\n".join(lines)
