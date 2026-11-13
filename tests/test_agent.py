from __future__ import annotations

import os
import sqlite3
import stat
import sys
from datetime import date

import pytest

from roblox_agent import catalog, cli, config, db, ingest, report
from roblox_agent.api import AnalyticsClient, AuthError, BadRequestError, Response
from roblox_agent.demo import FakeRoblox

KEY = "SECRET-test-key-123"
END = date(2026, 11, 13)


def make_client(fake: FakeRoblox, **kw) -> AnalyticsClient:
    return AnalyticsClient(api_key=KEY, universe_id=42, transport=fake, sleep=lambda _s: None, **kw)


@pytest.fixture
def fake() -> FakeRoblox:
    return FakeRoblox(valid_key=KEY)


@pytest.fixture
def conn() -> sqlite3.Connection:
    return db.connect(":memory:")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(config.KEY_ENV, raising=False)
    return tmp_path / "home"


# ---------------------------------------------------------------- API ---

def test_query_returns_points(fake):
    op = make_client(fake).query_metric("DailyActiveUsers", "OneDay", "2026-11-01T00:00:00Z",
                                        "2026-11-08T00:00:00Z")
    assert len(op["response"]["values"][0]["dataPoints"]) == 7
    _method, url, body = fake.calls[0]
    assert url.endswith("/v1/universes/42/metrics")
    assert body["metric"] == "DailyActiveUsers" and "breakdown" not in body


def test_slow_query_is_polled_until_done(fake):
    fake.slow = {"DailyActiveUsers"}
    op = make_client(fake).query_metric("DailyActiveUsers", "OneDay", "2026-11-01T00:00:00Z",
                                        "2026-11-03T00:00:00Z")
    assert op["done"] is True
    assert [c[0] for c in fake.calls] == ["POST", "GET"]
    assert "/operations/metrics/" in fake.calls[1][1]


def test_server_errors_are_retried():
    replies = [Response(503, {"code": 2002}), Response(500, {}),
               Response(200, {"done": True, "response": {"values": []}})]
    sleeps = []
    client = AnalyticsClient(api_key=KEY, universe_id=1, transport=lambda *a: replies.pop(0),
                             sleep=sleeps.append)
    assert client.query_metric("DailyActiveUsers", "OneDay", "a", "b")["done"]
    assert len(sleeps) == 2


def test_bad_key_message_is_friendly_and_hides_key():
    client = AnalyticsClient(api_key=KEY, universe_id=1, transport=lambda *a: Response(401, {}))
    with pytest.raises(AuthError) as e:
        client.query_metric("DailyActiveUsers", "OneDay", "a", "b")
    assert "universe.analytics:read" in str(e.value)
    assert KEY not in str(e.value) and KEY not in repr(client)


def test_unsupported_query_raises_bad_request(fake):
    fake.reject = {"Nope"}
    with pytest.raises(BadRequestError):
        make_client(fake).query_metric("Nope", "OneDay", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")


# ------------------------------------------------------------- Ingest ---

def test_first_pull_backfills_and_stores_status(fake, conn):
    specs = [catalog.BY_NAME["DailyActiveUsers"]]
    result = ingest.pull(make_client(fake), conn, specs, backfill_days=30, end=END)
    assert result.status == "ok"
    plain = conn.execute("SELECT COUNT(*) FROM data_points WHERE breakdown_key=''").fetchone()[0]
    assert plain == 30
    keys = {r[0] for r in conn.execute("SELECT DISTINCT breakdown_key FROM data_points")}
    assert {"", "Platform=Phone", "IsNewUser=true"} <= keys
    last = conn.execute("SELECT bucket_time, status FROM data_points WHERE breakdown_key=''"
                        " ORDER BY bucket_time DESC LIMIT 1").fetchone()
    assert last["bucket_time"] == "2026-11-12T00:00:00Z"  # through yesterday
    assert last["status"] == "Projected"
    assert conn.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0] == 3


def test_performance_metrics_only_go_back_27_days(fake, conn):
    ingest.pull(make_client(fake), conn, [catalog.BY_NAME["ServerCrashCount"]], backfill_days=365, end=END)
    body = fake.calls[0][2]
    assert body["startTime"] == "2026-10-17T00:00:00Z"


def test_second_pull_is_incremental_and_updates_values(fake, conn):
    spec = [catalog.MetricSpec("DailyActiveUsers", catalog.STANDARD, "DAU")]
    client = make_client(fake)
    ingest.pull(client, conn, spec, backfill_days=60, end=date(2026, 11, 10))
    fake.calls.clear()
    ingest.pull(client, conn, spec, backfill_days=60, end=END)
    body = fake.calls[0][2]
    # last stored day was Nov 9, so it re-pulls 10 days back from there
    assert body["startTime"] == "2026-10-30T00:00:00Z"
    n = conn.execute("SELECT COUNT(*) FROM data_points").fetchone()[0]
    assert n == 63  # Sep 11 .. Nov 12, no duplicates
    # a day that was "Projected" before is now updated and Valid (no status)
    row = conn.execute("SELECT status FROM data_points WHERE bucket_time='2026-11-09T00:00:00Z'").fetchone()
    assert row["status"] is None


