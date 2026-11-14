"""What "normal wobble" looks like for user-made games of a given size.

Learned from public data, so a brand-new game doesn't start from a blind
guess. As a game collects its own history the checks lean on that instead
(see ``detect._baseline``): the public numbers matter for the first few
weeks only.

Where the numbers come from (fitted 2026-11-13, script in ``research/``):

* 39,000 Fortnite Creative islands, daily players / minutes per player /
  peak players / Day 1 and Day 7 retention for 10 days (public dataset
  github.com/Agustin-Molina/ugc-game-analytics-data, from Epic's public API).
  Day-to-day noise after removing the platform-wide move of each day:
  players 37% for ~16 players a day, 22% at ~165, 14% at ~1,700, 10% at ~16,000.
* Cross-check on Roblox itself: 1,450 Roblox games with hourly public player
  counts on two full days (same dataset, from Roblox's public games API) gave
  6-10% day-to-day noise for games with 70 to 14,000 average concurrent
  players. Same range as Fortnite islands of similar size, so the size curve
  is used for Roblox with a floor of 6%.

Not covered by public data: crashes, frame rate, memory, revenue. Those keep
a flat starting guess until the game has its own history.

``noise(metric, size)`` returns the expected day-to-day wobble as a share of
the value (0.14 = 14%), or None when there is no public evidence.
"""

from __future__ import annotations

import math

# metric group -> (a, b, floor, cap): wobble = clamp(a * size**b)
CURVES: dict[str, tuple[float, float, float, float]] = {
    "players": (0.62, -0.19, 0.06, 0.40),
    "peak": (0.68, -0.17, 0.07, 0.45),
    "minutes": (1.00, -0.365, 0.025, 0.40),
    # retention: wobble in points (share of new players), not relative
    "d1_points": (0.11, -0.25, 0.008, 0.06),
}
GROUP = {
    "DailyActiveUsers": "players", "Visits": "players", "MonthlyActiveUsers": None,
    "UniqueUsersWithImpressions": "players",
    "PeakConcurrentPlayers": "peak",
    "AverageSessionLengthMinutes": "minutes", "AveragePlayTimeMinutesPerDAU": "minutes",
    "ForwardD1Retention": "d1_points", "ForwardD7Retention": "d1_points", "ForwardD30Retention": "d1_points",
}
RETENTION_SCALE = {"ForwardD7Retention": 0.6, "ForwardD30Retention": 0.4}
FLAT = 0.15  # no public evidence: only very big changes count until the game has history
FLAT_BY_METRIC = {"DailyRevenue": 0.35}  # money is lumpy: a few big spenders move a small game's day
NEW_PLAYER_SHARE = 0.3

# Where a value sits among user-made games of similar size (public Fortnite
# island data, so only a rough guide for Roblox). size bucket upper bound ->
# (25th, 50th, 75th percentile).
BENCH = {
    "ForwardD1Retention": ((50, (0.00, 0.05, 0.15)), (150, (0.03, 0.08, 0.16)), (500, (0.06, 0.11, 0.19)),
                           (1500, (0.08, 0.15, 0.23)), (5000, (0.10, 0.17, 0.26)), (15000, (0.12, 0.19, 0.27)),
                           (float("inf"), (0.13, 0.22, 0.30))),
    "ForwardD7Retention": ((50, (0.00, 0.00, 0.02)), (150, (0.00, 0.01, 0.05)), (500, (0.01, 0.02, 0.06)),
                           (1500, (0.01, 0.04, 0.09)), (5000, (0.02, 0.05, 0.11)), (15000, (0.03, 0.06, 0.12)),
                           (float("inf"), (0.03, 0.08, 0.15))),
}


def update(curves: dict) -> None:
    """Replace curves with better-fitted ones (e.g. later from the community database)."""
    for k, v in curves.items():
        if k in CURVES and len(v) == 4:
            CURVES[k] = tuple(float(x) for x in v)  # type: ignore[assignment]


def noise(metric: str, size: float | None, level: float | None = None) -> float | None:
    """Expected day-to-day wobble as a share of the value, or None if unknown.

    ``size``: typical players a day behind the number. ``level``: the metric's
    usual value (needed for retention, where the public data gives points).
    """
    group = GROUP.get(metric)
    if not group or not size or size <= 0:
        return None
    a, b, floor, cap = CURVES[group]
    w = min(cap, max(floor, a * size ** b))
    if group == "d1_points":
        if not level or level <= 0:
            return None
        w *= RETENTION_SCALE.get(metric, 1.0)
        # Roblox counts retention among new players only, a smaller group than "all players":
        # never assume less wobble than chance alone gives for that group (about 30% of players).
        chance = math.sqrt(level * (1 - min(level, 0.99)) / (NEW_PLAYER_SHARE * size))
        return min(0.6, max(w, chance) / level)
    return w


def weekday_allowance(metric: str) -> float:
    """Extra wobble to allow for while a game's weekday rhythm is still unknown.

    Player counts swing with the day of the week; in the public back-test the
    last day sat about twice as far from the previous week's middle as pure
    day-to-day noise explains.
    """
    return 0.10 if GROUP.get(metric) in ("players", "peak") else 0.0


def benchmark(metric: str, size: float, value: float) -> str:
    """'low', 'typical' or 'high' for games this size, or '' when unknown."""
    for top, (lo, _mid, hi) in BENCH.get(metric, ()):
        if size <= top:
            return "low" if value < lo else "high" if value > hi else "typical"
    return ""
