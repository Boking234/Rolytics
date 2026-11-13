"""Stage (b): the checks, the daily top 3, memory of past days, changes and the weekly check-up.

All offline. Pretend games come from ``roblox_agent.selftest``.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from roblox_agent import catalog, changes, checkup, cli, config, db, detect, ingest, mcp_server, selftest, weekly
from roblox_agent.selftest import Event, Game

END = date(2026, 11, 12)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(config.KEY_ENV, raising=False)


def game_db(end: date = END, **kw):
    conn = db.connect(":memory:")
    selftest.load(conn, 1, Game(**kw), end)
    return conn


# ----------------------------------------------------------- benchmark ---

def test_benchmark_finds_planted_problems_without_crying_wolf():
    """The headline promise: catches real problems, stays calm on calm days."""
    res = selftest.run_all(seeds=2, calm_runs=45)
    found = [o for o in res["outcomes"] if o.scenario.expect]
    traps = [o for o in res["outcomes"] if not o.scenario.expect]
    assert sum(o.passed for o in found) / len(found) >= 0.85, selftest.summary(res)
    assert all(o.passed for o in traps), selftest.summary(res)
    calm = res["calm"]
    assert sum(not o.passed for o in calm) / len(calm) <= 0.10, selftest.summary(res)
    assert not any(o.status == "Act now" for o in calm), selftest.summary(res)


def test_costliest_problems_are_caught_almost_always():
    """Crashes, lost players and a broken shop: at least 9 of 10 pretend games each."""
    for name in ("crash_spike_all", "phone_crash_spike", "players_drop", "shop_broken", "memory_leak"):
        sc = next(s for s in selftest.SCENARIOS if s.name == name)
        outs = [selftest.run_scenario(sc, seed, END - timedelta(days=seed)) for seed in range(10)]
        assert sum(o.passed for o in outs) >= 9, (name, [(o.seed, o.why) for o in outs if not o.passed])


def test_checkup_text_stays_tiny_in_every_scenario():
    worst = 0
    for sc in selftest.SCENARIOS:
        conn = game_db(seed=1, events=sc.events, **sc.game)
        worst = max(worst, len(checkup.run(conn, 1, today=END, record=False).text()))
    assert worst < 1500  # ~400 tokens; a 1 MB file is ~250,000


# -------------------------------------------------------------- checks ---

def test_phone_only_crash_spike_is_reported_on_phone_with_size():
    conn = game_db(seed=3, events=[Event("ClientCrashRate15m", 3.0, 1, "Phone")])
    res = checkup.run(conn, 1, today=END, record=False)
    first = res.top[0]
    assert res.status == "Act now"
    assert first.metric == "ClientCrashRate15m" and first.split == "Platform=Phone"
    assert "Whole game" in first.headline() and "players hitting crashes" in first.headline()
    assert sum(f.metric == "ClientCrashRate15m" for f in res.top) == 1  # one story, told once


def test_related_drops_are_grouped_into_one_item():
    conn = game_db(seed=2, events=[Event("DailyActiveUsers", 0.6, 2)])
    res = checkup.run(conn, 1, today=END, record=False)
    players = [f for f in res.top if f.sig.spec.family == "players"]
    assert len(players) == 1 and players[0].metric == "DailyActiveUsers"
    assert "Also:" in players[0].headline()


def test_more_players_means_more_crash_counts_is_not_a_problem():
    conn = game_db(seed=4, events=[Event("DailyActiveUsers", 1.8, 2)])
    res = checkup.run(conn, 1, today=END, record=False)
    assert res.status == "All good"
    assert not any(f.metric in ("OomUnexpectedExits", "ServerCrashCount") for f in res.top)


def test_rates_sent_as_percent_are_understood():
    a = game_db(seed=5)
    b = game_db(seed=5, percent_units=True)
    pa = detect.load_series(a, 1, "ForwardD1Retention", "")
    pb = detect.load_series(b, 1, "ForwardD1Retention", "")
    assert pa[-1].value == pytest.approx(pb[-1].value)
    assert 0.05 < pb[-1].value < 0.5
    # Small crash rates in percent (0.8 = 0.8%) on a quiet device are converted too.
    console = detect.load_series(b, 1, "ClientCrashRate15m", "Platform=Console")
    assert all(p.value < 0.2 for p in console)


def test_slow_slide_is_caught_and_explained():
    conn = game_db(seed=0, events=[Event("DailyActiveUsers", 0.75, 0, ramp_weeks=8)])
    res = checkup.run(conn, 1, today=END, record=False)
    f = next(f for f in res.top if f.metric == "DailyActiveUsers")
    assert f.sig.kind == "trend" and "sliding for" in f.headline()


def test_budget_flags_a_game_that_is_always_slow_on_phones():
    conn = game_db(seed=1)
    conn.execute("UPDATE data_points SET value = 14 WHERE metric='ClientFpsP10' AND breakdown_key='Platform=Phone'")
    res = checkup.run(conn, 1, today=END, record=False)
    f = next(f for f in res.top if f.sig.kind == "budget")
    assert f.split == "Platform=Phone" and "rule of thumb" in f.headline()
    assert res.status == "Watch"  # known and chronic: not an emergency


def test_new_game_is_still_learning():
    conn = game_db(seed=1)
    conn.execute("DELETE FROM data_points WHERE bucket_time < ?", ((END - timedelta(days=15)).isoformat(),))
    res = checkup.run(conn, 1, today=END, record=False)
    assert any("Still learning" in n for n in res.notes)


def test_estimated_days_lower_confidence_for_one_day_blips():
    pts = [detect.Point(END - timedelta(days=40 - i), 1000.0 + (i % 5) * 15, "Valid") for i in range(40)]
    settled = detect.sudden(catalog.get("DailyActiveUsers"), [*pts, detect.Point(END, 1200.0, "Valid")])
    estimate = detect.sudden(catalog.get("DailyActiveUsers"), [*pts, detect.Point(END, 1200.0, "Projected")])
    assert settled.confidence == "high" and estimate.confidence == "medium" and estimate.provisional
    # A huge jump stays high even on an estimate.
    huge = detect.sudden(catalog.get("DailyActiveUsers"), [*pts, detect.Point(END, 1500.0, "Projected")])
    assert huge.confidence == "high"


# ------------------------------------------------------ memory + marks ---

def test_checkup_remembers_days_and_marks():
    conn = game_db(seed=3, events=[Event("ClientCrashRate15m", 3.0, 3, "Phone")], end=END - timedelta(days=2))
    first = checkup.run(conn, 1, today=END)
    assert first.top and "day " not in first.top[0].headline()
    checkup.run(conn, 1, today=END)                       # same day again: no double counting
    assert json.loads(conn.execute("SELECT top_keys FROM checkups").fetchone()[0])
    # Two more days of data, problem still there.
    selftest.load(conn, 1, Game(seed=3, events=[Event("ClientCrashRate15m", 3.0, 5, "Phone")]), END)
    later = checkup.run(conn, 1, today=END)
    crash = next(f for f in later.top if f.metric == "ClientCrashRate15m")
    assert "day 2 on this list" in crash.headline()
    # Snooze hides it.
    n = later.top.index(crash) + 1
    assert "Snoozed" in checkup.mark(conn, 1, n, "snooze")
    assert not any(f.metric == "ClientCrashRate15m" and f.split == crash.split
                   for f in checkup.run(conn, 1, today=END).top)
    assert "Unknown action" in checkup.mark(conn, 1, 1, "delete")


def test_marked_fixed_comes_back_if_it_is_not():
    conn = game_db(seed=3, events=[Event("ClientCrashRate15m", 3.0, 6, "Phone")], end=END - timedelta(days=4))
    res = checkup.run(conn, 1, today=END)
    n = next(i for i, f in enumerate(res.top, 1) if f.metric == "ClientCrashRate15m")
    checkup.mark(conn, 1, n, "done")
    assert not any(f.metric == "ClientCrashRate15m" for f in checkup.run(conn, 1, today=END).top)
    selftest.load(conn, 1, Game(seed=3, events=[Event("ClientCrashRate15m", 3.0, 6, "Phone")]), END)
    back = next(f for f in checkup.run(conn, 1, today=END).top if f.metric == "ClientCrashRate15m")
    assert "still there after you marked it fixed" in back.headline()


# ------------------------------------------------------------- changes ---

def test_change_weight_rubric():
    s = dict.fromkeys(changes.FACTOR_KEYS, 0)
    assert changes.weigh(s) == (0, "Light", 3)
    assert changes.weigh({**s, "reach": 2, "systems": 2, "spread": 2})[1] == "Medium"
    assert changes.weigh({**s, "paid": 3})[1] == "Heavy"      # one-way door rule
    assert changes.weigh({**s, "undo": 3})[1] == "Heavy"
    assert changes.weigh(dict.fromkeys(changes.FACTOR_KEYS, 3)) == (21, "Major", 21)
    with pytest.raises(ValueError):
        changes.weigh({**s, "reach": 4})
    with pytest.raises(ValueError):
        changes.weigh({"reach": 1})


def test_change_is_judged_good_when_target_improves():
    shipped = END - timedelta(days=9)
    conn = game_db(seed=2, events=[Event("ForwardD1Retention", 1.35, 9)])
    msg = changes.log(conn, 1, "Shorter tutorial", {**dict.fromkeys(changes.FACTOR_KEYS, 1)}, shipped,
                      "ForwardD1Retention")
    assert "Medium" in msg
    res = checkup.run(conn, 1, today=END)
    note = next(n for n in res.notes if n.startswith("Change #1"))
    assert "looks good" in note and "Day 1 retention" in note
    assert not any(n.startswith("Change #1") for n in checkup.run(conn, 1, today=END).notes)  # told once


def test_change_is_judged_harmful_when_crashes_rise():
    shipped = END - timedelta(days=9)
    conn = game_db(seed=2, events=[Event("ClientCrashRate15m", 2.0, 9)])
    changes.log(conn, 1, "New map", dict.fromkeys(changes.FACTOR_KEYS, 1), shipped)
    changes.log(conn, 1, "Price change", {**dict.fromkeys(changes.FACTOR_KEYS, 1), "paid": 3}, shipped)
    notes = checkup.run(conn, 1, today=END).notes
    first = next(n for n in notes if n.startswith("Change #1"))
    assert "looks harmful" in first and "Crash rate" in first and "Overlaps with #2" in first


def test_change_waits_until_due():
    conn = game_db(seed=2)
    changes.log(conn, 1, "Big rework", dict.fromkeys(changes.FACTOR_KEYS, 2), END - timedelta(days=2))
    assert not any(n.startswith("Change") for n in checkup.run(conn, 1, today=END).notes)
    assert "waiting, judge on" in weekly.build(conn, 1, END).text()


# -------------------------------------------------------------- weekly ---

def test_weekly_checkup_scorecard_and_issues():
    conn = game_db(seed=3, events=[Event("ClientCrashRate15m", 2.5, 5)])
    checkup.run(conn, 1, today=END)
    w = weekly.build(conn, 1, END)
    assert [a.name for a in w.areas] == [n for n, _ in weekly.AREAS]
    stab = w.areas[0]
    assert stab.verdict == "worse" and "crash rate" in stab.detail
    text = w.text()
    assert "Crash rate" in text and "Next week: 1) Fix: Crash rate" in text
    assert len(text) < 1200


def test_weekly_is_added_once_on_a_new_week_only():
    conn = game_db(seed=1, end=END - timedelta(days=7))
    assert checkup.run(conn, 1, today=END, weekly=None).extra == ""   # very first check-up
    selftest.load(conn, 1, Game(seed=1), END)
    assert "Weekly check-up" in checkup.run(conn, 1, today=END, weekly=None).extra
    assert checkup.run(conn, 1, today=END, weekly=None).extra == ""   # same day again


# ----------------------------------------------------------------- MCP ---

def test_mcp_mark_and_log_change_tools():
    conn = game_db(seed=3, events=[Event("ClientCrashRate15m", 3.0, 1, "Phone")])
    srv = mcp_server.Server(conn=conn, universe_id=1, client=None)
    tools = json.dumps({"tools": mcp_server.TOOLS})
    assert len(tools) < 3000  # ~750 tokens, sent once per session
    text = srv.daily_checkup(refresh=False)
    assert text.startswith("Roblox game check-up") and len(text) < 1500
    assert "Marked" in srv.mark_item(1, "done")
    rubric = srv.log_change("Faster rebirths")
    assert all(k in rubric for k in changes.FACTOR_KEYS)
    assert "Logged change #1" in srv.log_change("Faster rebirths", dict.fromkeys(changes.FACTOR_KEYS, 1))
    assert srv.log_change("x", {"reach": 9}).startswith("Not logged")
    assert srv.log_change("x", dict.fromkeys(changes.FACTOR_KEYS, 1), shipped_on="soon").startswith("Not logged")
    weekly_text = srv.daily_checkup(refresh=False, weekly=True)
    assert "Weekly check-up" in weekly_text and len(weekly_text) < 2600  # ~650 tokens, once a week


# ------------------------------------------------------------- privacy ---

def test_key_never_reaches_new_tables(tmp_path):
    from roblox_agent.api import AnalyticsClient
    from roblox_agent.demo import FakeRoblox
    key = "SECRET-test-key-1234567890abcdef"
    path = tmp_path / "a.db"
    conn = db.connect(path)
    client = AnalyticsClient(api_key=key, universe_id=42, transport=FakeRoblox(valid_key=key, incident=True),
                             sleep=lambda _s: None)
    ingest.pull(client, conn, backfill_days=60, end=END)
    checkup.run(conn, 42, today=END, weekly=True)
    changes.log(conn, 42, "x", dict.fromkeys(changes.FACTOR_KEYS, 0), END)
    conn.close()
    assert key.encode() not in path.read_bytes()


# ----------------------------------------------------------------- CLI ---

def test_cli_mark_change_week(capsys):
    assert cli.main(["demo", "--no-open"]) == 0
    out = capsys.readouterr().out
    assert "Act now" in out and "Weekly check-up" in out and "Crash rate on Phone" in out
    assert cli.main(["mark", "1", "done", "--demo"]) == 0
    assert cli.main(["change", "Double coins", "--scores", "3,2,3,3,1,2,0", "--demo"]) == 0
    assert "Heavy" in capsys.readouterr().out
    assert cli.main(["week", "--demo"]) == 0
    assert "Double coins" in capsys.readouterr().out
    assert cli.main(["change", "Bad", "--scores", "1,2", "--demo"]) == 1
