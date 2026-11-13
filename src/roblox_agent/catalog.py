"""Which metrics to pull, and how.

Names and allowed breakdowns follow
https://create.roblox.com/docs/cloud/guides/analytics/metrics (checked 2026-11-13).

"standard" metrics (players, money, retention) keep ~4 years of history on
Roblox, so we backfill a year on the first run.
"performance" metrics (crashes, FPS, memory) are deleted by Roblox after 28
days, so we must pull them every day to keep a long history.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STANDARD = "standard"
PERFORMANCE = "performance"

# Roblox keeps performance data for 28 days; stay one day inside that limit.
PERFORMANCE_LOOKBACK_DAYS = 27


@dataclass(frozen=True)
class MetricSpec:
    name: str
    kind: str
    label: str            # plain-language name shown to you
    unit: str = ""
    # Each entry is one query: [] = whole game, ["Platform"] = split by device, etc.
    breakdowns: tuple[tuple[str, ...], ...] = field(default=((),))
    dashboard_page: str = ""  # where to find the same number on the Creator Dashboard
    # --- how the checks treat it ---
    family: str = "other"         # findings in one family usually tell one story
    higher_is_better: bool = True
    weight: float = 1.0           # how much it matters when ranking (crashes, retention, money first)
    rate: bool = False            # a share (0-1). Shown as %, changes in points
    count: bool = False           # small whole numbers (crashes): judged by absolute size too
    min_rel: float = 0.10         # smallest change worth mentioning, as a share of normal
    min_abs: float = 0.0          # ...and in the metric's own units (rates: share, e.g. 0.002)
    percent_above: float = 1.0    # rates: a typical value above this means Roblox sent percents
    context: bool = False         # pulled to explain other findings, not checked on its own


DEFAULT_METRICS: tuple[MetricSpec, ...] = (
    # --- Engagement ---
    MetricSpec("DailyActiveUsers", STANDARD, "Daily active users", "players",
               ((), ("Platform",), ("IsNewUser",)), "Analytics > Engagement",
               family="players", weight=2.0, min_abs=5),
    MetricSpec("Visits", STANDARD, "Visits", "visits",
               ((),), "Analytics > Engagement",
               family="players", weight=1.0, min_rel=0.15, min_abs=10),
    MetricSpec("AverageSessionLengthMinutes", STANDARD, "Average session length", "min",
               ((), ("Platform",)), "Analytics > Engagement",
               family="engagement", weight=1.5, min_abs=0.5),
    # --- Retention ---
    MetricSpec("ForwardD1Retention", STANDARD, "Day 1 retention", "%",
               ((), ("Platform",)), "Analytics > Retention",
               family="retention", weight=3.0, rate=True, min_rel=0.08, min_abs=0.005),
    MetricSpec("ForwardD7Retention", STANDARD, "Day 7 retention", "%",
               ((),), "Analytics > Retention",
               family="retention", weight=2.5, rate=True, min_rel=0.10, min_abs=0.003),
    MetricSpec("ForwardD30Retention", STANDARD, "Day 30 retention", "%",
               ((),), "Analytics > Retention",
               family="retention", weight=2.0, rate=True, min_rel=0.12, min_abs=0.002, percent_above=0.6,
               context=True),  # few players behind it each day: too jumpy to alarm on, useful as a clue
    # --- Longevity and discovery (used to explain why player numbers move) ---
    MetricSpec("MonthlyActiveUsers", STANDARD, "Monthly active users", "players",
               ((),), "Analytics > Engagement", family="players", context=True),
    MetricSpec("DauMauStickiness", STANDARD, "Stickiness (daily / monthly players)", "%",
               ((),), "Analytics > Retention", family="engagement", rate=True, context=True),
    MetricSpec("UniqueUsersWithImpressions", STANDARD, "People who saw the game", "people",
               ((),), "Analytics > Acquisition",
               family="discovery", weight=1.5, min_rel=0.20, min_abs=50),
    MetricSpec("EndToEndCVR", STANDARD, "Saw-it-to-played-it rate", "%",
               ((),), "Analytics > Acquisition",
               family="discovery", weight=1.5, rate=True, min_rel=0.15, min_abs=0.001, percent_above=0.5),
    # --- Monetization ---
    MetricSpec("DailyRevenue", STANDARD, "Daily revenue", "Robux",
               ((),), "Analytics > Monetization",
               family="money", weight=2.0, min_rel=0.20, min_abs=50),
    MetricSpec("PayingUsersCVR", STANDARD, "Paying user conversion", "%",
               ((),), "Analytics > Monetization",
               family="money", weight=1.5, rate=True, min_rel=0.15, min_abs=0.001,
               percent_above=0.3),
    # --- Performance (28-day history on Roblox) ---
    MetricSpec("ClientCrashRate15m", PERFORMANCE, "Crash rate", "%",
               ((), ("Platform",)), "Analytics > Performance",
               family="stability", higher_is_better=False, weight=3.0, rate=True, min_rel=0.25,
               min_abs=0.002, percent_above=0.25),
    MetricSpec("ClientFpsP10", PERFORMANCE, "Frame rate (worst 10%)", "fps",
               ((), ("Platform",)), "Analytics > Performance",
               family="performance", weight=1.5, min_rel=0.08, min_abs=1.5),
    MetricSpec("ClientMemoryUsageP90", PERFORMANCE, "Memory use (top 10%)", "MB",
               ((),), "Analytics > Performance",
               family="performance", higher_is_better=False, weight=1.5, min_rel=0.10, min_abs=50),
    MetricSpec("OomUnexpectedExits", PERFORMANCE, "Out-of-memory exits", "exits",
               ((),), "Analytics > Performance",
               family="stability", higher_is_better=False, weight=2.0, count=True, min_rel=0.5, min_abs=3),
    MetricSpec("PeakConcurrentPlayers", PERFORMANCE, "Peak concurrent players", "players",
               ((),), "Analytics > Performance",
               family="players", weight=1.0, min_rel=0.15, min_abs=3),
    MetricSpec("ServerFrameRateAvg", PERFORMANCE, "Server frame rate", "fps",
               ((),), "Analytics > Performance",
               family="performance", weight=1.5, min_rel=0.08, min_abs=3),
    MetricSpec("ServerCrashCount", PERFORMANCE, "Server crashes", "crashes",
               ((),), "Analytics > Performance",
               family="stability", higher_is_better=False, weight=2.0, count=True, min_rel=0.5, min_abs=3),
)

BY_NAME = {m.name: m for m in DEFAULT_METRICS}


def get(name: str) -> MetricSpec:
    """Look up a known metric, or treat an unknown name as a plain standard metric."""
    return BY_NAME.get(name) or MetricSpec(name, STANDARD, name)


# Families, in the order the weekly scorecard lists them.
FAMILIES = {
    "stability": "Stability", "performance": "Performance", "players": "Players",
    "engagement": "Engagement", "retention": "Retention", "money": "Monetization", "discovery": "Discovery",
}
