"""The desktop app: setup rules, key safety, reports, inbox, scheduler, and the local server."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from roblox_agent import config, connect, db
from roblox_agent.app import discord_link
from roblox_agent.app import service as service_mod
from roblox_agent.app.server import App, start
from roblox_agent.app.service import Service, in_quiet_hours, public_game_info
from roblox_agent.demo import FakeRoblox

KEY = "SECRET-app-key-1234567890abcdef"
GAME_LINK = "https://create.roblox.com/dashboard/creations/experiences/42/overview"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(config.KEY_ENV, raising=False)
    return tmp_path / "home"


class Clock:
    def __init__(self, when: str = "2026-11-13 12:00"):
        self.t = datetime.fromisoformat(when)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def svc() -> Service:
    notes: list[tuple[str, str]] = []
    s = Service(transport=FakeRoblox(valid_key=KEY, incident=True), lookup=lambda uid: {"name": "My Obby", "genre": "Obby & Platformer"},
                notifier=lambda t, b: notes.append((t, b)), opener=lambda url: None, now=Clock())
    s.notes = notes  # type: ignore[attr-defined]
    return s


def wait_job(s: Service, timeout: float = 60) -> None:
    end = time.time() + timeout
    while s.job.running and time.time() < end:
        time.sleep(0.05)
    assert not s.job.running


def set_up(s: Service, age: str = "18plus") -> None:
    assert s.accept_terms(True, age, guardian_ok=True)["ok"]
    assert s.check_key(KEY)["ok"]
    assert s.connect_game(GAME_LINK)["ok"]


# ------------------------------------------------------------ terms & age ---

def test_terms_need_age_agreement_and_guardian_for_teens(svc):
    assert not svc.accept_terms(True, "")["ok"]
    assert not svc.accept_terms(False, "18plus")["ok"]
    assert not svc.accept_terms(True, "13to17", guardian_ok=False)["ok"]
    assert not svc.state()["terms_ok"]
    assert svc.accept_terms(True, "13to17", guardian_ok=True)["ok"]
    assert svc.state()["terms_ok"]


def test_under_13_is_local_only(svc):
    svc.accept_terms(True, "under13")
    assert not svc.set_sharing(True)["ok"]
    assert not svc.state()["share_lessons"]
    assert svc.state()["discord"] == "blocked"
    assert not svc.set_discord("remind")["ok"]
    assert not svc.update_settings(share_lessons=True)["ok"]


def test_sharing_is_off_by_default_and_can_be_switched(svc):
    svc.accept_terms(True, "18plus")
    assert svc.state()["share_lessons"] is False
    assert svc.set_sharing(True)["ok"] and svc.state()["share_lessons"] is True
    assert svc.set_sharing(False)["ok"] and svc.state()["share_lessons"] is False


def test_new_terms_version_asks_again(svc, monkeypatch):
    set_up(svc)
    svc.finish_setup()
    wait_job(svc)
    assert svc.state()["setup_done"]
    monkeypatch.setattr(service_mod, "TERMS_VERSION", "2099-01-01")
    assert not svc.state()["terms_ok"] and not svc.state()["setup_done"]


# --------------------------------------------------------------- key safety ---

def test_key_is_saved_only_after_it_works_with_the_game(svc):
    svc.accept_terms(True, "18plus")
    assert not svc.check_key("short")["ok"]
    assert not svc.check_key("has spaces in it 1234567890")["ok"]
    assert svc.check_key("WRONG-key-but-long-enough-123456")["ok"]  # looks fine, not tested yet
    assert config.load().api_key is None
    r = svc.connect_game(GAME_LINK)
    assert not r["ok"] and "didn't accept" in r["message"]
    assert config.load().api_key is None and config.load().universe_id is None
    svc.check_key(KEY)
    r = svc.connect_game(GAME_LINK)
    assert r["ok"] and r["game"]["name"] == "My Obby"
    assert config.load().api_key == KEY and config.load().universe_id == 42
    assert svc.settings()["genre"] == "Obby & Platformer"


def test_key_never_appears_in_state_or_errors(svc):
    set_up(svc)
    svc.check_key(KEY)  # pending again
    blob = json.dumps([svc.state(), svc.export_settings(), repr(svc)])
    assert KEY not in blob
    assert svc.state()["key_saved"] is True


def test_bad_game_link(svc):
    svc.check_key(KEY)
    assert not svc.connect_game("not a game")["ok"]


# ----------------------------------------------------------- reports, inbox ---

def test_finish_runs_first_report_into_inbox_and_notifies(svc):
    set_up(svc)
    assert svc.finish_setup()["ok"]
    wait_job(svc)
    assert svc.job.error == ""
    items = svc.inbox()
    assert len(items) == 1 and items[0]["kind"] == "daily" and items[0]["status"] in ("Act now", "Watch")
    assert svc.unread() == 1
    assert svc.notes and items[0]["status"] in svc.notes[0][0]
    today = svc.today()
    assert today["status"] == items[0]["status"] and 1 <= len(today["items"]) <= 3
    svc.mark_read(items[0]["id"])
    assert svc.unread() == 0


def test_quiet_hours_hold_the_notification(svc):
    set_up(svc)
    svc.update_settings(quiet_start="11:00", quiet_end="13:00")
    svc.finish_setup()
    wait_job(svc)
    assert svc.notes == [] and svc.unread() == 1
    svc.now.t = datetime.fromisoformat("2026-11-13 13:30")
    svc.tick()
    assert len(svc.notes) == 1


def test_mark_by_key_survives_a_rerun(svc):
    set_up(svc)
    svc.finish_setup()
    wait_job(svc)
    item = svc.today()["items"][0]
    r = svc.mark(item["key"], "snooze")
    assert r["ok"] and "Snoozed" in r["message"]
    assert svc.today()["items"][0]["marked"] == "snooze"
    assert not svc.mark("NoSuch|metric|spike", "done")["ok"]


def test_scheduler_runs_once_a_day_after_report_time_and_respects_pause(svc):
    set_up(svc)
    svc.finish_setup()
    wait_job(svc)
    assert len(svc.inbox()) == 1
    svc.tick()  # same day: nothing new
    time.sleep(0.05)
    assert len(svc.inbox()) == 1 and not svc.job.running
    svc.now.t = datetime.fromisoformat("2026-11-14 08:00")  # before the 09:00 report time
    svc.tick()
    assert not svc.job.running
    svc.update_settings(paused=True)
    svc.now.t = datetime.fromisoformat("2026-11-14 09:30")
    svc.tick()
    assert not svc.job.running
    svc.update_settings(paused=False)
    svc.tick()
    wait_job(svc)
    assert len(svc.inbox()) == 2


def test_settings_validation(svc):
    assert not svc.update_settings(report_time="9am")["ok"]
    assert svc.update_settings(report_time="07:30")["ok"] and svc.state()["report_time"] == "07:30"
    assert not svc.update_settings(colour="red")["ok"]
    assert not svc.save_details(genre="Not a genre")["ok"]


def test_quiet_hours_wrap_midnight():
    def at(s):
        return datetime.fromisoformat("2026-11-13 " + s)

    assert in_quiet_hours(at("23:00"), "22:00", "08:00")
    assert in_quiet_hours(at("07:59"), "22:00", "08:00")
    assert not in_quiet_hours(at("12:00"), "22:00", "08:00")
    assert in_quiet_hours(at("12:00"), "11:00", "13:00")
    assert not in_quiet_hours(at("12:00"), "09:00", "09:00")


def test_forget_key_returns_to_setup(svc):
    set_up(svc)
    svc.finish_setup()
    wait_job(svc)
    svc.forget_key()
    assert config.load().api_key is None and not svc.state()["setup_done"]


def test_only_safe_links_open(svc):
    assert svc.open_url("https://create.roblox.com/dashboard/credentials")["ok"]
    assert not svc.open_url("https://evil.example/")["ok"]
    assert not svc.open_url("file:///etc/passwd")["ok"]


def test_agent_choice(svc, monkeypatch):
    monkeypatch.setattr(connect, "connect", lambda agent: (True, "Connected."))
    assert svc.connect_agent("claude-code")["ok"] and svc.settings()["agent"] == "claude-code"
    assert svc.connect_agent("none")["ok"] and svc.settings()["agent"] is None
    assert not svc.connect_agent("nope")["ok"]
    keys = [a["key"] for a in svc.agents()]
    assert set(keys) == {a.key for a in connect.AGENTS}


def test_discord_placeholder(svc):
    svc.accept_terms(True, "18plus")
    assert svc.set_discord("remind")["ok"] and svc.state()["discord"] == "remind"
    assert svc.state()["discord_available"] is False
    assert discord_link.mirror({"title": "x", "body": "y"}, svc.settings()) is False
    assert len(discord_link.dm_text("t", "x" * 5000)) <= discord_link.DM_LIMIT


def test_public_game_info_is_best_effort():
    pages = {
        "games": {"data": [{"name": "Cool Game", "genre_l1": "Simulation"}]},
        "thumbnails": {"data": [{"imageUrl": "https://tr.rbxcdn.com/abc/150/150/Image/Png"}]},
    }
    info = public_game_info(7, get=lambda url: pages["games" if "games.roblox" in url else "thumbnails"])
    assert info == {"name": "Cool Game", "genre": "Simulation", "icon": "https://tr.rbxcdn.com/abc/150/150/Image/Png"}
    bad = public_game_info(7, get=lambda url: {"data": [{"imageUrl": "https://evil.example/x.png"}]})
    assert "icon" not in bad

    def boom(url):
        raise OSError("offline")
    assert public_game_info(7, get=boom) == {}


def test_demo_service_accepts_any_key():
    s = Service(demo=True, notifier=lambda t, b: None, opener=lambda u: None)
    s.accept_terms(True, "18plus")
    s.check_key("anything-at-all-long-enough-123")
    r = s.connect_game("123")
    assert r["ok"] and "made-up" in r["game"]["name"]


# ---------------------------------------------------------------- server ---

@pytest.fixture
def running(svc):
    app = App(svc, token="T0K3N")
    httpd = start(app)
    yield app
    httpd.shutdown()


def call(app, path, body=None, token="T0K3N", host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server_needs_token_and_local_host(running):
    assert call(running, "/api/state", token=None)[0] == 403
    assert call(running, "/api/state", token="wrong")[0] == 403
    assert call(running, "/api/state", host="evil.example")[0] == 403
    code, body = call(running, "/api/state")
    assert code == 200 and json.loads(body)["setup_done"] is False
    assert call(running, "/api/nothing")[0] == 404


def test_server_serves_page_with_token_and_files(running):
    code, body = call(running, "/", token=None)
    assert code == 200 and b"T0K3N" in body and b"{{TOKEN}}" not in body
    for name in ("app.js", "app.css", "TERMS.md", "PRIVACY.md", "key-1.svg"):
        assert call(running, "/" + name, token=None)[0] == 200
    assert call(running, "/../config.json", token=None)[0] == 404
    assert call(running, "/secrets.env", token=None)[0] == 404


def test_server_full_setup_never_echoes_key(running):
    replies = [
        call(running, "/api/terms", {"agree": True, "age": "18plus"}),
        call(running, "/api/key", {"key": KEY}),
        call(running, "/api/game", {"link": GAME_LINK}),
        call(running, "/api/details", {"genre": "Simulation", "report_time": "08:00"}),
        call(running, "/api/finish", {}),
    ]
    for code, body in replies:
        assert code == 200 and json.loads(body)["ok"], body
        assert KEY.encode() not in body
    wait_job(running.service)
    for path in ("/api/state", "/api/today", "/api/inbox", "/api/export-settings", "/api/learned"):
        code, body = call(running, path)
        assert code == 200 and KEY.encode() not in body
    code, body = call(running, "/report", token=None)
    assert code == 200 and b"<html" in body.lower()


def test_terms_and_privacy_files_match_the_app_version():
    web = Path(service_mod.__file__).parent / "web"
    for name in ("TERMS.md", "PRIVACY.md"):
        assert service_mod.TERMS_VERSION in (web / name).read_text(encoding="utf-8")


# --------------------------------------------------------- agent connection ---

def test_packaged_app_points_agents_at_console_program(tmp_path, monkeypatch):
    exe = tmp_path / "RobloxAnalyticsAgent.exe"
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert connect.server_command() == (str(exe), ["mcp"])  # no console program next to it
    (tmp_path / connect.CLI_EXE).write_text("")
    assert connect.server_command() == (str(tmp_path / connect.CLI_EXE), ["mcp"])


def test_detect_agents(monkeypatch, tmp_path):
    monkeypatch.setattr(connect, "json_config_path", lambda a: tmp_path / a.key / "x.json")
    (tmp_path / "cursor").mkdir()
    found = connect.detect(which=lambda name: "/bin/claude" if name == "claude" else None)
    assert found["claude-code"] and found["cursor"]
    assert not found["codex"] and not found["windsurf"] and not found["other"]


def test_inbox_table_exists():
    conn = db.connect(":memory:")
    conn.execute("SELECT id, kind, status, title, body, read FROM inbox")
