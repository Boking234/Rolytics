"""Game context file + agent-planned funnels: only missing funnels get created, tokens stay small."""

from __future__ import annotations

import json
import subprocess

import pytest

from roblox_agent import config, funnels
from roblox_agent.mcp_server import Server

GAME = {"data": [{"name": "Lava Tower", "genre_l1": "Obby & Platformer", "genre_l2": "Tower Obby",
                  "description": "Climb   the tower!\nBuy skips in the shop. " + "x" * 900, "maxPlayers": 12}]}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    return tmp_path / "home"


def settings(**extra):
    return {"universe_id": 42, "genre": "Obby & Platformer", "systems": "stages, shop", "agent": "claude-code",
            **extra}


def test_context_file_is_saved_and_small():
    _ctx, changed = funnels.refresh_context(settings(), get=lambda url: GAME)
    assert changed
    saved = json.loads(funnels.context_path().read_text())
    assert saved["name"] == "Lava Tower" and saved["subgenre"] == "Tower Obby"
    assert len(saved["description"]) <= funnels.MAX_DESCRIPTION and "  " not in saved["description"]
    text = funnels.context_text()
    assert "Lava Tower" in text and "Funnels already there: none" in text
    assert len(text) <= funnels.MAX_CONTEXT_CHARS  # ~300 tokens
    _, again = funnels.refresh_context(settings(), get=lambda url: GAME)
    assert not again  # same game, nothing to re-plan


def test_offline_falls_back_to_settings():
    ctx, _ = funnels.refresh_context(settings(game_name="My Obby"), offline=True)
    assert ctx["name"] == "My Obby" and ctx["genre"] == "Obby & Platformer"


PROPOSAL = [
    {"name": "Onboarding", "kind": "onboarding", "steps": ["Spawned", "Touched first checkpoint", "Reached stage 5"]},
    {"name": "Shop purchase", "steps": ["Opened shop", "Viewed skip", "Prompted purchase", "Bought"],
     "why": "Skips are the main revenue."},
]


def test_add_creates_only_missing_funnels():
    r = funnels.add(PROPOSAL)
    assert r["created"] == ["Onboarding", "Shop purchase"]
    again = funnels.add(PROPOSAL + [{"name": "Shop Funnel", "steps": ["Opened shop", "Viewed skip",
                                                                      "Prompted purchase", "Bought"]}])
    assert again["created"] == [] and len(again["skipped"]) == 3  # same name or same steps
    assert [f["name"] for f in funnels.load()] == ["Onboarding", "Shop purchase"]


def test_bad_funnels_are_rejected_and_names_cleaned():
    r = funnels.add([{"name": "One step", "steps": ["Only"]}, "nope",
                     {"name": "Rebirth<script>", "steps": ["Hit cap", "Rebirth!", "Bought boost"]}])
    assert r["created"] == ["Rebirthscript"]
    assert len(r["rejected"]) == 2
    assert funnels.load()[0]["steps"][1] == "Rebirth"


def test_cap_on_number_of_funnels():
    many = [{"name": f"F{i}", "steps": [f"a{i}", f"b{i}"]} for i in range(12)]
    r = funnels.add(many)
    assert len(r["created"]) == funnels.MAX_NEW_PER_RUN


def test_luau_module_uses_roblox_funnel_events():
    funnels.add(PROPOSAL)
    lua = funnels.luau_path().read_text()
    assert "LogOnboardingFunnelStepEvent" in lua and "LogFunnelStepEvent" in lua
    assert '["Shop purchase"] = { kind = "recurring", steps = { "Opened shop"' in lua


def test_code_scan_finds_systems_and_existing_funnels(tmp_path):
    code = tmp_path / "game" / "src"
    code.mkdir(parents=True)
    (code / "Shop.server.luau").write_text(
        'local MarketplaceService = game:GetService("MarketplaceService")\n'
        'AnalyticsService:LogFunnelStepEvent(player, "Shop purchase", id, 1, "Opened shop")\n')
    (code / "Intro.server.lua").write_text('AnalyticsService:LogOnboardingFunnelStepEvent(player, 1, "Spawned")\n')
    funnels.write_luau([{"name": "X", "kind": "recurring", "steps": ["a", "b"]}])
    (code / "FunnelTracker.lua").write_text(funnels.luau_path().read_text())  # our own module is ignored
    ctx, _ = funnels.refresh_context(settings(code_folder=str(tmp_path / "game")), offline=True)
    assert "shop" in ctx["systems_in_code"]
    assert set(ctx["funnels_in_code"]) == {"Shop purchase", "Onboarding"}
    assert {f["name"]: f["status"] for f in funnels.load()} == {"Shop purchase": "in_game", "Onboarding": "in_game"}
    assert "Shop purchase" in funnels.context_text()
    assert funnels.add(PROPOSAL)["created"] == []  # already in the game: nothing new


class FakeAgent:
    """Stands in for `claude -p`: plans funnels through the same MCP tools a real agent uses."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        srv = Server(conn=None, universe_id=42)
        assert "Lava Tower" in srv.game_context()
        out = srv.add_funnels(funnels=PROPOSAL)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def test_plan_runs_agent_only_when_context_changes():
    agent = FakeAgent()
    which = lambda name: "/usr/bin/" + name
    msg = funnels.plan(settings(), run=agent, which=which, get=lambda url: GAME)
    assert "Created: Onboarding, Shop purchase" in msg
    assert agent.calls[0][:2] == ["claude", "-p"] and "mcp__roblox-agent__add_funnels" in agent.calls[0][-1]
    msg = funnels.plan(settings(), run=agent, which=which, get=lambda url: GAME)
    assert len(agent.calls) == 1 and "unchanged" in msg  # no tokens spent when nothing changed
    funnels.plan(settings(systems="stages, shop, pets"), run=agent, which=which, get=lambda url: GAME)
    assert len(agent.calls) == 2


def test_agents_without_headless_mode_get_the_prompt():
    ok, msg = funnels.ask_agent("cursor", which=lambda n: None)
    assert not ok and "plan-funnels" in msg
    assert funnels.agent_command("codex", which=lambda n: "/x")[:2] == ["codex", "exec"]


def test_mcp_lists_tools_and_prompt():
    srv = Server(conn=None, universe_id=42)
    names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "game_context" in names and "add_funnels" in names
    got = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "plan-funnels"}})
    assert "add_funnels" in got["result"]["messages"][0]["content"]["text"]
    res = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "add_funnels", "arguments": {"funnels": PROPOSAL}}})
    assert "Created: Onboarding, Shop purchase" in res["result"]["content"][0]["text"]


def test_tool_list_stays_small():
    from roblox_agent.mcp_server import TOOLS
    assert len(json.dumps(TOOLS)) < 2400  # ~600 tokens for all tools, once per session


def test_step_hints_kept_as_a_map():
    funnels.add([{"name": "Shop", "steps": ["Open", "Buy"], "trigger_hints": {"Open": "ShopButton click", "Buy": "x" * 500}}])
    where = funnels.load()[0]["where"]
    assert where["Open"] == "ShopButton click" and len(where["Buy"]) == 120
