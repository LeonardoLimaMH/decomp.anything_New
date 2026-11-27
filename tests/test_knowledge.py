"""What agents learn about a game reaches every agent once helpers running two different models agree on it."""

import json
import types

import pytest

from decompnet.app import tools
from decompnet.app.agents import Manager, Session
from decompnet.app.engine import RomNetwork
from decompnet.app.knowledge import model_key
from decompnet.network import NetworkError

MODELS = {"codex-1": "gpt-6.2-sol", "codex-2": "GPT-6.2-Sol", "antigravity-1": "gemini-4-neon-xhigh",
          "claude-1": "claude-opus-5-5"}


def _net(tmp_path, kind="rom"):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": kind}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status) VALUES(?,?,?,?,?)",
                   (pid, "func_80001000", 16, 4, "open"))
    net.knowledge.model_of = lambda agent: MODELS.get(agent.name)
    return net


def _agent(net, name):
    return net.local_agent(name)[0]


def test_the_same_model_however_it_is_spelled():
    assert model_key("anthropic/claude-opus-5-5") == model_key("opus") == model_key("claude-opus-5.5") == "claude-opus"
    assert model_key("gemini-4-neon-high") == model_key("gemini-4-neon-xhigh") == "gemini-4-neon"
    assert model_key("GPT-6.2-Sol") == model_key("gpt-6.2-sol")
    assert model_key("gpt-6.2-sol") != model_key("gemini-4-neon")


def test_two_sessions_of_one_model_are_one_opinion_and_two_models_make_it_known(tmp_path):
    net = _net(tmp_path)
    k = net.knowledge
    said = k.share(_agent(net, "codex-1"), "game", "Every actor update takes the Actor pointer in a0 and returns void.")
    assert "Proposed as #1" in said
    assert "only one model" in k.share(_agent(net, "codex-2"), "game", supports=1)  # same model, other session
    assert k.listing("game")["items"][0]["state"] == "open"
    assert "every agent gets it now" in k.share(_agent(net, "antigravity-1"), "game", supports=1)
    item = k.listing("game")["items"][0]
    assert item["state"] == "accepted" and item["models"] == ["gemini-4-neon", "gpt-6.2-sol"]
    prompt = k.for_prompt("game")
    assert "What agents on this game agree on" in prompt and "Actor pointer in a0" in prompt


def test_saying_the_same_thing_again_backs_the_proposal(tmp_path):
    net = _net(tmp_path)
    k = net.knowledge
    k.share(_agent(net, "codex-1"), "game", "The player struct D_8004AE34 keeps hp at 0x1C and the state at 0x22.")
    said = k.share(_agent(net, "claude-1"), "game", "Player struct D_8004AE34: hp at 0x1C, state at 0x22.")
    assert "#1" in said and len(k.listing("game")["items"]) == 1


def test_open_proposals_are_offered_for_backing_and_the_user_decides(tmp_path):
    net = _net(tmp_path)
    k = net.knowledge
    k.set_auto("game", False)
    k.share(_agent(net, "codex-1"), "game", "Menus are drawn by the functions in the text engine file, in order.")
    assert "#1 (gpt-6.2-sol)" in k.for_prompt("game") and "supports=N" in k.for_prompt("game")
    said = k.share(_agent(net, "antigravity-1"), "game", supports=1)
    assert "once the user accepts it" in said and k.listing("game")["items"][0]["state"] == "open"
    k.decide("game", 1, True)
    assert k.listing("game")["items"][0]["state"] == "accepted"
    k.decide("game", 1, False)  # taken back
    assert k.listing("game")["items"] == [] and k.for_prompt("game") == ""
    with pytest.raises(NetworkError):
        k.share(_agent(net, "claude-1"), "game", supports=1)  # rejected: can't be backed again


def test_turning_auto_on_adds_what_two_models_already_agree_on(tmp_path):
    net = _net(tmp_path)
    k = net.knowledge
    k.set_auto("game", False)
    k.share(_agent(net, "codex-1"), "game", "Score tables are arrays of u16 indexed by the card id.")
    k.share(_agent(net, "claude-1"), "game", supports=1)
    assert k.listing("game")["items"][0]["state"] == "open"
    k.set_auto("game", True)
    assert k.listing("game")["items"][0]["state"] == "accepted"


def test_compiler_questions_and_empty_proposals_are_refused(tmp_path):
    net = _net(tmp_path)
    with pytest.raises(NetworkError, match="compiler_matrix"):
        net.knowledge.share(_agent(net, "codex-1"), "game", "This file was built with GCC 2.7.2-cdk and -O2.")
    with pytest.raises(NetworkError):
        net.knowledge.share(_agent(net, "codex-1"), "game", "")
    with pytest.raises(NetworkError):
        net.knowledge.share(_agent(net, "codex-1"), "game", supports=99)


def test_an_agent_the_app_has_no_model_for_is_its_own_opinion(tmp_path):
    net = _net(tmp_path)
    k = net.knowledge
    k.share(_agent(net, "you"), "game", "Strings are Shift-JIS, two bytes per character.")
    assert k.listing("game")["items"][0]["models"] == ["agent:you"]


def test_add_note_needs_a_function_and_share_knowledge_is_a_tool(tmp_path):
    net = _net(tmp_path)
    agent = _agent(net, "codex-1")
    with pytest.raises(NetworkError, match="share_knowledge"):
        tools.HANDLERS["add_note"](net, agent, "game", text="something general")
    assert "Noted" in tools.HANDLERS["add_note"](net, agent, "game", text="tried s16", function="func_80001000")
    assert "Proposed" in tools.HANDLERS["share_knowledge"](net, agent, "game", text="Card ids are u8 everywhere.")
    assert "share_knowledge" in tools.tool_names(fork=True) and "share_knowledge" in tools.tool_names()
    assert "share_knowledge" not in tools.tool_names(analysis=True)


def test_analysis_projects_keep_project_wide_notes(tmp_path):
    net = _net(tmp_path, kind="analysis")
    agent = _agent(net, "codex-1")
    assert "Noted" in tools.HANDLERS["add_note"](net, agent, "game", text="the dropper unpacks to %TEMP%")


def test_a_removed_project_takes_its_knowledge_along(tmp_path):
    net = _net(tmp_path)
    net.knowledge.share(_agent(net, "codex-1"), "game", "Every actor update takes the Actor pointer in a0.")
    net.remove_project("game")
    assert net.store.all("SELECT * FROM knowledge") == [] and net.store.all("SELECT * FROM knowledge_votes") == []


def test_the_manager_says_which_model_a_session_runs(monkeypatch):
    from decompnet.app import agents

    monkeypatch.setattr(agents, "_config_model", lambda kind: "gpt-default" if kind == "codex" else None)
    m = Manager.__new__(Manager)
    m.sessions = {"codex-1": Session("codex-1", "codex", "c", "game", "codex-1", {}),
                  "claude-1": Session("claude-1", "claude", "c", "game", "claude-1", {"model": "opus"}),
                  "antigravity-1": Session("antigravity-1", "antigravity", "a", "game", "antigravity-1", {})}
    assert m.model_of(types.SimpleNamespace(name="codex-1")) == "gpt-default"
    assert m.model_of(types.SimpleNamespace(name="claude-1")) == "opus"
    assert m.model_of(types.SimpleNamespace(name="antigravity-1")) == "antigravity"  # no default known: the CLI
    assert m.model_of(types.SimpleNamespace(name="you")) is None
