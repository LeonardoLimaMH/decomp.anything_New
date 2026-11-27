"""AI accounts: who Claude Code, Codex and Antigravity helpers run as, signing more accounts in from the page, and
spreading the sessions over them."""

import base64
import json
import sys
import time

from decompnet.app import agents, aiaccounts


def _codex_auth(home, email, plan="plus"):
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan,
                                                               "chatgpt_subscription_active_until": "2026-12-27T14:22:18+00:00"}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": f"h.{body}.s"}}))


def _agy_auth(home, email):
    d = home / ".gemini" / "antigravity-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "antigravity-oauth-token").write_text(json.dumps({"token": "x", "auth_method": "oauth", "id_token_email": email}))


def test_who_an_account_is_comes_from_the_clis_own_files(tmp_path):
    _codex_auth(tmp_path / "c", "someone@example.com")
    assert aiaccounts.identity("codex", tmp_path / "c") == {"signed_in": True, "email": "someone@example.com",
                                                            "plan": "plus", "until": "2026-12-27T14:22:18+00:00"}
    _agy_auth(tmp_path / "g", "g@example.com")
    assert aiaccounts.identity("antigravity", tmp_path / "g")["email"] == "g@example.com"
    assert aiaccounts.identity("codex", tmp_path / "none") == {"signed_in": False}
    assert aiaccounts.identity("antigravity", tmp_path / "none") == {"signed_in": False}
    status = tmp_path / "claude_status.py"
    status.write_text('import json; print(json.dumps({"loggedIn": True, "email": "c@example.com", "subscriptionType": "pro"}))')
    who = aiaccounts.identity("claude", tmp_path / "cl", None, [sys.executable, str(status)])
    assert who == {"signed_in": True, "email": "c@example.com", "plan": "pro", "until": ""}


def test_sessions_are_spread_over_the_accounts_switched_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "main"))
    _codex_auth(tmp_path / "main", "main@example.com")
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    _codex_auth(tmp_path / "accounts" / "codex" / "a1", "second@example.com")
    _codex_auth(tmp_path / "accounts" / "codex" / "a2", "third@example.com")
    assert acc.ids("codex") == ["main", "a1", "a2"] and acc.ids("claude") == ["main"]
    assert acc.pick("codex", {}) == "main"
    assert acc.pick("codex", {"main": 1}) == "a1"
    assert acc.pick("codex", {"main": 1, "a1": 1}) == "a2"
    acc.set_enabled("codex", "a1", False)
    assert acc.pick("codex", {"main": 1}) == "a2" and acc.usable("codex") == ["main", "a2"]
    assert acc.label("codex", "a2") == "third"
    acc.remove("codex", "a2")
    assert acc.ids("codex") == ["main", "a1"] and acc.usable("codex") == ["main"]
    assert acc.label("codex", "main") == ""  # one account in use: nothing to tell apart


def test_an_added_account_gets_the_main_settings_and_its_own_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "main"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("DECOMP_HOME", raising=False)
    _codex_auth(tmp_path / "main", "main@example.com")
    (tmp_path / "main" / "config.toml").write_text('model = "gpt-6.2-sol"\n')
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    assert acc.env("codex", "main") == {} and acc.env("codex", None) == {}
    assert acc.env("codex", "a1") == {}  # not signed in
    _codex_auth(tmp_path / "accounts" / "codex" / "a1", "second@example.com")
    assert acc.env("codex", "a1") == {"CODEX_HOME": str(tmp_path / "accounts" / "codex" / "a1")}
    assert (tmp_path / "accounts" / "codex" / "a1" / "config.toml").read_text() == 'model = "gpt-6.2-sol"\n'
    # Antigravity keeps everything under ~/.gemini: another HOME, and the decomp bridge told where the app is
    main_agy = tmp_path / "home" / ".gemini" / "antigravity-cli"
    main_agy.mkdir(parents=True)
    (main_agy / "settings.json").write_text('{"permissions": {"allow": ["mcp(decomp/*)"]}}')
    _agy_auth(tmp_path / "accounts" / "antigravity" / "a1", "g@example.com")
    env = acc.env("antigravity", "a1")
    assert env["HOME"] == str(tmp_path / "accounts" / "antigravity" / "a1")
    assert env["DECOMP_HOME"] == str(tmp_path / "home" / "Decomp")
    copied = tmp_path / "accounts" / "antigravity" / "a1" / ".gemini" / "antigravity-cli" / "settings.json"
    assert "mcp(decomp/*)" in copied.read_text()


