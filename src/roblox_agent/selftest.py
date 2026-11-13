"""Self-test: can the checks find planted problems without crying wolf?

``roblox-agent selftest`` builds pretend games on your computer (no internet,
no key), plants known problems in them (a phone-only crash spike, a broken
shop, a slow retention slide...) and checks that the daily check-up names
them. It also runs many calm pretend games and counts false alarms.

The pretend games are more realistic than the ``demo`` data: weekends are
busier, small games are noisier, rates follow player counts, retention for
the last days isn't known yet, and low-traffic splits are marked "too few
players to trust", like on Roblox.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db

PLATFORMS = {"Computer": 0.40, "Phone": 0.42, "Tablet": 0.12, "Console": 0.06}
# Typical values per platform. Overall = player-weighted mix.
PER_PLATFORM = {
    "ClientCrashRate15m": {"Computer": 0.008, "Phone": 0.016, "Tablet": 0.014, "Console": 0.010},
    "ClientFpsP10": {"Computer": 45.0, "Phone": 26.0, "Tablet": 30.0, "Console": 50.0},
    "AverageSessionLengthMinutes": {"Computer": 17.0, "Phone": 12.0, "Tablet": 13.0, "Console": 15.0},
    "ForwardD1Retention": {"Computer": 0.23, "Phone": 0.19, "Tablet": 0.20, "Console": 0.22},
}
WEEKDAY = [0.95, 0.93, 0.95, 1.00, 1.08, 1.20, 1.15]  # Mon..Sun
NSS_BELOW = 40  # players in a split before Roblox trusts it (our guess)


@dataclass
class Event:
    """Multiply a metric (optionally on one platform) from ``start`` days before the end."""
    metric: str
    factor: float
    start: int                 # 0 = last day only, 2 = last 3 days, ...
    platform: str | None = None
    ramp_weeks: float = 0.0    # >0: a slow slide reaching ``factor`` at the end
    stop: int = -1             # >=0: event ends after this many days before the end (recovered)


@dataclass
class Game:
    dau: float = 1200
    weekend: float = 1.0       # 1 = normal weekend bump, 2 = twice as strong
    seed: int = 0
    events: list[Event] = field(default_factory=list)
    percent_units: bool = False  # Roblox sending 21 instead of 0.21 (not yet verified which)
    outlier_day: int | None = None  # one glitch day in the past (days before end)
    days: int = 120            # how much history the game has


NEW_SHARE = 0.35   # new players as a share of daily players in a steady game
COME_BACK = 0.65   # share of yesterday's players who return today (new + returning = today)


def _population(game: Game, days: int) -> tuple[list[float], list[float]]:
    """Player level and new-player share per day from the two things that drive them.

    Events on "NewPlayers" (how many arrive) and "ComeBack" (how many return)
    change players the way they do in a real game: gradually, through
        players today = new today + come-back rate x players yesterday.
    """
    if not any(e.metric in ("NewPlayers", "ComeBack") for e in game.events):
        return [1.0] * days, [NEW_SHARE] * days
    level, mult, share = 1.0, [], []
    for n in range(days):
        i = days - 1 - n
        new = NEW_SHARE * _factor(game, "NewPlayers", None, i)
        level = new + COME_BACK * _factor(game, "ComeBack", None, i) * level
        mult.append(level)
        share.append(new / level)
    return mult, share


def _factor(game: Game, metric: str, platform: str | None, i_from_end: int) -> float:
    f = 1.0
    for e in game.events:
        if e.metric != metric or (e.platform and platform != e.platform):
            continue
        if e.stop >= 0 and i_from_end < e.stop:
            continue
        if e.ramp_weeks:
            span = e.ramp_weeks * 7
            if i_from_end <= span:
                f *= 1 + (e.factor - 1) * (1 - i_from_end / span)
        elif i_from_end <= e.start:
            f *= e.factor
    return f


def simulate(game: Game, end: date, days: int | None = None) -> dict[tuple[str, str], list[tuple[date, float, str]]]:
    """Return {(metric, split): [(day, value, status), ...]} for a pretend game."""
    days = days or game.days
    rnd = random.Random(game.seed)
    rnd2 = random.Random(game.seed + 7919)  # separate dice for the longevity and discovery numbers
    pop, new_share = _population(game, days)
    mau_window: list[float] = []
    out: dict[tuple[str, str], list[tuple[date, float, str]]] = {}

    def add(metric: str, key: str, day: date, value: float, status: str) -> None:
        out.setdefault((metric, key), []).append((day, value, status))

    level = 0.0
    for n in range(days):
        day = end - timedelta(days=days - n)
        i = days - 1 - n  # days before the last day
        level = 0.9 * level + rnd.gauss(0, 0.01)
        wf = 1 + (WEEKDAY[day.weekday()] - 1) * game.weekend
        status = "Projected" if i < 2 else "Valid"
        glitch = 0.55 if game.outlier_day == i else 1.0

        def noisy(v: float, rel: float) -> float:
            return v * math.exp(rnd.gauss(0, rel))

        def binom(p: float, n_people: float, extra: float = 0.03) -> float:
            sd = math.sqrt(max(p * (1 - p), 1e-9) / max(n_people, 1))
            return max(0.0, rnd.gauss(p, sd) * math.exp(rnd.gauss(0, extra)))

        def poisson(lam: float) -> int:
            if lam > 50:
                return max(0, round(rnd.gauss(lam, math.sqrt(lam))))
            k, p, limit = 0, 1.0, math.exp(-lam)
            while True:
                p *= rnd.random()
                if p <= limit:
                    return k
                k += 1

        dau_p: dict[str, float] = {}
        for plat, share in PLATFORMS.items():
            v = (game.dau * share * wf * math.exp(level) * _factor(game, "DailyActiveUsers", plat, i) * glitch
                 * pop[n])
            dau_p[plat] = max(0.0, noisy(v, 0.03 + 1 / math.sqrt(max(v, 1))))
        dau = sum(dau_p.values())
        nss = {p: ("NotStatisticallySignificant" if dau_p[p] < NSS_BELOW else status) for p in PLATFORMS}
        add("DailyActiveUsers", "", day, round(dau), status)
        for p in PLATFORMS:
            add("DailyActiveUsers", f"Platform={p}", day, round(dau_p[p]), nss[p])
        new = dau * new_share[n]
        add("DailyActiveUsers", "IsNewUser=true", day, round(noisy(new, 0.03)), status)
        add("DailyActiveUsers", "IsNewUser=false", day, round(noisy(dau - new, 0.03)), status)
        add("Visits", "", day, round(noisy(dau * 2.4, 0.04) * _factor(game, "Visits", None, i)), status)
        add("PeakConcurrentPlayers", "", day,
            round(noisy(dau * 0.11, 0.06 + 1 / math.sqrt(max(dau * 0.11, 1)))), status)

        # Platform-split rates and levels; overall = player-weighted mix.
        for metric, per in PER_PLATFORM.items():
            vals = {}
            for p in PLATFORMS:
                base = per[p] * _factor(game, metric, p, i)
                if metric == "ClientCrashRate15m":
                    v = binom(base, dau_p[p] * 1.5, 0.08)
                elif metric == "ForwardD1Retention":
                    v = binom(base, dau_p[p] * 0.35, 0.03)
                elif metric == "AverageSessionLengthMinutes":
                    v = noisy(base * (1.05 if day.weekday() >= 5 else 1.0), 0.04)
                else:  # FPS
                    v = noisy(base, 0.03)
                vals[p] = v
            overall = sum(vals[p] * dau_p[p] for p in PLATFORMS) / max(dau, 1)
            if metric == "ForwardD1Retention" and i < 1:
                continue  # tomorrow hasn't happened yet
            for p in PLATFORMS:
                add(metric, f"Platform={p}", day, vals[p], nss[p])
            add(metric, "", day, overall, status)

        if i >= 7:
            add("ForwardD7Retention", "", day,
                binom(0.065 * _factor(game, "ForwardD7Retention", None, i), new, 0.04), status)
        cvr = binom(0.018 * _factor(game, "PayingUsersCVR", None, i), dau, 0.05)
        add("PayingUsersCVR", "", day, cvr, status)
        add("DailyRevenue", "", day,
            round(noisy(dau * cvr * 250, 0.15) * _factor(game, "DailyRevenue", None, i)), status)
        add("ClientMemoryUsageP90", "", day,
            noisy(1500 * _factor(game, "ClientMemoryUsageP90", None, i), 0.03), status)
        add("OomUnexpectedExits", "", day,
            poisson(dau * 0.006 * _factor(game, "OomUnexpectedExits", None, i)), status)
        add("ServerFrameRateAvg", "", day,
            min(60.0, noisy(58.5 * _factor(game, "ServerFrameRateAvg", None, i), 0.01)), status)
        add("ServerCrashCount", "", day,
            poisson(max(dau / 1000, 0.2) * 0.8 * _factor(game, "ServerCrashCount", None, i)), status)

        # Longevity and discovery numbers (their own dice, so older scenarios are unchanged).
        mau_window.append(dau)
        mau = sum(mau_window[-30:]) / len(mau_window[-30:]) * 6.5
        add("MonthlyActiveUsers", "", day, round(mau), status)
        add("DauMauStickiness", "", day, dau / mau, status)
        seen = new / 0.02 * math.exp(rnd2.gauss(0, 0.05))
        add("UniqueUsersWithImpressions", "", day, round(seen), status)
        add("EndToEndCVR", "", day, max(0.0, rnd2.gauss(0.02, math.sqrt(0.02 * 0.98 / max(seen, 1)))), status)
        if i >= 30:
            p30 = 0.022 * _factor(game, "ForwardD30Retention", None, i)
            sd = math.sqrt(p30 * (1 - p30) / max(new, 1))
            add("ForwardD30Retention", "", day, max(0.0, rnd2.gauss(p30, sd)), status)

    if game.percent_units:
        for (metric, key), rows in out.items():
            if metric in ("ForwardD1Retention", "ForwardD7Retention", "ForwardD30Retention", "PayingUsersCVR",
                          "ClientCrashRate15m", "EndToEndCVR", "DauMauStickiness"):
                out[(metric, key)] = [(d, v * 100, s) for d, v, s in rows]
    return out


def load(conn: sqlite3.Connection, universe_id: int, game: Game, end: date, days: int | None = None) -> None:
    """Write a pretend game into the database, as if it had been pulled."""
    run = db.start_run(conn, universe_id)
    now = db.utcnow()
    for (metric, key), rows in simulate(game, end, days).items():
        conn.executemany(
            "INSERT OR REPLACE INTO data_points (universe_id, metric, granularity, breakdown_key, bucket_time,"
            " value, string_values, status, first_seen_at, updated_at, run_id)"
            " VALUES (?, ?, 'OneDay', ?, ?, ?, NULL, ?, ?, ?, ?)",
            [(universe_id, metric, key, f"{d.isoformat()}T00:00:00Z", v, s, now, now, run) for d, v, s in rows])
    db.finish_run(conn, run, "ok", 0)
    conn.commit()


# ------------------------------------------------------------ scenarios ---

@dataclass
class Scenario:
    name: str
    plain: str                        # what happened, in plain words
    events: list[Event] = field(default_factory=list)
    expect: list[tuple[str, str | None, bool]] = field(default_factory=list)  # (metric, platform, is_issue)
    status: tuple[str, ...] = ()      # acceptable statuses
    game: dict = field(default_factory=dict)
    cause: str = ""                   # expected answer to "why are fewer people playing": new | return


SCENARIOS: list[Scenario] = [
    Scenario("phone_crash_spike", "Crashes triple on phones after an update (last 2 days)",
             [Event("ClientCrashRate15m", 3.0, 1, "Phone")],
             [("ClientCrashRate15m", "Phone", True)], ("Act now",)),
    Scenario("crash_spike_all", "Crashes double on every device (last 2 days)",
             [Event("ClientCrashRate15m", 2.2, 1)], [("ClientCrashRate15m", None, True)], ("Act now",)),
    Scenario("players_drop", "A third of players gone for 3 days (broken join, bad update)",
             [Event("DailyActiveUsers", 0.65, 2)], [("DailyActiveUsers", None, True)], ("Act now",)),
    Scenario("shop_broken", "Revenue down 60% for 3 days while players are normal (shop bug)",
             [Event("DailyRevenue", 0.4, 2)], [("DailyRevenue", None, True)], ("Act now", "Watch")),
    Scenario("d1_drop", "Day 1 retention down by a quarter for 4 days (bad tutorial change)",
             [Event("ForwardD1Retention", 0.72, 4)], [("ForwardD1Retention", None, True)], ("Act now", "Watch")),
    Scenario("phone_fps_drop", "Frame rate on phones down 30% (heavy new map)",
             [Event("ClientFpsP10", 0.7, 2, "Phone")], [("ClientFpsP10", "Phone", True)], ("Act now", "Watch")),
    Scenario("memory_leak", "Memory up 35% and out-of-memory exits up 4x (leak)",
             [Event("ClientMemoryUsageP90", 1.35, 2), Event("OomUnexpectedExits", 4.0, 2)],
             [("ClientMemoryUsageP90", None, True)], ("Act now", "Watch")),
    Scenario("server_crashes", "Server crashes jump from about 1 a day to 10",
             [Event("ServerCrashCount", 10.0, 1)], [("ServerCrashCount", None, True)], ("Act now", "Watch")),
    Scenario("d7_slow_slide", "Day 7 retention slides about 3% a week for 8 weeks",
             [Event("ForwardD7Retention", 0.76, 0, ramp_weeks=8)], [("ForwardD7Retention", None, True)],
             ("Watch", "Act now")),
    Scenario("players_slow_slide", "Players slide about 3% a week for 7 weeks",
             [Event("DailyActiveUsers", 0.8, 0, ramp_weeks=7)], [("DailyActiveUsers", None, True)],
             ("Watch", "Act now")),
    Scenario("session_up", "Good news: sessions 30% longer (new content)",
             [Event("AverageSessionLengthMinutes", 1.3, 2)], [("AverageSessionLengthMinutes", None, False)],
             ("All good",)),
    Scenario("viral_spike", "Good news: players up 70% (a video went viral)",
             [Event("DailyActiveUsers", 1.7, 2)], [("DailyActiveUsers", None, False)], ("All good",)),
    Scenario("drop_that_stayed", "Players fell 20% three weeks ago and never came back",
             [Event("DailyActiveUsers", 0.8, 20)], [("DailyActiveUsers", None, True)], ("Watch", "Act now")),
    Scenario("fewer_new_players", "New players dry up over 5 weeks (the game is shown to fewer people)",
             [Event("NewPlayers", 0.65, 0, ramp_weeks=5)], [("DailyActiveUsers", None, True)],
             ("Watch", "Act now"), cause="new"),
    Scenario("coming_back_less", "Since 4 weeks ago players return less often (stale content)",
             [Event("ComeBack", 0.9, 28), Event("ForwardD7Retention", 0.85, 28)],
             [("DailyActiveUsers", None, True)], ("Watch", "Act now"), cause="return"),
    Scenario("young_game_crashes", "Game with only 16 days of history: crashes triple on phones",
             [Event("ClientCrashRate15m", 3.0, 1, "Phone")], [("ClientCrashRate15m", "Phone", True)],
             ("Act now", "Watch"), {"days": 16}),
    # ----- traps: nothing is wrong, the check-up should stay calm -----
    Scenario("young_game_calm", "Nothing wrong; game with only 16 days of history", [], [], ("All good",),
             {"days": 16, "dau": 400}),
    Scenario("calm", "Nothing happened", [], [], ("All good",)),
    Scenario("strong_weekends", "Nothing wrong, just very strong weekends", [], [], ("All good",),
             {"weekend": 2.2}),
    Scenario("small_game", "Nothing wrong, tiny game (about 150 players a day)", [], [], ("All good",),
             {"dau": 150}),
    Scenario("big_game", "Nothing wrong, big game (about 40,000 players a day)", [], [], ("All good",),
             {"dau": 40000}),
    Scenario("old_glitch", "Nothing wrong now; one glitch day 10 days ago", [], [], ("All good",),
             {"outlier_day": 10}),
    Scenario("fixed_last_week", "Crash spike 9 days ago, fixed 6 days ago", [Event("ClientCrashRate15m", 3.0, 9, stop=6)],
             [], ("All good",)),
    Scenario("percent_units", "Nothing wrong; Roblox sends rates as 21 instead of 0.21", [], [], ("All good",),
             {"percent_units": True}),
    Scenario("percent_units_crash", "Phone crash spike, with rates sent as percents",
             [Event("ClientCrashRate15m", 3.0, 1, "Phone")], [("ClientCrashRate15m", "Phone", True)],
             ("Act now",), {"percent_units": True}),
]


@dataclass
class Outcome:
    scenario: Scenario
    seed: int
    status: str
    passed: bool
    why: str
    lines: list[str]


def _matches(item, metric: str, platform: str | None, is_issue: bool) -> bool:
    split = getattr(item, "split", "")
    if item.metric != metric or item.good == is_issue:
        return False
    if platform is None:
        return True
    return split == f"Platform={platform}" or (split == "" and platform in getattr(item, "note", ""))


def run_scenario(sc: Scenario, seed: int, end: date, checker: Callable | None = None) -> Outcome:
    from . import checkup
    checker = checker or (lambda c, uid, today: checkup.run(c, uid, today=today, record=False))
    conn = db.connect(":memory:")
    game = Game(seed=seed, events=sc.events, **sc.game)
    load(conn, 1, game, end)
    result = checker(conn, 1, end)
    top = list(result.top)
    why = []
    ok = result.status in sc.status
    if not ok:
        why.append(f"status {result.status}, wanted {'/'.join(sc.status)}")
    for metric, platform, is_issue in sc.expect:
        if not any(_matches(f, metric, platform, is_issue) for f in top):
            ok = False
            why.append(f"missed {metric}{' on ' + platform if platform else ''}")
    if sc.cause:
        from . import why as why_mod
        w = why_mod.explain(conn, 1)
        got = w.cause if w else "nothing"
        if got != sc.cause:
            ok = False
            why_text = {"new": "fewer new players", "return": "players coming back less"}
            want = why_text[sc.cause]
            why.append(f"wrong reason: said {why_text.get(got, got)}, wanted {want}")
    if not sc.expect:
        wrong = [f for f in top if not f.good]
        if wrong:
            ok = False
            why.append(f"false alarm: {wrong[0].metric} {getattr(wrong[0], 'split', '')}".strip())
    return Outcome(sc, seed, result.status, ok, "; ".join(why), result.text().splitlines())


def run_all(seeds: int = 5, end: date | None = None, checker: Callable | None = None,
            calm_runs: int = 0) -> dict:
    """Run every scenario ``seeds`` times. Optionally also many calm games for a false-alarm rate."""
    end = end or date(2026, 11, 12)
    outcomes = [run_scenario(sc, s, end - timedelta(days=s * 3), checker)
                for sc in SCENARIOS for s in range(seeds)]
    calm = []
    for k in range(calm_runs):
        size = (150, 1200, 40000)[k % 3]
        sc = Scenario("calm_sweep", "calm", [], [], ("All good",), {"dau": size})
        calm.append(run_scenario(sc, 1000 + k, end - timedelta(days=k), checker))
    return {"outcomes": outcomes, "calm": calm}


def summary(res: dict) -> str:
    outs: list[Outcome] = res["outcomes"]
    lines = []
    by_name: dict[str, list[Outcome]] = {}
    for o in outs:
        by_name.setdefault(o.scenario.name, []).append(o)
    found = [o for o in outs if o.scenario.expect]
    traps = [o for o in outs if not o.scenario.expect]
    lines.append(f"Planted problems found: {sum(o.passed for o in found)}/{len(found)}")
    lines.append(f"Calm cases kept calm:   {sum(o.passed for o in traps)}/{len(traps)}")
    if res["calm"]:
        calm = res["calm"]
        alarms = sum(1 for o in calm if not o.passed)
        act = sum(1 for o in calm if o.status == "Act now")
        lines.append(f"Calm-day sweep: {alarms}/{len(calm)} days raised any issue, {act} said 'Act now'")
    lines.append("")
    for group in by_name.values():
        ok = sum(o.passed for o in group)
        mark = "ok  " if ok == len(group) else "MISS"
        lines.append(f"{mark} {ok}/{len(group)}  {group[0].scenario.plain}")
        for o in group:
            if not o.passed:
                lines.append(f"          seed {o.seed}: {o.why}")
    return "\n".join(lines)
