"""Self-test: can the checks find planted problems without crying wolf?

``roblox-agent selftest`` builds pretend games on your computer (no internet,
no key), plants known problems in them (a phone-only crash spike, a broken
shop, a slow retention slide...) and checks that the daily check-up names
them. It also runs many calm pretend games and counts false alarms.

The pretend games are more realistic than the ``demo`` data: weekends are
busier, small games are noisier, rates follow player counts, retention for
the last days isn't known yet, and low-traffic splits are marked "too few
players to trust", like on Roblox.

It also checks that the right *cause* is named (a change you shipped, a shift
to phones, a wave of new players, the game being shown less), and keeps a
scorecard (``accuracy.json``) so a new version can't quietly get worse: the
tests fail if any number drops below the saved scorecard.

``roblox-agent selftest --mine`` plants the same kinds of problems into a
copy of *your own game's* history, so you can see how well the checks work on
your real numbers. Your data is never changed.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import db

SCORECARD = Path(__file__).with_name("accuracy.json")

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
                 * pop[n] * _factor(game, "PlatformShare", plat, i))
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
        # People shown the game follow discovery (new-player events, a mobile feature), not a broken
        # join or a bad update: those lose players *after* Roblox showed them the game.
        reach = sum(game.dau * share * wf * math.exp(level) * _factor(game, "PlatformShare", plat, i)
                    for plat, share in PLATFORMS.items()) * pop[n] * new_share[n]
        seen = reach / 0.02 * math.exp(rnd2.gauss(0, 0.05))
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
    cause_of: tuple[str, str] = ("", "")  # (metric, kind): the item for metric must name this cause
    changes: list[tuple[int, str, str]] = field(default_factory=list)  # (days before last day, title, rubric)


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
             ("Watch", "Act now"), cause="new", cause_of=("DailyActiveUsers", "discovery")),
    Scenario("coming_back_less", "Since 4 weeks ago players return less often (stale content)",
             [Event("ComeBack", 0.9, 28), Event("ForwardD7Retention", 0.85, 28)],
             [("DailyActiveUsers", None, True)], ("Watch", "Act now"), cause="return"),
    # ----- the right cause -----
    Scenario("update_broke_d1", "An update ships, then day 1 retention falls by a quarter (cause: the update)",
             [Event("ForwardD1Retention", 0.72, 4)], [("ForwardD1Retention", None, True)], ("Act now", "Watch"),
             cause_of=("ForwardD1Retention", "change"), changes=[(5, "New tutorial", "2,1,1,0,0,2,1")]),
    Scenario("phone_players_surge", "Phone players triple (featured on mobile): frame rate falls overall, "
             "no device got slower (cause: player mix)",
             [Event("PlatformShare", 3.0, 6, "Phone")], [("ClientFpsP10", None, True)], ("Watch",),
             cause_of=("ClientFpsP10", "mix")),
    Scenario("new_player_wave", "A wave of new players (a video), who come back less: day 1 retention dips "
             "(cause: traffic)",
             [Event("NewPlayers", 2.5, 6), Event("ForwardD1Retention", 0.7, 6)],
             [("ForwardD1Retention", None, True)], ("Act now", "Watch"), cause_of=("ForwardD1Retention", "traffic")),
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
    cause_ok: bool | None = None   # None = this scenario doesn't test a cause


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
    if sc.changes:
        from . import changes
        for days_before, title, rubric in sc.changes:
            scores = dict(zip(changes.FACTOR_KEYS, (int(x) for x in rubric.split(","))))
            changes.log(conn, 1, title, scores, end - timedelta(days=1 + days_before))
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
    cause_ok = None
    if sc.cause_of[0]:
        item = next((f for f in top if f.metric == sc.cause_of[0] and not f.good), None)
        got = getattr(getattr(item, "cause", None), "kind", "none")
        cause_ok = got == sc.cause_of[1]
        if not cause_ok:
            ok = False
            why.append(f"wrong cause for {sc.cause_of[0]}: {got}, wanted {sc.cause_of[1]}")
    if not sc.expect:
        wrong = [f for f in top if not f.good]
        if wrong:
            ok = False
            why.append(f"false alarm: {wrong[0].metric} {getattr(wrong[0], 'split', '')}".strip())
    return Outcome(sc, seed, result.status, ok, "; ".join(why), result.text().splitlines(), cause_ok)


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
    caused = [o for o in outs if o.cause_ok is not None]
    if caused:
        lines.append(f"Right cause named:      {sum(bool(o.cause_ok) for o in caused)}/{len(caused)}")
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


# ------------------------------------------------------------- scorecard ---

def scorecard(res: dict) -> dict:
    """The headline numbers, to compare one version with the next."""
    outs: list[Outcome] = res["outcomes"]
    found = [o for o in outs if o.scenario.expect]
    traps = [o for o in outs if not o.scenario.expect]
    caused = [o for o in outs if o.cause_ok is not None]
    calm = res["calm"]
    return {
        "planted_found": sum(o.passed for o in found), "planted_total": len(found),
        "calm_kept": sum(o.passed for o in traps), "calm_total": len(traps),
        "causes_right": sum(bool(o.cause_ok) for o in caused), "causes_total": len(caused),
        "sweep_alarms": sum(not o.passed for o in calm), "sweep_total": len(calm),
        "sweep_act_now": sum(o.status == "Act now" for o in calm),
    }


def saved_scorecard() -> dict | None:
    try:
        return json.loads(SCORECARD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_scorecard(card: dict, seeds: int, calm_runs: int) -> None:
    from . import __version__
    data = {"version": __version__, "seeds": seeds, "calm_runs": calm_runs, **card}
    SCORECARD.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def regressions(card: dict, saved: dict | None) -> list[str]:
    """Plain-language list of what got worse than the saved scorecard (empty = nothing)."""
    if not saved:
        return []
    out = []
    for key, total, label in (("planted_found", "planted_total", "planted problems found"),
                              ("calm_kept", "calm_total", "calm cases kept calm"),
                              ("causes_right", "causes_total", "causes named right")):
        # Only comparable when the test is at least as big as before (new scenarios can be added).
        if card[key] < saved.get(key, 0) and card[total] >= saved.get(total, 0):
            out.append(f"{label}: {card[key]}, was {saved[key]}")
    for key, label in (("sweep_alarms", "false alarms on calm days"), ("sweep_act_now", "'Act now' on calm days")):
        if card[key] > saved.get(key, 0):
            out.append(f"{label}: {card[key]}, was {saved[key]}")
    return out


def card_text(card: dict, saved: dict | None = None) -> str:
    lines = [f"Planted problems found: {card['planted_found']}/{card['planted_total']}",
             f"Calm cases kept calm:   {card['calm_kept']}/{card['calm_total']}",
             f"Right cause named:      {card['causes_right']}/{card['causes_total']}"]
    if card["sweep_total"]:
        lines.append(f"Calm-day sweep: {card['sweep_alarms']}/{card['sweep_total']} days raised any issue, "
                     f"{card['sweep_act_now']} said 'Act now'")
    if saved:
        bad = regressions(card, saved)
        lines.append(f"Compared with the saved scorecard (v{saved.get('version', '?')}): "
                     + ("worse on " + "; ".join(bad) if bad else "no worse on anything"))
    return "\n".join(lines)


# ------------------------------------------------- on your own game data ---

PLANTS = (
    ("Crashes double on every device (last 2 days)", "ClientCrashRate15m", "", 2.2, 1),
    ("Crashes triple on phones (last 2 days)", "ClientCrashRate15m", "Platform=Phone", 3.0, 1),
    ("A third of players gone (last 3 days)", "DailyActiveUsers", "", 0.65, 2),
    ("Revenue down 60% (last 3 days)", "DailyRevenue", "", 0.4, 2),
    ("Day 1 retention down a quarter (last 4 days)", "ForwardD1Retention", "", 0.72, 4),
    ("Frame rate down 30% (last 3 days)", "ClientFpsP10", "", 0.7, 2),
)


REPLAYS = (0, 7, 14, 21)   # replay the check-up as if today were 0, 7, 14 and 21 days ago


def run_on_own(conn: sqlite3.Connection, uid: int) -> str:
    """Plant problems into copies of your game's own history and see if the check-up finds them.

    Each problem is planted at several points in the past (the last day, a week
    ago, two and three weeks ago), so one lucky or unlucky day can't decide the
    result. Your database is only read; all changes happen in a copy.
    """
    from . import checkup
    days = sorted({r[0][:10] for r in conn.execute(
        "SELECT bucket_time FROM data_points WHERE universe_id=? AND metric='DailyActiveUsers' AND"
        " breakdown_key='' AND granularity='OneDay'", (uid,))})
    if not days:
        return "No data stored yet. Pull your game's data first."
    if len(days) < 35:
        return f"Needs about 5 weeks of your game's history (has {len(days)} days)."
    lines, found_all, tried_all = [], 0, 0
    for plain, metric, split, factor, last in PLANTS:
        found = tried = 0
        for back in REPLAYS:
            copy = sqlite3.connect(":memory:")
            conn.backup(copy)  # your database is only read: everything is changed in the copy
            copy.row_factory = sqlite3.Row
            end = date.fromisoformat(days[-1]) - timedelta(days=back)
            copy.execute("DELETE FROM data_points WHERE universe_id=? AND bucket_time>=?",
                         (uid, (end + timedelta(days=1)).isoformat()))
            hit = [r[0] for r in copy.execute(
                "SELECT bucket_time FROM data_points WHERE universe_id=? AND metric=? AND breakdown_key=? AND"
                " granularity='OneDay' ORDER BY bucket_time DESC", (uid, metric, split)).fetchall()]
            if len(hit) < 28:
                copy.close()
                continue
            hit = hit[:last + 1]
            marks = ",".join("?" * len(hit))
            if split:  # one device: that device, and the whole game by that device's share
                whole = 1 + (factor - 1) * _share(copy, uid, split, hit)
                copy.execute(f"UPDATE data_points SET value=value*? WHERE universe_id=? AND metric=? AND"
                             f" breakdown_key=? AND bucket_time IN ({marks})", (factor, uid, metric, split, *hit))
                copy.execute(f"UPDATE data_points SET value=value*? WHERE universe_id=? AND metric=? AND"
                             f" breakdown_key='' AND bucket_time IN ({marks})", (whole, uid, metric, *hit))
            else:  # whole game: every device moves too
                copy.execute(f"UPDATE data_points SET value=value*? WHERE universe_id=? AND metric=? AND"
                             f" bucket_time IN ({marks})", (factor, uid, metric, *hit))
            res = checkup.run(copy, uid, today=end + timedelta(days=1), record=False)
            ok = any(_matches(f, metric, split.split("=")[1] if split else None, True) for f in res.top)
            found += ok
            tried += 1
            copy.close()
        if tried:
            found_all += found
            tried_all += tried
            lines.append(f"{found}/{tried}  {plain}")
    if not tried_all:
        return "Not enough history yet in the numbers this test uses."
    return "\n".join([(f"On your own game's history: found {found_all} of {tried_all} planted problems "
                      f"(each planted at {len(REPLAYS)} points in the past)."), *lines,
                      "Your stored data was copied, not changed."])


def _share(conn: sqlite3.Connection, uid: int, split: str, days: list[str]) -> float:
    marks = ",".join("?" * len(days))
    q = ("SELECT AVG(value) FROM data_points WHERE universe_id=? AND metric='DailyActiveUsers' AND breakdown_key=?"
         f" AND bucket_time IN ({marks})")
    part = conn.execute(q, (uid, split, *days)).fetchone()[0]
    total = conn.execute(q, (uid, "", *days)).fetchone()[0]
    return min(1.0, part / total) if part and total else 0.4
