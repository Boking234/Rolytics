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

KEY = "SECRET-test-key-1234567890abcdef"
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
    config.save_api_key("file-key-0123456789abcdef")
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


# ------------------------------------------------------------ Check-up ---

def _seed(conn, metric, values, key="", start=date(2026, 9, 1), status=None):
    from datetime import timedelta
    for i, v in enumerate(values):
        d = start + timedelta(days=i)
        conn.execute("INSERT OR REPLACE INTO pull_runs (id, universe_id, started_at) VALUES (1, 42, '2026-11-13T00:00:00Z')")
        conn.execute("INSERT OR REPLACE INTO data_points VALUES (42, ?, 'OneDay', ?, ?, ?, NULL, ?, 'x', 'x', 1)",
                     (metric, key, f"{d.isoformat()}T00:00:00Z", v, status))


def test_checkup_flags_a_real_crash_spike_first():
    from roblox_agent import checkup
    c = db.connect(":memory:")
    _seed(c, "DailyActiveUsers", [1000 + (i % 7) * 20 for i in range(42)])
    _seed(c, "ClientCrashRate15m", [0.010 + (i % 3) * 0.0005 for i in range(41)] + [0.03])
    result = checkup.run(c, 42, today=date(2026, 10, 13))
    assert result.status == "Act now"
    assert result.top[0].metric == "ClientCrashRate15m" and not result.top[0].good
    assert len(result.top) == 1  # steady DAU is not news


def test_checkup_ignores_normal_weekend_pattern():
    from roblox_agent import checkup
    c = db.connect(":memory:")
    # Weekends are always 40% higher: that's normal, not a finding.
    _seed(c, "DailyActiveUsers", [1400 if (date(2026, 9, 1).toordinal() + i) % 7 in (5, 6) else 1000
                                  for i in range(42)])
    assert checkup.run(c, 42, today=date(2026, 10, 13)).status == "All good"


def test_checkup_skips_low_sample_days():
    from roblox_agent import checkup
    c = db.connect(":memory:")
    _seed(c, "DailyRevenue", [500.0] * 41)
    _seed(c, "DailyRevenue", [5.0], start=date(2026, 10, 12), status="NotStatisticallySignificant")
    assert checkup.run(c, 42, today=date(2026, 10, 13)).top == []


def test_checkup_text_is_tiny(fake, conn):
    """The daily check-up an agent reads must stay far below a 1 MB file (~250k tokens)."""
    from roblox_agent import checkup
    ingest.pull(make_client(fake), conn, backfill_days=120, end=END)
    text = checkup.run(conn, 42, today=END).text()
    assert len(text.encode()) < 1500  # ~400 tokens at most


def test_html_page_leads_with_checkup(fake, conn):
    ingest.pull(make_client(fake), conn, backfill_days=60, end=END)
    page = report.html_report(conn, 42)
    assert page.index("Today's check-up") < page.index("All metrics")


# ----------------------------------------------------------------- MCP ---

def _mcp(server, *messages):
    import io
    import json
    out = io.BytesIO()
    from roblox_agent import mcp_server
    data = b"".join(json.dumps(m).encode() + b"\n" for m in messages)
    mcp_server.serve(io.BytesIO(data), out, server)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_handshake_tools_and_budget(fake, conn):
    from roblox_agent import mcp_server
    ingest.pull(make_client(fake), conn, backfill_days=60, end=END)
    srv = mcp_server.Server(conn=conn, universe_id=42, client=None)
    replies = _mcp(srv,
                   {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                   {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "daily_checkup", "arguments": {"refresh": False}}},
                   {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "metric_history", "arguments": {"metric": "DailyActiveUsers", "days": 500}}},
                   {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                   {"jsonrpc": "2.0", "id": 6, "method": "nope"})
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6]  # no reply to the notification
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    import json
    assert len(json.dumps(replies[1]["result"])) < 2000           # tool definitions stay small
    checkup_text = replies[2]["result"]["content"][0]["text"]
    assert "check-up" in checkup_text and len(checkup_text) < 1500
    history = replies[3]["result"]["content"][0]["text"].splitlines()
    assert len(history) <= 91                                      # capped at 90 days
    assert replies[4]["result"] == {}
    assert replies[5]["error"]["code"] == -32601
    assert KEY not in json.dumps(replies)


def test_mcp_newer_stateless_protocol(conn):
    from roblox_agent import mcp_server
    srv = mcp_server.Server(conn=conn, universe_id=42, client=None)
    meta = {"_meta": {mcp_server.META_VERSION_KEY: "2026-07-28"}}
    r = _mcp(srv, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": meta},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": meta})
    assert "2026-07-28" in r[0]["result"]["supportedVersions"]
    assert r[1]["result"]["resultType"] == "complete"


def test_mcp_server_runs_as_a_real_process(tmp_path):
    """Start 'python -m roblox_agent mcp' like an agent would; stdout must carry only protocol messages."""
    import json
    import subprocess
    env = {**os.environ, config.HOME_ENV: str(tmp_path), "PYTHONPATH": os.pathsep.join(sys.path)}
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "daily_checkup"}}]
    out = subprocess.run([sys.executable, "-m", "roblox_agent", "mcp"], env=env, capture_output=True, timeout=30, check=False,
                         input="".join(json.dumps(m) + "\n" for m in msgs).encode())
    lines = [json.loads(x) for x in out.stdout.decode().splitlines()]
    assert [m["id"] for m in lines] == [1, 2]
    assert "Not set up" in lines[1]["result"]["content"][0]["text"]


# ------------------------------------------------------------- Connect ---

def test_connect_json_agent_merges_and_never_writes_key(tmp_path, monkeypatch):
    import json

    from roblox_agent import connect
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    monkeypatch.setattr(connect, "json_config_path", lambda a: path)
    config.save_api_key(KEY)
    ok, _msg = connect.connect(connect.BY_KEY["cursor"])
    data = json.loads(path.read_text())
    assert ok and "other" in data["mcpServers"]
    assert data["mcpServers"]["roblox-agent"]["args"] == ["-m", "roblox_agent", "mcp"]
    assert KEY not in path.read_text()
    assert (tmp_path / "mcp.json.bak").exists()


def test_connect_refuses_to_overwrite_broken_config(tmp_path, monkeypatch):
    from roblox_agent import connect
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ broken")
    monkeypatch.setattr(connect, "json_config_path", lambda a: path)
    ok, _msg = connect.connect(connect.BY_KEY["claude-desktop"])
    assert not ok and path.read_text() == "{ broken"


@pytest.mark.parametrize("key,program", [("claude-code", "claude"), ("codex", "codex"),
                                         ("gemini", "gemini"), ("vscode", "code")])
def test_connect_cli_agents(key, program):
    from roblox_agent import connect
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "added", ""

    ok, _ = connect.connect(connect.BY_KEY[key], run=lambda cmd, **kw: calls.append(cmd) or Done(),
                            which=lambda name: "/bin/" + name)
    assert ok and calls[0][0] == program and KEY not in " ".join(calls[0])
    missing_ok, msg = connect.connect(connect.BY_KEY[key], which=lambda name: None)
    assert not missing_ok and "couldn't find" in msg


def test_home_command_runs_setup_first_time(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert cli.main([]) == 1  # empty answer: setup stops politely
    assert "Welcome" in capsys.readouterr().out
