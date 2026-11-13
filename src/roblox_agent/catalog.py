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


DEFAULT_METRICS: tuple[MetricSpec, ...] = (
    # --- Engagement ---
    MetricSpec("DailyActiveUsers", STANDARD, "Daily active users", "players",
               ((), ("Platform",), ("IsNewUser",)), "Analytics > Engagement"),
    MetricSpec("Visits", STANDARD, "Visits", "visits",
               ((),), "Analytics > Engagement"),
    MetricSpec("AverageSessionLengthMinutes", STANDARD, "Average session length", "min",
               ((), ("Platform",)), "Analytics > Engagement"),
    # --- Retention ---
    MetricSpec("ForwardD1Retention", STANDARD, "Day 1 retention", "",
               ((), ("Platform",)), "Analytics > Retention"),
    MetricSpec("ForwardD7Retention", STANDARD, "Day 7 retention", "",
               ((),), "Analytics > Retention"),
    # --- Monetization ---
    MetricSpec("DailyRevenue", STANDARD, "Daily revenue", "Robux",
               ((),), "Analytics > Monetization"),
    MetricSpec("PayingUsersCVR", STANDARD, "Paying user conversion", "",
               ((),), "Analytics > Monetization"),
    # --- Performance (28-day history on Roblox) ---
    MetricSpec("ClientCrashRate15m", PERFORMANCE, "Client crash rate (15 min)", "",
               ((), ("Platform",)), "Analytics > Performance"),
    MetricSpec("ClientFpsP10", PERFORMANCE, "Client FPS (worst 10%)", "fps",
               ((), ("Platform",)), "Analytics > Performance"),
    MetricSpec("ClientMemoryUsageP90", PERFORMANCE, "Client memory (top 10%)", "MB",
               ((),), "Analytics > Performance"),
    MetricSpec("OomUnexpectedExits", PERFORMANCE, "Out-of-memory exits", "exits",
               ((),), "Analytics > Performance"),
    MetricSpec("PeakConcurrentPlayers", PERFORMANCE, "Peak concurrent players", "players",
               ((),), "Analytics > Performance"),
    MetricSpec("ServerFrameRateAvg", PERFORMANCE, "Server frame rate", "fps",
               ((),), "Analytics > Performance"),
    MetricSpec("ServerCrashCount", PERFORMANCE, "Server crashes", "crashes",
               ((),), "Analytics > Performance"),
)

BY_NAME = {m.name: m for m in DEFAULT_METRICS}


def get(name: str) -> MetricSpec:
    """Look up a known metric, or treat an unknown name as a plain standard metric."""
    return BY_NAME.get(name) or MetricSpec(name, STANDARD, name)
