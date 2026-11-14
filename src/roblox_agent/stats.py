"""Small, dependency-free statistics helpers used by the checks.

Everything here is "robust": medians instead of averages, so one weird day
(an outage, a Roblox data glitch) can't drag the idea of "normal" around.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import median

MAD_SCALE = 1.4826  # makes the median absolute deviation comparable to a standard deviation


def mad_sigma(values: Sequence[float]) -> float:
    """Robust spread: how much a typical value wanders from the middle."""
    if len(values) < 2:
        return 0.0
    mid = median(values)
    return MAD_SCALE * median(abs(v - mid) for v in values)


def normal_sf(z: float) -> float:
    """Chance that a standard normal value is above z."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def mann_kendall(values: Sequence[float]) -> tuple[float, float]:
    """Trend test. Returns (z, two-sided p). Positive z = going up.

    Counts how many later values are above vs below earlier ones, so it only
    cares about direction, not size, and one odd week can't fake a trend.
    """
    n = len(values)
    if n < 4:
        return 0.0, 1.0
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            d = values[j] - values[i]
            s += (d > 0) - (d < 0)
    # tie correction
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    ties = sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)
    var = (n * (n - 1) * (2 * n + 5) - ties) / 18
    if var <= 0:
        return 0.0, 1.0
    if s > 0:
        z = (s - 1) / math.sqrt(var)
    elif s < 0:
        z = (s + 1) / math.sqrt(var)
    else:
        z = 0.0
    return z, 2 * normal_sf(abs(z))


def theil_sen(values: Sequence[float]) -> float:
    """Robust slope per step: the median of all pairwise slopes."""
    n = len(values)
    slopes = [(values[j] - values[i]) / (j - i) for i in range(n - 1) for j in range(i + 1, n)]
    return median(slopes) if slopes else 0.0


def welch_z(a: Sequence[float], b: Sequence[float]) -> float:
    """How many standard errors mean(b) is above mean(a)."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return 0.0 if ma == mb else math.copysign(99.0, mb - ma)
    return (mb - ma) / se


def linear_trend(values: Sequence[float]) -> tuple[float, float]:
    """Straight-line fit over equally spaced points. Returns (slope per step, t-statistic)."""
    n = len(values)
    if n < 3:
        return 0.0, 0.0
    mx, my = (n - 1) / 2, sum(values) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    slope = sum((i - mx) * (v - my) for i, v in enumerate(values)) / sxx
    res = [v - (my + slope * (i - mx)) for i, v in enumerate(values)]
    s2 = sum(r * r for r in res) / (n - 2)
    if s2 == 0:
        return slope, 0.0 if slope == 0 else math.copysign(99.0, slope)
    return slope, slope / math.sqrt(s2 / sxx)
