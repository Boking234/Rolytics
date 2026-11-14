"""The list of checks, and how they adapt to your game.

Each check is a small function that looks at one metric series and returns a
``Signal`` or nothing. They are registered here, so the check-up is just "run
every registered check on every series". Adding a new kind of check later
(from the community database, or your own) is one ``@register`` away, and any
check can be switched off without touching the others.

Adapting happens in three ways, all stored on your computer:

* **Its own history.** Normal wobble starts from what public data says about
  games of the same size and is replaced by the game's own history over the
  first weeks (``detect._baseline``).
* **Your verdicts.** Every "false alarm" you mark makes that metric need
  stronger evidence next time (+0.5 on the evidence bar, at most +1.5); every
  problem you confirm by marking it fixed wins half of that back.
* **Sensitivity.** One setting: ``low`` (fewer, surer items), ``normal`` or
  ``high`` (earlier warnings, more false alarms). GameAnalytics offers the
  same kind of dial for its anomaly detection (70% to 99% confidence).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import catalog, db, decline, detect
from .detect import Point, Signal

SENSITIVITY = {"low": 1.0, "normal": 0.0, "high": -0.5}  # added to the evidence bar (z-score)
WRONG_STEP = 0.5
DONE_STEP = 0.25
MAX_FEEDBACK = 1.5
FEEDBACK_DAYS = 180


@dataclass
class Context:
    """Everything a check may look at for one series."""
    spec: catalog.MetricSpec
    key: str                      # '' = whole game, else e.g. 'Platform=Phone'
    pts: list[Point]
    exposure: dict[date, float] | None = None   # daily players (for counts)
    size: float | None = None     # typical players a day behind this series
    strict: float = 0.0           # extra evidence required (sensitivity + feedback)
    limits: tuple = detect.BUDGETS


@dataclass
class Check:
    name: str
    run: Callable[[Context], Signal | None]
    about: str
    whole_game_only: bool = False


REGISTRY: dict[str, Check] = {}


def register(name: str, about: str, whole_game_only: bool = False):
    def wrap(fn: Callable[[Context], Signal | None]):
        REGISTRY[name] = Check(name, fn, about, whole_game_only)
        return fn
    return wrap


@register("sudden", "A spike or a shift in the last 7 days, vs the same weekdays before")
def _sudden(c: Context) -> Signal | None:
    return detect.sudden(c.spec, c.pts, c.key, exposure=c.exposure, size=c.size, strict=c.strict)


@register("limit", "Past a rule-of-thumb limit (or your own) on most of the last 7 days")
def _limit(c: Context) -> Signal | None:
    return detect.budget(c.spec, c.pts, c.key, limits=c.limits)


@register("slide", "A steady slide or climb over the last 12 weeks", whole_game_only=True)
def _slide(c: Context) -> Signal | None:
    sig = detect.trend(c.spec, c.pts, c.key)
    if sig:
        tp = decline.turning_point(c.pts, c.spec)
        if tp and tp[0] > sig.since + timedelta(days=10):
            sig.detail = f"it started around {tp[0]:%b} {tp[0].day}"
    return sig


@register("four_weeks", "The last 4 weeks vs the 4 weeks before (a drop that stayed)", whole_game_only=True)
def _four_weeks(c: Context) -> Signal | None:
    return decline.level_change(c.spec, c.pts, c.key, strict=c.strict)


# ----------------------------------------------------------------- tuning ---

@dataclass
class Tuning:
    sensitivity: str = "normal"
    off: tuple[str, ...] = ()
    limits: tuple = detect.BUDGETS
    feedback: dict[tuple[str, str], float] = field(default_factory=dict)

    def strict(self, metric: str, split: str) -> float:
        return SENSITIVITY.get(self.sensitivity, 0.0) + self.feedback.get((metric, split), 0.0)

    def active(self) -> list[Check]:
        return [c for c in REGISTRY.values() if c.name not in self.off]


def load(conn: sqlite3.Connection, uid: int, today: date | None = None) -> Tuning:
    t = Tuning()
    s = db.get_setting(conn, "sensitivity", "normal")
    t.sensitivity = s if s in SENSITIVITY else "normal"
    t.off = tuple(db.get_setting(conn, "checks_off", []) or [])
    own = db.get_setting(conn, "limits", {}) or {}
    merged = {m: (k, lim) for m, k, lim in detect.BUDGETS}
    for m, rule in own.items():
        if rule is None:
            merged.pop(m, None)
        else:
            merged[m] = (rule[0], float(rule[1]))
    t.limits = tuple((m, k, lim) for m, (k, lim) in merged.items())
    since = ((today or datetime.now(timezone.utc).date()) - timedelta(days=FEEDBACK_DAYS)).isoformat()
    for r in conn.execute("SELECT metric, split, action, COUNT(*) AS n FROM feedback WHERE universe_id=?"
                          " AND day>=? AND action IN ('done', 'wrong') GROUP BY metric, split, action",
                          (uid, since)):
        k = (r["metric"], r["split"])
        step = WRONG_STEP if r["action"] == "wrong" else -DONE_STEP
        t.feedback[k] = t.feedback.get(k, 0.0) + step * r["n"]
    t.feedback = {k: min(MAX_FEEDBACK, v) for k, v in t.feedback.items() if v > 0}
    return t


def describe(conn: sqlite3.Connection, uid: int) -> str:
    """Plain-language summary of how the checks are currently tuned."""
    t = load(conn, uid)
    lines = [f"Sensitivity: {t.sensitivity} (low = fewer, surer items; high = earlier warnings)."]
    lines.append("Checks: " + "; ".join(
        f"{c.name}{' (off)' if c.name in t.off else ''}" for c in REGISTRY.values()) + ".")
    lines.append("Limits: " + "; ".join(
        f"{catalog.get(m).label} {'at least' if k == 'min' else 'at most'} {lim:g}" for m, k, lim in t.limits) + ".")
    if t.feedback:
        lines.append("Needs stronger evidence now (you marked false alarms): " + ", ".join(
            catalog.get(m).label + (f" [{s}]" if s else "") for (m, s) in t.feedback) + ".")
    from . import learn
    ranked = learn.ranking(conn, uid).lines()
    lines += ranked
    if not t.feedback and not ranked:
        lines.append("Nothing learned from your clicks yet. Fixed, Snooze and Not a problem all teach it.")
    return "\n".join(lines)
