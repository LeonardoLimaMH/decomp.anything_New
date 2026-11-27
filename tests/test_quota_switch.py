"""Out of quota: a helper carries on on another account of its provider, or a helper of another provider stands in,
and it carries on by itself (or when the user says) once the quota comes back."""

import json
import sys
import threading
import time

import pytest

from decompnet.app import agents, aiaccounts
from decompnet.app.agenttools import OutOfQuota, no_credit, quota_out
from decompnet.app.engine import RomNetwork

from .test_quota_failover import W, _codex_auth, app  # noqa: F401  (the app fixture)

NOON = time.mktime((2026, 11, 27, 12, 0, 0, 0, 0, -1))


def test_what_each_provider_says_when_its_quota_is_spent():
    codex = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
             "https://chatgpt.com/settings/usage to purchase more credits or try again at 4:22 PM.")
    msg, until = quota_out(codex, NOON)
    assert "usage limit" in msg and time.localtime(until)[3:5] == (16, 22) and until > NOON
    agy = "error: Individual quota reached. Please upgrade your subscription. Resets in 2h48m11s."
    assert quota_out(agy, NOON)[1] == NOON + 2 * 3600 + 48 * 60 + 11
    assert quota_out("Claude AI usage limit reached|1764273600", NOON)[1] == 1764273600.0
    assert time.localtime(quota_out("5-hour limit reached ∙ resets 3pm", NOON)[1])[3] == 15
    early = quota_out("You've hit your usage limit ... try again at 9:05 AM.", NOON)[1]  # past today: tomorrow
    assert early - NOON > 20 * 3600
    assert quota_out("RESOURCE_EXHAUSTED: quota exhausted", NOON) == ("RESOURCE_EXHAUSTED: quota exhausted", None)
    assert quota_out("Error: too many requests (429)", NOON) is None  # a throttle, not the quota
    assert quota_out("insufficient_quota", NOON) is None and no_credit("insufficient_quota")  # an API bill


def _accounts(tmp_path, monkeypatch, extra=("a1",)):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-main"))
    _codex_auth(tmp_path / "codex-main", "main@example.com")
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    for a in extra:
        _codex_auth(tmp_path / "accounts" / "codex" / a, f"{a}@example.com")
    return acc


def test_an_account_out_of_quota_is_left_alone_until_it_comes_back(tmp_path, monkeypatch):
    acc = _accounts(tmp_path, monkeypatch)
    acc.mark_out("codex", "main", time.time() + 600)
    assert acc.usable("codex") == ["a1"] and acc.with_quota("codex", {}) == "a1"
    assert acc.with_quota("codex", {}, exclude="a1") is None
    assert {a["id"]: bool(a["quota_until"]) for a in acc.list("codex")} == {"main": True, "a1": False}
    acc.mark_back("codex", "main")
    assert acc.usable("codex") == ["main", "a1"]
    acc.mark_out("codex", "a1", time.time() - 1)  # already over: as if never out
    assert acc.quota_until("codex", "a1") is None


def _manager(tmp_path, monkeypatch, extra=("a1",), installed=("codex", "claude")):
    acc = _accounts(tmp_path, monkeypatch, extra)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom"}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status) VALUES(?,?,?,?,?)",
                   (pid, "func_80001000", 16, 4, "open"))
    m = agents.Manager(net, tmp_path / "home", "http://127.0.0.1:1/mcp")
    m.accounts = acc
    m.persist = lambda running: None
    m._run = lambda s: None  # sessions don't run here: the quota paths are called directly
    monkeypatch.setattr(agents, "_which", lambda cmd: f"/bin/{cmd}" if cmd in installed else None)
    return m, net


def _claim(net, s):
    agent, _ = net.local_agent(s.agent_name)
    fid = net.store.one("SELECT id FROM functions WHERE name='func_80001000'")["id"]
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, time.time(), time.time() + 600))
    return agent


def _in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("go", fn()))
    t.start()
    return t, out


def test_out_of_quota_carries_on_on_another_account_of_the_provider(tmp_path, monkeypatch):
    m, net = _manager(tmp_path, monkeypatch)
    s = m.sessions[m.start("codex", "game", 1, {"account": "main"})[0]]
    agent = _claim(net, s)
    assert m._out_of_quota(s, agent, OutOfQuota("You've hit your usage limit", time.time() + 900))
    assert s.options["account"] == "a1" and "carrying on on a1@example.com" in s.feed[-1]["text"]
    assert m.accounts.quota_until("codex", "main") and s.options["resume_function"] == "func_80001000"
    net.overview = lambda slug: {"kind": "rom", "title": "Game", "compiler": {"toolchain": "gcc"}, "platform": "n64"}
    net.intake_line = net.instructions = net.references = lambda slug: ""
    assert "carry on with func_80001000" in m.prompt(s) and "resume_function" not in s.options


