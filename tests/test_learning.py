"""v0.5: learning from clicks and changes, likely causes, fix suggestions and fix checks, the scorecard.

All offline, on pretend games from ``roblox_agent.selftest``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from roblox_agent import causes, changes, checks, checkup, cli, config, db, fixes, learn, selftest
from roblox_agent.selftest import Event, Game

END = date(2026, 11, 12)
CRASH = [Event("ClientCrashRate15m", 2.2, 1)]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(config.KEY_ENV, raising=False)


def game_db(end: date = END, **kw):
    conn = db.connect(":memory:")
    selftest.load(conn, 1, Game(**kw), end)
    return conn


def scores(text: str) -> dict:
    return dict(zip(changes.FACTOR_KEYS, (int(x) for x in text.split(","))))


# ------------------------------------------------------------- the scorecard ---

def test_accuracy_is_no_worse_than_the_saved_scorecard():
    """Every release: planted problems, calm days and causes must not get worse than accuracy.json."""
    saved = selftest.saved_scorecard()
    assert saved, "accuracy.json is missing: run 'roblox-agent selftest --save'"
    res = selftest.run_all(seeds=saved["seeds"], calm_runs=saved["calm_runs"])
    card = selftest.scorecard(res)
    assert selftest.regressions(card, saved) == [], selftest.summary(res)
    assert card["sweep_act_now"] == 0
    assert card["causes_right"] / card["causes_total"] >= 0.8, selftest.summary(res)


def test_regressions_are_reported_in_plain_words():
    saved = {"planted_found": 10, "planted_total": 12, "calm_kept": 5, "calm_total": 5, "causes_right": 3,
             "causes_total": 3, "sweep_alarms": 1, "sweep_total": 20, "sweep_act_now": 0, "version": "0.5.0"}
    worse = dict(saved, planted_found=9, sweep_act_now=1)
    out = selftest.regressions(worse, saved)
    assert any("planted problems found: 9, was 10" in o for o in out)
    assert any("'Act now' on calm days" in o for o in out)
    assert selftest.regressions(saved, saved) == []
    # A bigger test (new scenarios) is not compared on raw counts it can't match.
    assert selftest.regressions(dict(saved, planted_total=11, planted_found=9), saved) == []


def test_selftest_on_own_data_plants_into_a_copy_only():
    conn = game_db(seed=3)
    before = conn.execute("SELECT SUM(value) FROM data_points").fetchone()[0]
    text = selftest.run_on_own(conn, 1)
    assert "On your own game's history: found" in text
    found, total = (int(x) for x in text.split("found ")[1].split(" planted")[0].split(" of "))
    assert total == 24 and found >= 18, text
    assert conn.execute("SELECT SUM(value) FROM data_points").fetchone()[0] == before


# ---------------------------------------------------------- likely causes ---

def test_a_change_shipped_just_before_is_named_as_the_likely_cause():
    sc = next(s for s in selftest.SCENARIOS if s.name == "update_broke_d1")
    outs = [selftest.run_scenario(sc, seed, END - timedelta(days=seed)) for seed in range(1, 4)]
    assert sum(o.cause_ok is True for o in outs) >= 2, [o.why for o in outs]
    assert any("Likely cause: change #1 'New tutorial'" in " ".join(o.lines) for o in outs)


def test_device_mix_is_not_reported_as_a_bug():
    sc = next(s for s in selftest.SCENARIOS if s.name == "phone_players_surge")
    for seed in range(3):
        o = selftest.run_scenario(sc, seed, END - timedelta(days=seed))
        assert o.passed, o.why
        assert o.status != "Act now"
        text = " ".join(o.lines)
        assert "player mix, not a new bug" in text and "Mostly on" not in text.split("Frame rate")[1][:80]


def test_new_player_wave_is_named_and_broken_join_is_not_blamed_on_discovery():
    wave = next(s for s in selftest.SCENARIOS if s.name == "new_player_wave")
    assert selftest.run_scenario(wave, 0, END).cause_ok
    conn = game_db(seed=2, events=[Event("DailyActiveUsers", 0.65, 2)])  # broken join: Roblox still shows it
    res = checkup.run(conn, 1, today=END, record=False)
    dau = next(f for f in res.top if f.metric == "DailyActiveUsers")
    assert dau.cause is None or dau.cause.kind != "discovery"


def test_no_cause_is_invented_on_calm_games():
    for seed in range(4):
        conn = game_db(seed=seed)
        changes.log(conn, 1, "Small fix", scores("0,1,0,0,0,0,0"), END - timedelta(days=4))
        res = checkup.run(conn, 1, today=END, record=False)
        assert "Likely cause" not in res.text()


# ------------------------------------------------------- learning from clicks ---

def _mark_crash_items(conn, action: str, times: int) -> None:
    for k in range(times):
        conn.execute("INSERT OR REPLACE INTO checkups (universe_id, data_day, status, top_keys, text, created_at)"
                     " VALUES (1, ?, 'Watch', ?, '', '')",
                     ((END - timedelta(days=40 - k)).isoformat(), '["ClientCrashRate15m||up"]'))
        conn.commit()
        assert not checkup.mark(conn, 1, 1, action).startswith(("Unknown", "Pick"))


def test_clicks_move_the_ranking_but_never_the_status():
    conn = game_db(seed=1, events=CRASH)
    base = checkup.run(conn, 1, today=END, record=False)
    item = next(f for f in base.top if f.metric == "ClientCrashRate15m")
    _mark_crash_items(conn, "wrong", 4)
    after = checkup.run(conn, 1, today=END, record=False)
    dismissed = next((f for f in after.top if f.metric == "ClientCrashRate15m"), None)
    assert dismissed is None or dismissed.learned < 1.0
    rk = learn.ranking(conn, 1, END)
    assert rk.factor("ClientCrashRate15m", "sudden") < 1.0
    assert rk.factor("ClientCrashRate15m", "sudden") >= learn.CLAMP[0]
    assert item.severity == "act"


def test_acting_on_items_ranks_them_higher_and_snooze_is_remembered():
    conn = game_db(seed=1)
    _mark_crash_items(conn, "done", 3)
    _mark_crash_items(conn, "snooze", 1)
    rk = learn.ranking(conn, 1, END)
    assert rk.factor("ClientCrashRate15m", "sudden") > 1.0
    acts = {r["action"] for r in conn.execute("SELECT action FROM feedback")}
    assert acts == {"done", "snooze"}
    assert any("usually act on" in line for line in rk.lines())
    # Snoozes don't make the checks ask for stronger evidence (only false alarms do).
    assert checks.load(conn, 1, END).feedback == {}


def test_one_stray_click_barely_moves_anything():
    rk = learn.Ranking(by_metric={"DailyRevenue": (0.0, 1.0)})
    assert 0.9 < rk.factor("DailyRevenue", "sudden") < 1.0


# ------------------------------------------------------- learning from changes ---

def _judged_change(conn, title: str, rubric: str, shipped: date, outcome: str, effects: dict[str, tuple]):
    changes.log(conn, 1, title, scores(rubric), shipped)
    cid = conn.execute("SELECT MAX(id) FROM changes").fetchone()[0]
    conn.execute("UPDATE changes SET outcome=?, outcome_note='', announced=1 WHERE id=?", (outcome, cid))
    for metric, (rel, verdict) in effects.items():
        conn.execute("INSERT INTO change_effects (change_id, metric, rel, z, verdict) VALUES (?, ?, ?, 3, ?)",
                     (cid, metric, rel, verdict))
    conn.commit()
    return cid


def test_lessons_from_judged_changes():
    conn = game_db(seed=1)
    d = END - timedelta(days=60)
    _judged_change(conn, "Cheaper pets", "1,1,1,2,0,0,1", d, "bad", {"PayingUsersCVR": (-0.15, "worse")})
    _judged_change(conn, "New coin source", "1,1,1,3,0,0,1", d, "bad", {"PayingUsersCVR": (-0.11, "worse")})
    _judged_change(conn, "Trade fees", "1,1,1,2,0,0,1", d, "good", {"PayingUsersCVR": (0.0, "same")})
    out = changes.lessons(conn, 1)
    assert out and out[0].startswith("2 of your 3 economy changes lowered paying user conversion")
    msg = changes.log(conn, 1, "Gem prices", scores("1,1,1,2,0,0,1"), END)
    assert "Past changes like this: 2 of your 3 economy changes" in msg
    assert "Past changes like this" not in changes.log(conn, 1, "New hat", scores("1,0,0,0,0,0,0"), END)
    assert "Learned from your changes" in checkup.run(conn, 1, today=END, record=False, weekly=True).text()


def test_light_changes_that_kept_hurting_are_judged_later():
    conn = game_db(seed=1)
    d = END - timedelta(days=60)
    for t in ("Tweak A", "Tweak B"):
        _judged_change(conn, t, "1,1,0,0,0,0,0", d, "bad", {})
    msg = changes.log(conn, 1, "Tweak C", scores("1,1,0,0,0,0,0"), END)
    assert "Light" in msg and "Waiting longer than usual" in msg
    row = conn.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 1").fetchone()
    assert (date.fromisoformat(row["judge_on"]) - END).days == 7


def test_judging_keeps_each_metrics_move():
    conn = game_db(seed=2, events=[Event("DailyRevenue", 0.5, 9)])
    changes.log(conn, 1, "Shop rework", scores("2,1,1,2,2,0,1"), END - timedelta(days=10), "DailyRevenue")
    checkup.run(conn, 1, today=END)
    eff = {r["metric"]: r["verdict"] for r in conn.execute("SELECT * FROM change_effects")}
    assert eff.get("DailyRevenue") == "worse"


# ------------------------------------------------------- fixes and fix checks ---

def test_every_problem_gets_one_short_thing_to_try():
    for sc in selftest.SCENARIOS:
        if not sc.expect or not sc.expect[0][2]:
            continue
        conn = game_db(seed=1, events=sc.events, **sc.game)
        for f in checkup.run(conn, 1, today=END, record=False).top:
            if not f.good:
                assert f.fix and len(f.fix) < 130, (sc.name, f.metric)
            else:
                assert not f.fix


def test_suggestions_fit_the_device_and_the_kind_of_problem():
    conn = db.connect(":memory:")
    assert fixes.suggest(conn, 1, "ClientCrashRate15m", "Platform=Phone")[0] == "crash-lowend"
    assert fixes.suggest(conn, 1, "ClientCrashRate15m", "")[0] == "crash-errors"
    assert fixes.suggest(conn, 1, "DailyActiveUsers", "", kind="sudden")[0] == "join-test"
    assert fixes.suggest(conn, 1, "DailyActiveUsers", "", kind="slow", prefer=("content",))[0] == "content"
    cause = causes.Cause("change", "", change_id=4)
    fid, text = fixes.suggest(conn, 1, "ForwardD1Retention", "", cause)
    assert fid == "cause-change" and "#4" in text


def _fix_round(recovered: bool):
    conn = game_db(seed=4, events=CRASH)
    res = checkup.run(conn, 1, today=END)
    n = next(i for i, f in enumerate(res.top, 1) if f.metric == "ClientCrashRate15m")
    first_fix = res.top[n - 1].fix_id
    assert "check in about 3 days" in checkup.mark(conn, 1, n, "done")
    later = END + timedelta(days=5)
    event = Event("ClientCrashRate15m", 2.2, 6, stop=5 if recovered else -1)
    selftest.load(conn, 1, Game(seed=4, events=[event]), later)
    return conn, checkup.run(conn, 1, today=later), first_fix


def test_fix_that_worked_is_confirmed_and_learned():
    conn, res, first_fix = _fix_round(recovered=True)
    assert any(n.startswith("Fix check: Crash rate is back to") and "the fix worked" in n for n in res.notes)
    assert conn.execute("SELECT result FROM fix_tries").fetchone()["result"] == "worked"
    assert conn.execute("SELECT COUNT(*) FROM feedback WHERE action='worked'").fetchone()[0] == 1
    assert first_fix in fixes._history(conn, 1, "ClientCrashRate15m", "")[0]
    # Announced once only.
    assert not any("Fix check" in n for n in checkup.run(conn, 1, today=END + timedelta(days=5)).notes)


def test_fix_that_failed_brings_the_item_back_with_a_new_idea():
    conn, res, first_fix = _fix_round(recovered=False)
    assert any("the fix didn't work yet" in n for n in res.notes)
    again = checkup.run(conn, 1, today=END + timedelta(days=5), record=False)
    item = next(f for f in again.top if f.metric == "ClientCrashRate15m")
    assert item.fix_id and item.fix_id != first_fix


def test_fix_check_waits_for_retention_cohorts():
    assert fixes.wait_days("ForwardD7Retention", "ForwardD7Retention||down") == 10
    assert fixes.wait_days("DailyActiveUsers", "DailyActiveUsers||down|trend") == 14


# ------------------------------------------------------------- plumbing ---

def test_old_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, universe_id INTEGER NOT NULL,
            metric TEXT NOT NULL, split TEXT NOT NULL, action TEXT NOT NULL, day TEXT NOT NULL,
            created_at TEXT NOT NULL);
        INSERT INTO feedback (universe_id, metric, split, action, day, created_at)
            VALUES (1, 'DailyRevenue', '', 'wrong', '2026-11-01', '2026-11-01');
        CREATE TABLE findings (universe_id INTEGER NOT NULL, key TEXT NOT NULL, metric TEXT NOT NULL,
            split TEXT NOT NULL, good INTEGER NOT NULL, first_day TEXT NOT NULL, last_day TEXT NOT NULL,
            days_shown INTEGER NOT NULL DEFAULT 0, last_shown TEXT, state TEXT NOT NULL DEFAULT 'open',
            state_until TEXT, peak_rel REAL, last_line TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (universe_id, key));
    """)
    old.commit()
    old.close()
    conn = db.connect(path)
    assert "kind" in {r[1] for r in conn.execute("PRAGMA table_info(feedback)")}
    assert "fix" in {r[1] for r in conn.execute("PRAGMA table_info(findings)")}
    assert checks.load(conn, 1, date(2026, 11, 2)).feedback  # the old label still counts
    assert learn.ranking(conn, 1, date(2026, 11, 2)).by_kind == {}


def test_learned_command(capsys):
    assert cli.main(["demo", "--no-open"]) == 0
    capsys.readouterr()
    assert cli.main(["learned", "--demo"]) == 0
    out = capsys.readouterr().out
    assert "From your clicks" in out and "From the changes you shipped" in out and "From fixes" in out


def test_checkup_stays_small_with_causes_and_fixes():
    worst = 0
    for sc in selftest.SCENARIOS:
        conn = game_db(seed=1, events=sc.events, **sc.game)
        for days_before, title, rubric in sc.changes:
            changes.log(conn, 1, title, scores(rubric), END - timedelta(days=1 + days_before))
        worst = max(worst, len(checkup.run(conn, 1, today=END, record=False).text()))
    assert worst < 1500  # about 375 tokens at most
