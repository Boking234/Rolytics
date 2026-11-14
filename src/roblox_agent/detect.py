"""The checks: decide whether a metric is behaving unusually.

Three kinds of check, all run on your computer:

* **Sudden change** (``spike`` / ``shift``): the last few days compared with
  what was expected for *that weekday*, given the game's recent level. A
  change counts only if it is both big enough to matter and well outside the
  game's normal day-to-day wobble (a robust z-score, built from medians so one
  odd day can't fool it). One very unusual day is a ``spike``; 2 to 7 days
  that together are clearly off in the same direction are a ``shift`` (this
  catches a 20% drop that hides inside one day's noise but not inside a week's).
* **Slow slide** (``trend``): weekly levels over the last 12 weeks, tested with
  the Mann-Kendall trend test. Catches a retention that sinks a little every
  week, which a day-by-day check never notices.
* **Budget** (``budget``): a simple floor/ceiling that stays broken for a
  week, e.g. phones under 20 fps. Catches problems that are bad but "normal"
  for the game.

Days Roblox marks "too few players to trust" are skipped. Days marked as an
estimate ("Projected") are used, but findings that rest only on them get
lower confidence.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import NormalDist, median

from . import catalog, priors
from .stats import linear_trend, mad_sigma, mann_kendall

_ND = NormalDist()
NSS = "NotStatisticallySignificant"
PROJECTED = "Projected"

SPIKE_Z = 4.0         # one day this far outside normal wobble = sudden change
SHIFT_ZC = 4.0        # 2-7 days whose combined evidence is this strong = shift
WINDOW = 7            # most recent days examined for sudden changes
LEVEL_DAYS = 14       # recent days used to judge the game's current level
HISTORY_DAYS = 56     # days used to learn weekday pattern and normal wobble
MIN_DAYS = 10         # fewer settled days than this: no verdict for the metric
TREND_WEEKS = 12
TREND_P = 0.05
TREND_T = 3.0
TREND_MIN_REL = 0.10
FALLBACK_SIGMA = priors.FLAT  # while still learning, with no public evidence: only very big changes count
CHANCE_FACTOR = 0.75
LEARNING_Z = 4.0       # evidence needed while the game has under ~3 weeks of history
BLEND_DAYS = 28        # the game's own wobble fully replaces the public-data guess after this many days

# Rule-of-thumb limits. A metric past its limit for most of the last week is
# flagged even if that is "normal" for the game. (metric, kind, limit)
BUDGETS = (
    ("ClientFpsP10", "min", 20.0),
    ("ServerFrameRateAvg", "min", 40.0),
    ("ClientCrashRate15m", "max", 0.03),
)


@dataclass
class Point:
    day: date
    value: float
    status: str


@dataclass
class Signal:
    metric: str
    split: str           # '' = whole game, else e.g. 'Platform=Phone'
    kind: str            # spike | shift | trend | level | budget
    since: date          # first affected day
    day: date            # last affected day
    value: float         # actual (mean over affected days)
    usual: float         # expected / normal value
    rel: float           # (value - usual) / usual
    z: float             # how unusual, all affected days together (0 for budget)
    days: int            # affected days (trend: weeks)
    good: bool
    provisional: bool    # rests only on Roblox estimates
    confidence: str      # high | medium | low
    learning: bool = False
    detail: str = ""     # extra words for the headline, e.g. when a slide started

    @property
    def spec(self) -> catalog.MetricSpec:
        return catalog.get(self.metric)

    @property
    def diff(self) -> float:
        return self.value - self.usual


def load_series(conn: sqlite3.Connection, uid: int, metric: str, key: str) -> list[Point]:
    rows = conn.execute(
        "SELECT bucket_time, value, status FROM data_points WHERE universe_id=? AND metric=?"
        " AND granularity='OneDay' AND breakdown_key=? AND value IS NOT NULL ORDER BY bucket_time",
        (uid, metric, key)).fetchall()
    pts = [Point(date.fromisoformat(r["bucket_time"][:10]), float(r["value"]), r["status"] or "Valid")
           for r in rows if r["status"] != NSS]
    spec = catalog.get(metric)
    if spec.rate and pts and _sent_as_percent(conn, uid, spec, pts):
        # Roblox sent a percent (21.0) instead of a share (0.21). Accept both.
        pts = [Point(p.day, p.value / 100, p.status) for p in pts]
    return pts


def _sent_as_percent(conn: sqlite3.Connection, uid: int, spec: catalog.MetricSpec, pts: list[Point]) -> bool:
    """Decide once per metric (from the whole-game numbers) whether values are percents."""
    rows = conn.execute(
        "SELECT value FROM data_points WHERE universe_id=? AND metric=? AND granularity='OneDay'"
        " AND breakdown_key='' AND value IS NOT NULL ORDER BY bucket_time DESC LIMIT 28",
        (uid, spec.name)).fetchall()
    vals = [r[0] for r in rows] or [p.value for p in pts]
    return median(vals) > spec.percent_above


def _transform(spec: catalog.MetricSpec, values: list[float]) -> tuple[Callable[[float], float],
                                                                          Callable[[float], float]]:
    """Pick a scale where 'normal wobble' is about the same size at any level."""
    if spec.count:
        return (lambda v: math.sqrt(max(v, 0) + 0.375)), (lambda y: max(y * y - 0.375, 0.0))
    if values and min(values) <= 0:
        # Zeros happen when few players are behind a rate (0 crashes today on
        # Console). The square root keeps such days from looking extreme.
        return (lambda v: math.sqrt(max(v, 0.0))), (lambda y: max(y, 0.0) ** 2)
    return math.log, math.exp


def _weekday_effects(days: list[date], ys: list[float]) -> dict[int, float]:
    """How much higher/lower each weekday usually is, vs the surrounding week."""
    if len(days) < 21:
        return {}
    by_day = dict(zip(days, ys))
    devs: dict[int, list[float]] = {}
    for d, y in by_day.items():
        around = [by_day[d + timedelta(days=k)] for k in range(-3, 4) if d + timedelta(days=k) in by_day]
        if len(around) >= 5:
            devs.setdefault(d.weekday(), []).append(y - median(around))
    eff = {k: median(v) for k, v in devs.items() if len(v) >= 3}
    if len(eff) < 7:
        return {}
    mean = sum(eff.values()) / 7
    eff = {k: v - mean for k, v in eff.items()}
    # Shrink toward "no weekday pattern" when the pattern is mostly noise, so
    # a noisy small game isn't given a made-up weekly rhythm.
    noise = sum(mad_sigma(v) ** 2 * 1.57 / len(v) for v in devs.values()) / 7  # var of a median
    signal = sum(v * v for v in eff.values()) / 7
    shrink = max(0.0, 1 - noise / signal) if signal > 0 else 0.0
    return {k: v * shrink for k, v in eff.items()}


def _poisson_z(x: float, lam: float, disp: float) -> float:
    """How surprising a count is, as a z-score, using the Poisson tail (scaled for extra spread)."""
    x, lam = x / disp, max(lam / disp, 1e-9)
    # P(X <= k) for Poisson(lam)
    def cdf(k: int) -> float:
        if k < 0:
            return 0.0
        term = total = math.exp(-lam)
        for i in range(1, k + 1):
            term *= lam / i
            total += term
        return min(total, 1.0)
    k = math.floor(x)
    if x >= lam:
        tail = 1 - cdf(k - 1) - 0.5 * (cdf(k) - cdf(k - 1))  # mid-p: P(X > x) + P(X = x) / 2
        return min(-_ND.inv_cdf(max(tail, 1e-15)), 8.0) if tail < 0.5 else 0.0
    tail = cdf(k - 1) + 0.5 * (cdf(k) - cdf(k - 1))
    return max(_ND.inv_cdf(max(tail, 1e-15)), -8.0) if tail < 0.5 else 0.0


def _baseline(spec: catalog.MetricSpec, pts: list[Point], window: list[Point],
              exposure: dict[date, float] | None, size: float | None = None):
    """Expected values for the window days, and the size of normal wobble."""
    start = window[0].day
    hist = [p for p in pts if start - timedelta(days=HISTORY_DAYS) <= p.day < start]
    if len(hist) < 7:
        return None
    ref = median(exposure[p.day] for p in hist if p.day in exposure) if exposure and any(
        p.day in exposure for p in hist) else None

    def adjust(p: Point) -> float:  # counts "as if player numbers were normal"
        if ref and exposure and exposure.get(p.day):
            return p.value * ref / exposure[p.day]
        return p.value

    f, g = _transform(spec, [adjust(p) for p in pts])
    hdays = [p.day for p in hist]
    hy = [f(adjust(p)) for p in hist]
    week = _weekday_effects(hdays, hy)
    z_h = [y - week.get(d.weekday(), 0.0) for d, y in zip(hdays, hy)]
    level = median(z_h[-LEVEL_DAYS:])
    # Normal wobble: how far each past day was from the level of the 14 days before it.
    resid = []
    for i in range(7, len(z_h)):
        prior = [z for d, z in zip(hdays[:i], z_h[:i]) if d >= hdays[i] - timedelta(days=LEVEL_DAYS)]
        if len(prior) >= 7:
            resid.append(z_h[i] - median(prior))
    learning = len(resid) < 10
    # Starting guess for the wobble: what public data says about games this
    # size (see priors.py), or a flat 15% when there is no public evidence.
    guess = priors.noise(spec.name, size, g(level) if f is math.log else None)
    informed = guess is not None and f is math.log
    rel_guess = guess if informed else priors.FLAT_BY_METRIC.get(spec.name, FALLBACK_SIGMA)
    if spec.rate and size:
        # A share of few players is jumpy by chance alone (3 buyers or 5 is luck, not news).
        share = min(max(g(level), 1e-6), 0.99)
        people = size * (priors.NEW_PLAYER_SHARE if "Retention" in spec.name else 1.0)
        rel_guess = max(rel_guess, CHANCE_FACTOR * math.sqrt((1 - share) / (share * people)))
    if informed and not week:
        # The weekday rhythm isn't known yet (needs 3 weeks), so it counts as wobble for now.
        rel_guess = math.hypot(rel_guess, priors.weekday_allowance(spec.name))
    start_sigma = rel_guess * (1.0 if f is math.log else max(abs(level), 1e-9))
    if learning:
        sigma = start_sigma
    else:
        sigma = mad_sigma(resid) * math.sqrt(1 + 1 / len(z_h[-LEVEL_DAYS:]))  # level is an estimate too
        if informed and len(resid) < BLEND_DAYS:
            # Adapt: lean on the public-data guess at first, on the game's own history more every day.
            w = 1 - len(resid) / BLEND_DAYS
            sigma = math.sqrt(w * start_sigma ** 2 + (1 - w) * sigma ** 2)
    # Never assume less wobble than 1% (flat series such as a server capped at 60 fps).
    floor = 0.08 if spec.count else (0.01 if f is math.log else 0.005 * abs(level))
    sigma = max(sigma, floor, 1e-9)
    expect, zs = [], []
    raw = [adjust(p) for p in hist[-LEVEL_DAYS:]]
    mean = sum(raw) / len(raw)
    disp = max(1.0, (sum((v - mean) ** 2 for v in raw) / (len(raw) - 1)) / mean) if mean > 0 else 1.0
    for p in window:
        y_hat = level + week.get(p.day.weekday(), 0.0)
        scale = (exposure[p.day] / ref) if ref and exposure and exposure.get(p.day) else 1.0
        expect.append(g(y_hat) * scale)
        if spec.count and not learning:
            zs.append(_poisson_z(p.value, g(y_hat) * scale, disp))
        else:
            zs.append((f(adjust(p)) - y_hat) / sigma)
    return expect, zs, learning, informed


def sudden(spec: catalog.MetricSpec, pts: list[Point], split: str = "",
           exposure: dict[date, float] | None = None, force_days: int | None = None,
           size: float | None = None, strict: float = 0.0) -> Signal | None:
    """Look for a spike or shift in the last few days.

    ``exposure``: daily players, for counts that grow with player numbers
    (more players = more out-of-memory exits, which is not a problem).
    ``force_days``: skip the tests and just measure the change over the last
    N days (used to say which device a whole-game change mostly comes from).
    ``size``: typical players a day behind this number (for the public-data
    starting guess of normal wobble). ``strict``: extra evidence required,
    from the sensitivity setting and from past "false alarm" marks.
    """
    if len(pts) < MIN_DAYS:
        return None
    window = pts[-WINDOW:]
    base = _baseline(spec, pts, window, exposure, size)
    if base is None:
        return None
    expect, zs, learning, informed = base
    spike_z, shift_zc = SPIKE_Z + strict, SHIFT_ZC + strict
    sign = 1 if zs[-1] > 0 else -1

    if force_days:
        n = min(force_days, len(window))
        best_zc = sum(z * sign for z in zs[-n:]) / math.sqrt(n)
    else:
        # Which days are affected? Try the last 1..7 days and keep the stretch
        # with the strongest combined evidence (Stouffer: sum of z / sqrt(days)).
        # A stretch must still be going on today and mostly point one way.
        best_zc, n = 0.0, 0
        for k in range(1, len(zs) + 1):
            seg = [z * sign for z in zs[-k:]]
            if min(seg) < -1.0:
                break  # a clearly opposite day: the stretch can't reach further back
            if k == 1:
                if seg[0] >= spike_z:
                    best_zc, n = seg[0], 1
                continue
            if seg[-1] < 1.0 or sum(1 for z in seg if z >= 1.0) < 0.6 * k:
                continue
            zc = sum(seg) / math.sqrt(k)
            if zc >= shift_zc and zc > best_zc:
                best_zc, n = zc, k
        if not n:
            return None
    kind = "spike" if n == 1 else "shift"
    zc = best_zc
    if learning and not force_days and zc < LEARNING_Z + strict:
        return None  # first weeks: only very big changes
    aff = window[-n:]
    value = sum(p.value for p in aff) / n
    usual = sum(expect[-n:]) / n
    if usual == 0:
        rel = 1.0 if value > 0 else 0.0
    else:
        rel = (value - usual) / abs(usual)
    if not force_days and (abs(rel) < spec.min_rel or abs(value - usual) < spec.min_abs):
        return None  # real, but too small to be worth your time
    provisional = all(p.status == PROJECTED for p in aff)
    conf = "high" if zc >= 5.5 + strict else "medium" if zc >= 4 + strict else "low"
    if learning:
        # With a public-data guess for this game's size, a very clear change can
        # be trusted a little even in the first weeks. Without one, it can't.
        conf = "medium" if informed and zc >= 5 + strict else "low"
    elif provisional and n == 1 and zc < 7:  # one estimated day alone: be careful
        conf = {"high": "medium", "medium": "low", "low": "low"}[conf]
    good = (rel > 0) == spec.higher_is_better
    return Signal(spec.name, split, kind, aff[0].day, aff[-1].day, value, usual, rel,
                  zc * sign, n, good, provisional, conf, learning)


def trend(spec: catalog.MetricSpec, pts: list[Point], split: str = "") -> Signal | None:
    """Look for a steady slide (or climb) over the last 8 weeks."""
    if spec.count:
        return None
    settled = [p for p in pts if p.status != PROJECTED]
    if len(settled) < 7 * 6:
        return None
    end = settled[-1].day
    span = [p for p in settled if p.day > end - timedelta(days=7 * TREND_WEEKS)]
    f, g = _transform(spec, [p.value for p in span])
    ys = [f(p.value) for p in span]
    week = _weekday_effects([p.day for p in span], ys)
    # Weekly averages (after removing the weekday pattern). Using weeks, not
    # days, matters: neighbouring days move together, which would make a
    # day-level trend test far too eager.
    weekly: dict[int, list[float]] = {}
    for p, y in zip(span, ys):
        weekly.setdefault((end - p.day).days // 7, []).append(y - week.get(p.day.weekday(), 0.0))
    keys = sorted((k for k, v in weekly.items() if len(v) >= 4), reverse=True)  # oldest week first
    if len(keys) < 6:
        return None
    levels = [sum(weekly[k]) / len(weekly[k]) for k in keys]
    slope, t = linear_trend(levels)  # per week
    _, p = mann_kendall(levels)      # second opinion that ignores sizes: one odd week can't fake it
    if abs(t) < TREND_T or p > TREND_P:
        return None
    weeks = len(levels)
    mid = sum(levels) / weeks
    first, last = g(mid - slope * (weeks - 1) / 2), g(mid + slope * (weeks - 1) / 2)
    if first == 0:
        return None
    rel = (last - first) / abs(first)
    if abs(rel) < max(TREND_MIN_REL, spec.min_rel) or abs(last - first) < spec.min_abs:
        return None
    # The latest weeks must agree with the direction, so a recovered slide isn't reported.
    if (sum(levels[-2:]) / 2 - sum(levels[:2]) / 2) * slope <= 0:
        return None
    good = (rel > 0) == spec.higher_is_better
    return Signal(spec.name, split, "trend", end - timedelta(days=7 * weeks - 1), end, last, first, rel, t,
                  weeks, good, False, "high" if abs(t) >= 5 else "medium")


def budget(spec: catalog.MetricSpec, pts: list[Point], split: str = "",
           limits: tuple | None = None) -> Signal | None:
    """Past a rule-of-thumb limit (or one you set yourself) on most of the last 7 days."""
    rule = next(((k, lim) for m, k, lim in (limits or BUDGETS) if m == spec.name), None)
    if not rule or len(pts) < 7:
        return None
    kind, limit = rule
    last7 = pts[-7:]
    bad = [p for p in last7 if (p.value < limit if kind == "min" else p.value > limit)]
    if len(bad) < 5:
        return None
    mid = median(p.value for p in last7)
    rel = (mid - limit) / limit
    return Signal(spec.name, split, "budget", last7[0].day, last7[-1].day, mid, limit, rel, 0.0,
                  len(bad), False, False, "high")
