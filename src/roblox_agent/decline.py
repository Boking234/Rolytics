"""Slow declines and longevity: is the game losing players over weeks and months?

The day-by-day checks in ``detect`` compare the last week with the two weeks
before it. That is right for sudden problems, but it has a blind spot: after
a couple of weeks a drop becomes "the new normal" and vanishes from view.
The checks here look further back, the way the big analytics dashboards do:

* **4 weeks vs the 4 weeks before** (``level_change``). The Roblox Creator
  Dashboard draws the same comparison as a 28-day-offset line. Each day is
  compared with the same weekday four weeks earlier, so weekends cancel out.
* **When did it start?** (``turning_point``). Fits "flat, then a drop" and
  "flat, then a slide" to the last 12 weeks and reports the best-fitting
  start day, so you can match it against what you shipped.
* **Lifecycle** (``lifecycle``). Where the game is in its life: growing, at
  its peak, steady, declining or fading, how far below its best 4 weeks it
  is, how fast it shrinks, and when it would halve at that pace.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from . import catalog, detect
from .detect import Point, Signal
from .stats import linear_trend, mad_sigma

LEVEL_MIN_REL = 0.10      # smallest 4-week change worth an item
LEVEL_Z = 3.0
AUTOCORR = 2.0            # neighbouring days move together: count the evidence at half strength
TURN_DAYS = 84
TURN_GAIN = 0.25          # a start day must explain at least this share of the movement
NOW_DAYS = 14             # "now" = the last 2 weeks
REF_DAYS = 28             # "before" = the 4 weeks before it started


def _settled(pts: list[Point]) -> list[Point]:
    return [p for p in pts if p.status != detect.PROJECTED]


def turning_point(pts: list[Point], spec: catalog.MetricSpec | None = None) -> tuple[date, str] | None:
    """When the current drop or slide began: (day, 'step' | 'slide'), or None if unclear."""
    pts = _settled(pts)[-TURN_DAYS:]
    if len(pts) < 28:
        return None
    spec = spec or catalog.get("DailyActiveUsers")
    f, _ = detect._transform(spec, [p.value for p in pts])
    ys = [f(p.value) for p in pts]
    week = detect._weekday_effects([p.day for p in pts], ys)
    ys = [y - week.get(p.day.weekday(), 0.0) for p, y in zip(pts, ys)]
    n = len(ys)
    mean = sum(ys) / n
    sse0 = sum((y - mean) ** 2 for y in ys)
    if sse0 <= 0:
        return None
    best: tuple[float, int, str] | None = None
    # prefix sums for the two-level ("step") fit
    c1 = [0.0]
    c2 = [0.0]
    for y in ys:
        c1.append(c1[-1] + y)
        c2.append(c2[-1] + y * y)
    for t in range(7, n - 2):
        a, b = c1[t], c1[n] - c1[t]
        sse = (c2[t] - a * a / t) + (c2[n] - c2[t] - b * b / (n - t))
        if best is None or sse < best[0]:
            best = (sse, t, "step")
    for t in range(7, n - 6):  # flat until t, then a straight slide
        xs = [max(0, i - t) for i in range(n)]
        mx = sum(xs) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - mean) for x, y in zip(xs, ys)) / sxx
        sse = sum((y - mean - slope * (x - mx)) ** 2 for x, y in zip(xs, ys))
        if sse < best[0] * 0.97:  # a slide has to fit clearly better than a simple step
            best = (sse, t, "slide")
    if best is None or best[0] > (1 - TURN_GAIN) * sse0:
        return None
    return pts[best[1]].day, best[2]


def windows(pts: list[Point], spec: catalog.MetricSpec | None = None):
    """The two stretches to compare: (now_from, now_to, before_from, before_to, turning point or None).

    "Now" is the last 2 weeks. "Before" is the 4 weeks before the change
    began when a start day is clear, else the 4 weeks before "now". Both are
    whole weeks, so every weekday counts equally.
    """
    settled = _settled(pts)
    if len(settled) < NOW_DAYS + 14:
        return None
    end = settled[-1].day
    now_lo = end - timedelta(days=NOW_DAYS - 1)
    tp = turning_point(settled, spec)
    if tp and tp[0] < end - timedelta(days=70):
        tp = None
    if tp and tp[0] > now_lo:
        now_lo = max(tp[0], end - timedelta(days=6))  # very recent: the days since, at least a week
    ref_hi = min(tp[0], now_lo) - timedelta(days=1) if tp else now_lo - timedelta(days=1)
    return now_lo, end, ref_hi - timedelta(days=REF_DAYS - 1), ref_hi, tp


def _avg(pts: list[Point], lo: date, hi: date) -> float | None:
    vals = [p.value for p in pts if lo <= p.day <= hi]
    return sum(vals) / len(vals) if len(vals) >= max(4, ((hi - lo).days + 1) // 2) else None


def level_change(spec: catalog.MetricSpec, pts: list[Point], split: str = "",
                 strict: float = 0.0) -> Signal | None:
    """The last 4 weeks vs the 4 weeks before: a drop that stayed, or a slide too young for the 12-week test."""
    if spec.count:
        return None
    pts = _settled(pts)
    if len(pts) < 48:
        return None
    end = pts[-1].day
    f, _ = detect._transform(spec, [p.value for p in pts[-84:]])
    by = {p.day: p.value for p in pts}
    diffs: list[tuple[int, float]] = []  # (days before the end, change vs the same weekday 4 weeks earlier)
    now, before = [], []
    for k in range(28):
        d = end - timedelta(days=k)
        if d in by and d - timedelta(days=28) in by:
            diffs.append((k, f(by[d]) - f(by[d - timedelta(days=28)])))
            now.append(by[d])
            before.append(by[d - timedelta(days=28)])
    if len(diffs) < 22:
        return None
    vals = [v for _, v in diffs]
    mean = sum(vals) / len(vals)
    # Normal wobble from day-to-day steps (a robust measure, so the drop itself doesn't count as wobble).
    ys = [f(p.value) for p in pts[-84:]]
    week = detect._weekday_effects([p.day for p in pts[-84:]], ys)
    ys = [y - week.get(p.day.weekday(), 0.0) for p, y in zip(pts[-84:], ys)]
    sigma = mad_sigma([b - a for a, b in pairwise(ys)]) / math.sqrt(2)
    floor = 0.01 if f is math.log else 0.005 * abs(sum(ys) / len(ys))
    se = max(sigma, floor) * math.sqrt(2 / len(vals)) * AUTOCORR
    z = mean / se
    if abs(z) < LEVEL_Z + strict:
        return None
    # It must hold week by week and still be going on, not be one bad week long ago.
    weeks = [[v for k, v in diffs if k // 7 == w] for w in range(4)]
    agree = sum(1 for w in weeks if w and (sum(w) / len(w)) * mean > 0)
    latest = weeks[0]
    if agree < 3 or not latest or (sum(latest) / len(latest)) * mean <= 0:
        return None
    value, usual = sum(now) / len(now), sum(before) / len(before)
    since, detail = end - timedelta(days=27), ""
    win = windows(pts, spec)
    tp = win[4] if win else None
    if tp:
        # A clear start day: compare the last 2 weeks with the 4 weeks before it began.
        a, b = _avg(pts, win[0], win[1]), _avg(pts, win[2], win[3])
        if a is not None and b and (a - b) * mean > 0:
            value, usual, since = a, b, tp[0]
            when = f"{tp[0]:%b} {tp[0].day}"
            if tp[1] == "step":
                detail = f"{'dropped' if a < b else 'jumped'} around {when}"
            else:
                detail = f"{'sliding' if a < b else 'climbing'} since about {when}"
    if usual == 0:
        return None
    rel = (value - usual) / abs(usual)
    if abs(rel) < max(LEVEL_MIN_REL, spec.min_rel) or abs(value - usual) < spec.min_abs:
        return None
    # If it is turning back already (last week much closer to the old level), let it go.
    last_week = [by[end - timedelta(days=k)] for k in range(7) if end - timedelta(days=k) in by]
    if last_week and abs(sum(last_week) / len(last_week) - usual) < 0.4 * abs(value - usual):
        return None
    good = (rel > 0) == spec.higher_is_better
    conf = "high" if abs(z) >= 5 + strict else "medium"
    return Signal(spec.name, split, "level", since, end, value, usual, rel, z, 4, good, False, conf,
                  detail=detail)


# ------------------------------------------------------------ lifecycle ---

STAGES = {
    "new": "new (still building history)",
    "growing": "growing",
    "peak": "at its peak",
    "steady": "steady",
    "recovering": "recovering",
    "declining": "declining",
    "fading": "fading",
}


@dataclass
class Lifecycle:
    stage: str
    now: float                    # players a day, 4-week average
    peak: float                   # best 4-week average ever stored
    peak_day: date
    vs_prev: float | None         # 4 weeks vs the 4 before
    weekly_rate: float | None     # share per week over the last 8 weeks, when it's a clear trend
    history_days: int
    end: date

    @property
    def halving_weeks(self) -> float | None:
        if self.weekly_rate is None or self.weekly_rate >= -0.005:
            return None
        return math.log(0.5) / math.log(1 + self.weekly_rate)

    def text(self) -> str:
        out = f"Lifecycle: {STAGES[self.stage]}. {self.now:,.0f} players a day (4-week average)"
        if self.stage == "new":
            return out + f", {self.history_days} days of history."
        share = self.now / self.peak if self.peak else 1.0
        if share < 0.95:
            out += f", {share * 100:.0f}% of the best 4 weeks ({self.peak:,.0f} around {self.peak_day:%b %Y})"
        if self.vs_prev is not None and abs(self.vs_prev) >= 0.03:
            out += f", {self.vs_prev * 100:+.0f}% vs the 4 weeks before"
        out += "."
        if self.weekly_rate is not None and abs(self.weekly_rate) >= 0.005:
            word = "Losing" if self.weekly_rate < 0 else "Gaining"
            out += f" {word} about {abs(self.weekly_rate) * 100:.1f}% a week over the last 8 weeks"
            half = self.halving_weeks
            if half:
                when = self.end + timedelta(weeks=round(half))
                out += f"; at this pace players halve in about {half:.0f} weeks ({when:%b %Y})"
            out += "."
        return out


def lifecycle(conn: sqlite3.Connection, uid: int) -> Lifecycle | None:
    pts = _settled(detect.load_series(conn, uid, "DailyActiveUsers", ""))
    if len(pts) < 14:
        return None
    end = pts[-1].day
    vals = [p.value for p in pts]

    def avg(i: int, days: int = 28) -> float:  # mean of the ``days`` values ending at index i
        seg = vals[max(0, i - days + 1): i + 1]
        return sum(seg) / len(seg)

    n = len(vals)
    now = avg(n - 1)
    if n < 56:
        return Lifecycle("new", now, now, end, None, None, n, end)
    best_i = max(range(27, n), key=avg)
    peak = avg(best_i)
    prev = avg(n - 29)
    vs_prev = (now - prev) / prev if prev else None
    # Pace: straight line through the last 8 whole weeks (log scale = % a week).
    weeks = [sum(vals[n - 7 * (k + 1): n - 7 * k]) / 7 for k in range(min(8, n // 7))][::-1]
    rate = None
    if len(weeks) >= 6 and min(weeks) > 0:
        slope, t = linear_trend([math.log(w) for w in weeks])
        if abs(t) >= 3:
            rate = math.exp(slope) - 1
    share = now / peak if peak else 1.0
    sig = level_change(catalog.get("DailyActiveUsers"), pts)
    falling = ((vs_prev is not None and vs_prev <= -0.10) or (rate is not None and rate <= -0.015)
               or (sig is not None and sig.rel < 0))
    rising = not falling and ((vs_prev is not None and vs_prev >= 0.10) or (rate is not None and rate >= 0.015)
                              or (sig is not None and sig.rel > 0))
    if falling:
        stage = "fading" if share < 0.5 and (end - pts[best_i].day).days > 84 else "declining"
    elif rising:
        stage = "growing" if share >= 0.8 else "recovering"
    else:
        stage = "peak" if share >= 0.95 else "steady"
    return Lifecycle(stage, now, peak, pts[best_i].day, vs_prev, rate, n, end)