def test_with_no_account_left_another_provider_stands_in_and_steps_back(tmp_path, monkeypatch):
    m, net = _manager(tmp_path, monkeypatch, extra=())
    claude = tmp_path / "claude-main"
    claude.mkdir()
    (claude / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    real = aiaccounts.identity
    monkeypatch.setattr(aiaccounts, "identity", lambda kind, home, env=None, exe=None:
                        real(kind, home) if kind != "claude" else {"signed_in": True, "email": "c@example.com"})
    m.helper_model = lambda kind: "opus" if kind == "claude" else None
    sid = m.start("codex", "game", 1, {})[0]
    s = m.sessions[sid]
    s.state = "working"
    agent = _claim(net, s)
    t, out = _in_thread(lambda: m._out_of_quota(s, agent, OutOfQuota("usage limit", time.time() + 1.5)))
    for _ in range(60):
        if s.options.get("standin"):
            break
        time.sleep(0.05)
    stand = m.sessions[s.options["standin"]]
    assert stand.kind == "claude" and stand.options["model"] == "opus" and stand.options["standin_for"] == sid
    assert stand.options["resume_function"] == "func_80001000"  # it carries on with the same function
    assert net.store.one("SELECT COUNT(*) AS n FROM claims WHERE agent_id=?", agent.id)["n"] == 0  # released for it
    assert s.state == "quota" and s.until
    assert {r["id"]: r for r in m.snapshot()}[sid]["standin"] == stand.id
    assert [r["kind"] for r in m.running()].count("claude") == 0  # stand-ins aren't resumed after a restart
    stand.state = "working"
    t.join(10)
    assert out["go"] is True and s.until == 0 and stand.stop.is_set()  # the quota came back: the stand-in stops
    assert "standin" not in s.options


def test_waiting_for_the_quota_by_itself_or_for_the_user(tmp_path, monkeypatch):
    m, net = _manager(tmp_path, monkeypatch, extra=(), installed=("codex",))
    sid = m.start("codex", "game", 1, {"auto_resume": False})[0]
    s = m.sessions[sid]
    agent, _ = net.local_agent(s.agent_name)
    t, out = _in_thread(lambda: m._out_of_quota(s, agent, OutOfQuota("usage limit", time.time() + 0.2)))
    time.sleep(1.5)
    assert t.is_alive() and s.state == "quota"  # its quota is back, but this one waits for the user
    assert [r for r in m.running() if r["options"].get("auto_resume") is False]  # kept across a restart, waiting
    m.resume_now(sid)
    t.join(5)
    assert out["go"] is True
    # turned on later: it carries on by itself
    t, out = _in_thread(lambda: m._out_of_quota(s, agent, OutOfQuota("usage limit", time.time() + 0.3)))
    time.sleep(0.8)
    m.set_auto_resume(sid, True)
    t.join(5)
    assert out["go"] is True
    # stopped while it waits: it stops
    t, out = _in_thread(lambda: m._out_of_quota(s, agent, OutOfQuota("usage limit", time.time() + 60)))
    time.sleep(0.3)
    m.stop(sid)
    t.join(5)
    assert out["go"] is False


def test_a_stand_in_whose_provider_runs_out_too_stops(tmp_path, monkeypatch):
    m, net = _manager(tmp_path, monkeypatch, extra=())
    s = m.sessions[m.start("codex", "game", 1, {"standin_for": "claude-9"})[0]]
    agent, _ = net.local_agent(s.agent_name)
    assert m._out_of_quota(s, agent, OutOfQuota("usage limit", time.time() + 60)) is False
    assert s.state == "error" and "ran out of quota too" in s.error


def test_the_policy_can_turn_the_switches_off(tmp_path, monkeypatch):
    m, net = _manager(tmp_path, monkeypatch)
    m.quota_policy = lambda: {"account": False, "provider": False}
    s = m.sessions[m.start("codex", "game", 1, {"account": "main"})[0]]
    agent, _ = net.local_agent(s.agent_name)
    t, out = _in_thread(lambda: m._out_of_quota(s, agent, OutOfQuota("usage limit", time.time() + 0.3)))
    t.join(5)
    assert out["go"] is True and s.options["account"] == "main" and "standin" not in s.options  # it waited


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script stands in for the Codex CLI")
def test_the_codex_cli_saying_usage_limit_is_out_of_quota(tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\ncat > /dev/null\necho \"ERROR: You've hit your usage limit. try again at 4:22 PM.\" >&2\n"
                   "exit 1\n")
    exe.chmod(0o755)
    m, _ = _manager(tmp_path, monkeypatch, extra=())
    monkeypatch.setattr(agents, "_which", lambda cmd: str(exe))
    monkeypatch.setattr(agents, "_codex_config", dict)
    m.prompt = lambda s: "go"
    s = agents.Session("codex-1", "codex", "c", "game", "codex-1", {})
    (tmp_path / "w").mkdir()
    with pytest.raises(OutOfQuota) as e:
        m._run_cli(s, "tok", tmp_path / "w")
    assert e.value.until and time.localtime(e.value.until)[3:5] == (16, 22)


def test_the_page_routes_for_the_policy_and_each_helper(app):  # noqa: F811
    client, _, manager, _ = app
    r = client.post("/api/ai/accounts", json={"action": "policy", "account": True, "provider": False}, headers=W)
    assert r.status_code == 200 and r.json()["policy"] == {"account": True, "provider": False}
    assert manager.quota_policy() == {"account": True, "provider": False}
    s = agents.Session("codex-7", "codex", "c", "game", "codex-7", {})
    manager.sessions["codex-7"] = s
    assert client.post("/api/agents/session", json={"id": "codex-7", "auto_resume": False}, headers=W).status_code == 200
    assert s.options["auto_resume"] is False
    client.post("/api/agents/session", json={"id": "codex-7", "action": "resume"}, headers=W)
    assert s.resume.is_set()
    assert client.post("/api/agents/session", json={"id": "nobody"}, headers=W).status_code >= 400
    assert client.post("/api/ai/accounts", json={"action": "quota_back", "kind": "codex", "id": "a1"},
                       headers=W).status_code == 200
