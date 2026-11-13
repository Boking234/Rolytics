"""Public-data starting guesses, slow declines, longevity, "why fewer players", and adapting.

All offline. Pretend games come from ``roblox_agent.selftest``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from roblox_agent import catalog, checks, checkup, cli, config, db, decline, detect, mcp_server, priors, selftest, why
from roblox_agent.detect import Point, Signal
from roblox_agent.selftest import Event, Game

END = date(2026, 11, 12)
DAU = catalog.get("DailyActiveUsers")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(config.KEY_ENV, raising=False)


def game_db(end: date = END, **kw):
    conn = db.connect(":memory:")
    selftest.load(conn, 1, Game(**kw), end)
    return conn


# ------------------------------------------------- public-data guesses ---

def test_small_games_are_expected_to_wobble_more():
    sizes = (20, 200, 2000, 20000, 2_000_000)
    wobble = [priors.noise("DailyActiveUsers", s) for s in sizes]
    assert wobble == sorted(wobble, reverse=True)
    assert 0.3 < wobble[0] <= 0.40 and wobble[-1] == 0.06     # the fitted curve, capped and floored
    assert priors.noise("ClientCrashRate15m", 1000) is None   # no public evidence: no made-up number
    # retention needs the usual level, and is never calmer than chance alone allows
    assert priors.noise("ForwardD1Retention", 1000) is None
    assert priors.noise("ForwardD1Retention", 100, 0.2) > priors.noise("ForwardD1Retention", 10000, 0.2)


def test_young_small_game_normal_wobble_is_not_an_alarm():
    """16 days of history, 60 players a day, a 30% dip on the last day: ordinary for a game this small."""
    days = [END - timedelta(days=15 - i) for i in range(16)]
    vals = [64, 57, 68, 59, 60, 71, 52, 66, 58, 75, 49, 63, 70, 55, 61, 42]
    pts = [Point(d, v, "Valid") for d, v in zip(days, vals)]
    assert detect.sudden(DAU, pts, size=60) is None
    big = [Point(d, v * 500, "Valid") for d, v in zip(days, 
                                                     [61, 60, 59, 60, 60, 61, 59, 60, 62, 60, 59, 61, 60, 60, 61, 30])]
    sig = detect.sudden(DAU, big, size=30000)   # a big game halving overnight is news even in week two
    assert sig is not None and sig.learning and sig.confidence == "medium"


def test_public_guess_fades_as_the_game_gets_history():
    """With months of history the result must not depend on the public-data guess at all."""
    conn = game_db(seed=3, events=[Event("DailyActiveUsers", 0.65, 2)])
    pts = detect.load_series(conn, 1, "DailyActiveUsers", "")
    a, b = detect.sudden(DAU, pts, size=1200), detect.sudden(DAU, pts)
    assert a is not None and b is not None and a.z == b.z


# ------------------------------------------------------- slow declines ---

def test_drop_that_stayed_is_still_reported_three_weeks_later_with_its_start_day():
    conn = game_db(seed=3, events=[Event("DailyActiveUsers", 0.8, 20)])
    c = checkup.run(conn, 1, today=END, record=False)
    f = next(f for f in c.top if f.metric == "DailyActiveUsers")
    assert f.sig.kind in checkup.SLOW and not f.good and "around" in f.headline()
    sig = decline.level_change(DAU, detect.load_series(conn, 1, "DailyActiveUsers", ""))
    assert abs((sig.since - (END - timedelta(days=21))).days) <= 3
    assert "dropped around" in sig.detail and -0.25 < sig.rel < -0.15
    assert c.status == "Watch"  # old news is never "Act now"


def test_four_week_check_is_calm_on_calm_and_recovered_games():
    for kw in ({}, {"weekend": 2.2}, {"dau": 150}, {"events": [Event("DailyActiveUsers", 0.7, 40, stop=20)]}):
        for seed in range(4):
            conn = game_db(seed=seed, **kw)
            pts = detect.load_series(conn, 1, "DailyActiveUsers", "")
            assert decline.level_change(DAU, pts) is None, (kw, seed)


def test_turning_point_tells_a_drop_from_a_slide():
    drop = detect.load_series(game_db(seed=1, events=[Event("DailyActiveUsers", 0.75, 30)]), 1, "DailyActiveUsers", "")
    day, shape = decline.turning_point(drop)
    assert shape == "step" and abs((day - (END - timedelta(days=31))).days) <= 3
    slide = detect.load_series(game_db(seed=1, events=[Event("DailyActiveUsers", 0.6, 0, ramp_weeks=6)]), 1,
                               "DailyActiveUsers", "")
    day, shape = decline.turning_point(slide)
    assert shape == "slide" and abs((day - (END - timedelta(days=43))).days) <= 8
    assert decline.turning_point(detect.load_series(game_db(seed=1), 1, "DailyActiveUsers", "")) is None


def test_lifecycle_of_a_fading_game():
    conn = game_db(seed=2, days=300, events=[Event("DailyActiveUsers", 0.3, 0, ramp_weeks=30)])
    life = decline.lifecycle(conn, 1)
    assert life.stage == "fading" and life.now < 0.5 * life.peak
    assert -0.06 < life.weekly_rate < -0.02 and 10 < life.halving_weeks < 40
    text = life.text()
    assert "fading" in text and "halve in about" in text and len(text) < 330
    assert decline.lifecycle(game_db(seed=2), 1).stage in ("steady", "peak")
    assert decline.lifecycle(game_db(seed=2, days=30), 1).stage == "new"


# --------------------------------------------- why are players leaving ---

def test_why_blames_fewer_new_players():
    conn = game_db(seed=4, events=[Event("NewPlayers", 0.65, 0, ramp_weeks=5)])
    w = why.explain(conn, 1)
    assert w.cause == "new" and w.rel < -0.1 and w.new_rel < -0.2
    assert abs(w.stay_now - w.stay_before) < 0.02
    text = w.text()
    assert "fewer new players arriving" in text and "fewer people are shown the game" in text
    assert w.new_needed > w.new_now
    assert len(text) < 1300  # ~330 tokens
    c = checkup.run(conn, 1, today=END, record=False)
    assert any("Mostly fewer new players" in f.headline() for f in c.top)


def test_why_blames_players_coming_back_less():
    conn = game_db(seed=4, events=[Event("ComeBack", 0.9, 28), Event("ForwardD7Retention", 0.8, 28)])
    w = why.explain(conn, 1)
    assert w.cause == "return" and w.stay_now < w.stay_before - 0.04 and abs(w.new_rel) < 0.06
    assert "players coming back less" in w.text() and "Day 7 retention" in w.text()


def test_why_names_the_device_and_the_change_shipped_then():
    conn = game_db(seed=5, events=[Event("DailyActiveUsers", 0.5, 24, "Phone")])
    from roblox_agent import changes
    changes.log(conn, 1, "New phone controls", dict.fromkeys(changes.FACTOR_KEYS, 1), END - timedelta(days=26))
    text = why.report(conn, 1)
    assert "of the loss is on Phone" in text and "New phone controls" in text


def test_why_on_a_steady_game_and_with_no_history():
    assert "about the same as before" in why.report(game_db(seed=1), 1)
    assert "Not enough history" in why.report(db.connect(":memory:"), 1)
    assert why.short(game_db(seed=1), 1) == ""


# ------------------------------------------------------------ adapting ---

def test_false_alarm_marks_raise_the_bar_for_that_metric_only():
    conn = game_db(seed=0, events=[Event("DailyRevenue", 0.62, 2)])
    first = checkup.run(conn, 1, today=END)
    n = next(i for i, f in enumerate(first.top, 1) if f.metric == "DailyRevenue")
    z = abs(first.top[n - 1].sig.z)
    assert "stronger evidence" in checkup.mark(conn, 1, n, "wrong")
    t = checks.load(conn, 1, END)
    assert t.strict("DailyRevenue", "") == 0.5 and t.strict("DailyActiveUsers", "") == 0.0
    # three verdicts: capped; a later confirmed problem wins some back
    for action in ("wrong", "wrong", "wrong", "done"):
        conn.execute("INSERT INTO feedback (universe_id, metric, split, action, day, created_at)"
                     " VALUES (1, 'DailyRevenue', '', ?, ?, 'x')", (action, END.isoformat()))
    assert checks.load(conn, 1, END).strict("DailyRevenue", "") == 1.5
    pts = detect.load_series(conn, 1, "DailyRevenue", "")
    spec = catalog.get("DailyRevenue")
    assert detect.sudden(spec, pts) is not None
    assert detect.sudden(spec, pts, strict=max(0.0, 9 - z)) is None or z > 9


def test_sensitivity_setting():
    conn = game_db(seed=1)
    for level, want in (("low", 1.0), ("high", -0.5), ("nonsense", 0.0)):
        db.set_setting(conn, "sensitivity", level)
        assert checks.load(conn, 1, END).strict("DailyActiveUsers", "") == want
    # high sensitivity never shows fewer items than low
    counts = {}
    for level in ("low", "high"):
        db.set_setting(conn, "sensitivity", level)
        sc = next(s for s in selftest.SCENARIOS if s.name == "d1_drop")
        c2 = game_db(seed=0, events=sc.events)
        db.set_setting(c2, "sensitivity", level)
        counts[level] = sum(not f.good for f in checkup.run(c2, 1, today=END, record=False).top)
    assert counts["high"] >= counts["low"]


def test_own_limit_and_switching_a_check_off():
    conn = game_db(seed=2)
    assert checkup.run(conn, 1, today=END, record=False).status == "All good"
    db.set_setting(conn, "limits", {"ClientFpsP10": ["min", 40]})   # stricter than the default 20 fps
    c = checkup.run(conn, 1, today=END, record=False)
    assert any(f.sig.kind == "budget" and f.metric == "ClientFpsP10" for f in c.top)
    db.set_setting(conn, "checks_off", ["limit"])
    assert checkup.run(conn, 1, today=END, record=False).status == "All good"
    assert "limit (off)" in checks.describe(conn, 1)


def test_a_new_check_can_be_plugged_in():
    @checks.register("round_number", "Demo: players passed 1,000")
    def _milestone(c: checks.Context) -> Signal | None:
        if c.spec.name != "DailyActiveUsers" or c.key or c.pts[-1].value < 1000:
            return None
        p = c.pts[-1]
        return Signal(c.spec.name, "", "spike", p.day, p.day, p.value, 1000.0, 1.0, 9.0, 1, True, False, "high")
    try:
        c = checkup.run(game_db(seed=1), 1, today=END, record=False)
        assert any(f.metric == "DailyActiveUsers" and f.good for f in c.top)
    finally:
        del checks.REGISTRY["round_number"]


def test_context_metrics_are_stored_but_never_alarm():
    conn = game_db(seed=2, events=[Event("ForwardD30Retention", 0.2, 5)])
    assert detect.load_series(conn, 1, "ForwardD30Retention", "")
    assert checkup.run(conn, 1, today=END, record=False).status == "All good"


# ------------------------------------------------------- agent and CLI ---

def test_agent_tools_stay_small_and_explain_player_trend():
    import json
    assert len(json.dumps(mcp_server.TOOLS)) < 2300   # ~550 tokens for the whole tool list
    conn = game_db(seed=4, events=[Event("NewPlayers", 0.65, 0, ramp_weeks=5)])
    server = mcp_server.Server(conn=conn, universe_id=1)
    out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "player_trend", "arguments": {}}})
    text = out["result"]["content"][0]["text"]
    assert "Main cause: fewer new players" in text and "Lifecycle:" in text and len(text) < 1500


def test_weekly_checkup_has_the_lifecycle_line_and_stays_small():
    conn = game_db(seed=4, events=[Event("NewPlayers", 0.65, 0, ramp_weeks=5)])
    text = checkup.run(conn, 1, today=END, weekly=True).text()
    assert "Lifecycle: declining" in text and "roblox-agent why" in text
    assert len(text) < 3200   # ~800 tokens on the one day a week it is longest


def test_cli_why_and_tune(capsys):
    assert cli.main(["demo", "--no-open"]) == 0
    out = capsys.readouterr().out
    assert "Mostly fewer new players" in out and "Lifecycle: declining" in out
    assert cli.main(["why", "--demo"]) == 0
    out = capsys.readouterr().out
    assert "Main cause: fewer new players" in out and "New icon and thumbnail" in out
    assert cli.main(["tune", "sensitivity", "low", "--demo"]) == 0
    assert "Sensitivity: low" in capsys.readouterr().out
    assert cli.main(["tune", "limit", "ClientFpsP10", "min", "25", "--demo"]) == 0
    assert "at least 25" in capsys.readouterr().out
    assert cli.main(["tune", "sensitivity", "loud", "--demo"]) == 1
