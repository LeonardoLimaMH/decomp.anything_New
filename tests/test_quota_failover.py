"""Helpers and their accounts: picking the account in the launcher, moving on when an account runs out of quota
(another account, or another provider), and carrying on by themselves when the quota comes back."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from decompnet.app import agents, aiaccounts
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build

PORT, SECRET = 6498, "s3cret-quota"
W = {"X-Decomp": "1"}


def _codex_auth(home, email):
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": f"h.{body}.s"}}))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-main"))
    _codex_auth(tmp_path / "codex-main", "main@example.com")
    (tmp_path / ".decomp").mkdir()
    net = RomNetwork(tmp_path / ".decomp" / "decomp.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                   ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom"})))
    mcp, manager, security = build(net, tmp_path / ".decomp", PORT, SECRET)
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"),
                        base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("decomp_live", SECRET)
    _codex_auth(tmp_path / ".decomp" / "accounts" / "codex" / "a1", "second@example.com")
    started = []
    monkeypatch.setattr(manager, "start", lambda kind, slug, count=1, options=None: started.append((kind, options)) or ["x-1"])
    return client, net, manager, started


def test_the_launcher_can_pick_a_signed_in_account(app):
    client, _, _, started = app
    r = client.post("/api/agents/start", json={"kind": "codex", "slug": "game", "count": 1,
                                               "options": {"account": "a1"}}, headers=W)
    assert r.status_code == 200 and started[-1][1]["account"] == "a1"
    r = client.post("/api/agents/start", json={"kind": "codex", "slug": "game", "count": 1,
                                               "options": {"account": "a7"}}, headers=W)
    assert r.status_code >= 400 and "isn't signed in" in r.text
    client.post("/api/agents/start", json={"kind": "codex", "slug": "game", "count": 1, "options": {"account": ""}}, headers=W)
    assert "account" not in started[-1][1]  # automatic


def test_each_session_says_which_account_it_runs_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-main"))
    _codex_auth(tmp_path / "codex-main", "main@example.com")
    m = agents.Manager.__new__(agents.Manager)
    m.accounts = aiaccounts.Accounts(tmp_path / "accounts")
    _codex_auth(tmp_path / "accounts" / "codex" / "a1", "second@example.com")
    m.accounts.rename("codex", "a1", "work")
    m.sessions = {"codex-1": agents.Session("codex-1", "codex", "Codex #1", "game", "codex-1", {"account": "a1"}),
                  "codex-2": agents.Session("codex-2", "codex", "Codex #2", "game", "codex-2", {}),
                  "api-1": agents.Session("api-1", "api", "API #1", "game", "api-1", {})}
    rows = {r["id"]: r for r in m.snapshot()}
    assert rows["codex-1"]["account"] == "a1" and rows["codex-1"]["account_label"] == "work · second@example.com"
    assert rows["codex-2"]["account"] == "main" and rows["codex-2"]["account_label"] == "main@example.com"
    assert "account" not in rows["api-1"]