def test_too_big_queries_are_split(conn):
    fake = FakeRoblox(valid_key=KEY, budget_limit_days=40)
    spec = [catalog.MetricSpec("DailyRevenue", catalog.STANDARD, "Rev")]
    result = ingest.pull(make_client(fake), conn, spec, backfill_days=100, end=END)
    assert result.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM data_points").fetchone()[0] == 100


def test_one_bad_metric_does_not_stop_the_rest(conn):
    fake = FakeRoblox(valid_key=KEY, reject_metrics={"Broken"})
    specs = [catalog.MetricSpec("Broken", catalog.STANDARD, "x"),
             catalog.MetricSpec("Visits", catalog.STANDARD, "Visits")]
    result = ingest.pull(make_client(fake), conn, specs, backfill_days=5, end=END)
    assert result.status == "partial"
    assert result.points == 5
    assert conn.execute("SELECT metric FROM pull_errors").fetchone()["metric"] == "Broken"


def test_bad_key_stops_run_and_marks_it_failed(conn):
    fake = FakeRoblox(valid_key="other")
    with pytest.raises(AuthError):
        ingest.pull(make_client(fake), conn, end=END)
    assert conn.execute("SELECT status FROM pull_runs").fetchone()["status"] == "failed"
    assert len(fake.calls) == 1


def test_key_is_never_stored_in_database(fake, tmp_path):
    path = tmp_path / "a.db"
    c = db.connect(path)
    ingest.pull(make_client(fake), c, backfill_days=10, end=END)
    c.close()
    assert KEY.encode() not in path.read_bytes()


# ------------------------------------------------------------- Config ---

@pytest.mark.parametrize("text,expected", [
    ("1234567890", 1234567890),
    (" https://create.roblox.com/dashboard/creations/experiences/987654/overview ", 987654),
])
def test_parse_universe_id(text, expected):
    assert config.parse_universe_id(text) == expected


def test_parse_universe_id_rejects_junk():
    with pytest.raises(ValueError):
        config.parse_universe_id("my game")


def test_key_saved_outside_project_and_private(isolated_home):
    config.save_api_key(KEY)
    config.save_universe_id(5)
    cfg = config.load()
    assert cfg.api_key == KEY and cfg.universe_id == 5
    assert KEY not in repr(cfg)
    assert config.secrets_path().parent == isolated_home
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(config.secrets_path()).st_mode) == 0o600
    assert config.forget_api_key() and config.load().api_key is None


def test_env_var_key_wins(monkeypatch):
    config.save_api_key("file-key")
    monkeypatch.setenv(config.KEY_ENV, "env-key")
    assert config.load().api_key == "env-key"


# ------------------------------------------------------------- Report ---

def test_reports(fake, conn, tmp_path):
    ingest.pull(make_client(fake), conn, backfill_days=30, end=END)
    text = report.text_summary(conn, 42)
    assert "Daily active users" in text and "Last pull" in text
    page = report.html_report(conn, 42)
    assert "<svg" in page and "Day 1 retention" in page
    n = report.export_csv(conn, 42, tmp_path / "out.csv")
    assert n == conn.execute("SELECT COUNT(*) FROM data_points").fetchone()[0]


def test_empty_database_says_what_to_do(conn):
    assert "roblox-agent pull" in report.text_summary(conn, 1)


# ---------------------------------------------------------------- CLI ---

def test_demo_command_end_to_end(capsys, isolated_home):
    assert cli.main(["demo", "--no-open"]) == 0
    assert (isolated_home / "demo-report.html").exists()
    assert cli.main(["show", "--demo"]) == 0
    assert "Daily active users" in capsys.readouterr().out


def test_commands_explain_missing_setup():
    with pytest.raises(SystemExit) as e:
        cli.main(["pull"])
    assert "roblox-agent setup" in str(e.value)


def test_setup_saves_and_tests_key(monkeypatch, capsys):
    fake = FakeRoblox(valid_key=KEY)
    monkeypatch.setattr(cli, "AnalyticsClient",
                        lambda **kw: AnalyticsClient(transport=fake, sleep=lambda _s: None, **kw))
    monkeypatch.setattr("builtins.input",
                        lambda _p: "https://create.roblox.com/dashboard/creations/experiences/777/overview")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p: KEY)
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Connected" in out and KEY not in out
    assert config.load().universe_id == 777