def test_sessions_run_on_their_account_and_codex_gets_a_lower_case_model(tmp_path, monkeypatch):
    """Codex refuses "GPT-6.2-Sol" as typed in the page: OpenAI's model ids are lower case."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "main"))
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(agents, "_which", lambda cmd: str(exe))
    m = agents.Manager.__new__(agents.Manager)
    m.mcp_url = "http://127.0.0.1:6464/mcp"
    m.prompt = lambda s: "go"
    m.accounts = aiaccounts.Accounts(tmp_path / "accounts")
    _codex_auth(tmp_path / "accounts" / "codex" / "a1", "second@example.com")
    s = agents.Session("codex-1", "codex", "c", "demo", "codex-1", {"model": "GPT-6.2-Sol", "account": "a1"})
    (tmp_path / "w").mkdir()
    cmd, env, _ = m._command(s, "tok", tmp_path / "w")
    assert cmd[cmd.index("-m") + 1] == "gpt-6.2-sol"
    assert env["CODEX_HOME"] == str(tmp_path / "accounts" / "codex" / "a1")
    s2 = agents.Session("codex-2", "codex", "c", "demo", "codex-2", {"account": "main"})
    _, env2, _ = m._command(s2, "tok", tmp_path / "w")
    assert env2.get("CODEX_HOME") == str(tmp_path / "main")  # the CLI's own sign-in: left as it was


DEVICE = r'''
import json, os, pathlib, sys, time
print("Follow these steps to sign in with ChatGPT using device code authorization:")
print("1. Open this link in your browser and sign in to your account")
print("   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m")
print("2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m")
print("   \x1b[94mAB12-CDE34\x1b[0m", flush=True)
time.sleep(0.5)
if os.environ.get("FAKE_FAIL"):
    print("Error: device code expired"); sys.exit(1)
(pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json").write_text("{}")
print("Successfully logged in")
'''

PASTE = r'''
import os, pathlib, sys
url = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
sys.stdout.write("Opening browser to sign in…\nIf the browser didn't open, visit: \x1b]8;;" + url + "\x1b\\" + url + "\x1b]8;;\x1b\\\n")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
code = sys.stdin.readline().strip()
if code != "the-code":
    print("Error: invalid code"); sys.exit(1)
(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text("{}")
print("Login successful.")
'''


def _wait(acc):
    for _ in range(100):
        if acc.login.state not in ("starting", "waiting", "checking"):
            return
        time.sleep(0.1)


def test_a_new_codex_account_signs_in_with_a_device_code(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "main"))
    (tmp_path / "fake.py").write_text(DEVICE)
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    acc.exes["codex"] = [sys.executable, str(tmp_path / "fake.py")]
    shown = acc.start_login("codex")
    assert shown["url"] == "https://auth.openai.com/codex/device" and shown["code"] == "AB12-CDE34"
    assert shown["account"] == "a1" and not shown["paste"]
    _wait(acc)
    assert acc.login.state == "done" and acc.ids("codex") == ["main", "a1"]


def test_a_claude_account_signs_in_with_a_pasted_code(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "main"))
    (tmp_path / "fake.py").write_text(PASTE)
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    acc.exes["claude"] = [sys.executable, str(tmp_path / "fake.py")]
    shown = acc.start_login("claude")
    assert shown["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=abc" and shown["paste"]
    assert shown["state"] == "waiting"
    acc.submit_code("the-code")
    _wait(acc)
    assert acc.login.state == "done" and acc.ids("claude") == ["main", "a1"]


def test_a_sign_in_that_fails_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "main"))
    (tmp_path / "fake.py").write_text(PASTE)
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    acc.exes["claude"] = [sys.executable, str(tmp_path / "fake.py")]
    acc.start_login("claude")
    acc.submit_code("wrong")
    _wait(acc)
    assert acc.login.state == "failed" and "invalid" in acc.login.error
    assert acc.ids("claude") == ["main"]


def test_usage_and_names_are_kept_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "main"))
    _codex_auth(tmp_path / "main", "main@example.com")
    acc = aiaccounts.Accounts(tmp_path / "accounts")
    _codex_auth(tmp_path / "accounts" / "codex" / "a1", "second@example.com")
    acc.record("codex", "a1", session=True)
    acc.record("codex", "a1", usd=0.5, tokens=1200)
    acc.record("codex", None, tokens=300)  # the main account
    acc.record("api", "x", tokens=5)  # not a managed CLI: nothing kept
    acc.flush()
    again = aiaccounts.Accounts(tmp_path / "accounts")  # a restart keeps it
    assert again.usage("codex", "a1")["tokens"] == 1200 and again.usage("codex", "a1")["sessions"] == 1
    assert again.usage("codex", "a1")["usd"] == 0.5 and again.usage("codex", "main")["tokens"] == 300
    acc.rename("codex", "a1", "  work   account ")
    rows = {a["id"]: a for a in acc.list("codex")}
    assert rows["a1"]["name"] == "work account" and rows["a1"]["usage"]["tokens"] == 1200
    assert acc.label("codex", "a1") == "work account"  # its helpers say which account they run on
    acc.remove("codex", "a1")
    _codex_auth(tmp_path / "accounts" / "codex" / "a1", "someone-else@example.com")  # the number comes back
    rows = {a["id"]: a for a in acc.list("codex")}
    assert rows["a1"]["name"] == "" and rows["a1"]["usage"]["tokens"] == 0  # nothing of the old one carries over


def test_a_session_counts_for_its_account(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "main"))
    m = agents.Manager.__new__(agents.Manager)
    m.accounts = aiaccounts.Accounts(tmp_path / "accounts")
    m.net = type("N", (), {"add_spend": lambda self, *a: None})()
    s = agents.Session("codex-1", "codex", "c", "demo", "codex-1", {"account": "main"})
    m._account(s, {"tokens": 700, "usd": 0.1})
    assert m.accounts.usage("codex", "main")["tokens"] == 700 and s.tokens == 700
