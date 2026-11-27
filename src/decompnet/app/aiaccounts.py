"""The accounts the helper agents run on: Claude Code, Codex and Antigravity CLI.

Each agent CLI keeps its sign-in in a folder of its own, and each one can be pointed at another folder: Codex with
CODEX_HOME, Claude Code with CLAUDE_CONFIG_DIR, Antigravity CLI with HOME (it keeps everything under ~/.gemini). The
user's own sign-in is the "main" account; more accounts are added from the Agents page, each in a folder under the
app's home, with the main account's settings copied in (MCP servers, permissions, model) so only the sign-in
differs. Helper sessions are spread over the accounts switched on, the least busy first, so several subscriptions
share the work and one account's usage limit doesn't stop the others.

Signing in runs the CLI's own login with the account's folder: Codex shows a link and a code to type there (device
code); Claude Code and Antigravity show a link, and the code their page gives back is pasted into ours. Nothing is
read from or sent anywhere else: who an account is (e-mail, plan) comes from the CLI's own files or `auth status`.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAIN = "main"
_ESC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")  # OSC (hyperlinks) and CSI codes
_URL = re.compile(r"https://\S+")
_DEVICE_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")


@dataclass(frozen=True)
class Provider:
    kind: str
    name: str
    env: str  # the variable that points the CLI at an account's folder
    copy: tuple[str, ...]  # settings carried over from the main account (relative to its folder)
    login: tuple[str, ...]  # the CLI's sign-in command
    paste: bool  # the user pastes a code back (otherwise: types the shown code on the provider's page)
    token: str  # the file whose presence means "signed in" (relative to the folder)

    def main_home(self) -> Path:
        if self.kind == "codex":
            return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        if self.kind == "claude":
            return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return Path.home()


PROVIDERS = {
    "claude": Provider("claude", "Claude Code", "CLAUDE_CONFIG_DIR", ("settings.json",), ("auth", "login"), True,
                       ".credentials.json"),
    "codex": Provider("codex", "Codex", "CODEX_HOME", ("config.toml",), ("login", "--device-auth"), False, "auth.json"),
    "antigravity": Provider("antigravity", "Antigravity CLI", "HOME",
                            (".gemini/antigravity-cli/settings.json", ".gemini/antigravity-cli/mcp"), ("models",), True,
                            ".gemini/antigravity-cli/antigravity-oauth-token"),
}


def _jwt_claims(tok) -> dict:
    if isinstance(tok, dict):
        tok = tok.get("raw_jwt")
    if not isinstance(tok, str) or tok.count(".") < 2:
        return {}
    part = tok.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return {}


def identity(kind: str, home: Path, env: dict | None = None, exe: str | list | None = None) -> dict:
    """Who an account's folder is signed in as: {"signed_in", "email", "plan", "until"}."""
    p = PROVIDERS[kind]
    if kind == "codex":
        try:
            auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"signed_in": False}
        claims = _jwt_claims((auth.get("tokens") or {}).get("id_token"))
        info = claims.get("https://api.openai.com/auth") or {}
        return {"signed_in": True, "email": claims.get("email") or ("API key" if auth.get("OPENAI_API_KEY") else ""),
                "plan": info.get("chatgpt_plan_type") or "", "until": info.get("chatgpt_subscription_active_until") or ""}
    if kind == "antigravity":
        try:
            tok = json.loads((home / p.token).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"signed_in": False}
        return {"signed_in": True, "email": tok.get("id_token_email") or "", "plan": "", "until": ""}
    # Claude Code: `claude auth status` says it (the account's e-mail lives in .claude.json, beside or inside the folder)
    if exe:
        try:
            r = subprocess.run([*_cmd(exe), "auth", "status"], env=env, capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            d = json.loads(r.stdout)
            if not d.get("loggedIn"):
                return {"signed_in": False}
            return {"signed_in": True, "email": d.get("email") or "", "plan": d.get("subscriptionType") or "", "until": ""}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {"signed_in": (home / p.token).exists(), "email": "", "plan": "", "until": ""}


def _cmd(exe) -> list[str]:
    return [*exe] if isinstance(exe, (list, tuple)) else [str(exe)]


class Login:
    """A sign-in in progress: the link, the code (to type there or to paste here) and how it ended."""

    def __init__(self, kind: str, account: str, new: bool):
        self.kind, self.account, self.new = kind, account, new
        self.url = self.code = self.error = ""
        self.paste = PROVIDERS[kind].paste
        self.state = "starting"  # starting | waiting | checking | done | failed
        self.proc: subprocess.Popen | None = None

    def snapshot(self) -> dict:
        return {"kind": self.kind, "account": self.account, "state": self.state, "url": self.url, "code": self.code,
                "paste": self.paste, "error": self.error}


class Accounts:
    def __init__(self, root: Path, which=shutil.which):
        self.root = root  # <app home>/accounts/<kind>/<account>
        self.which = which  # how the CLIs are found (the app's own lookup)
        self.exes: dict[str, str | list] = {}  # set by tests: an interpreter and a script
        self.login: Login | None = None
        self._lock = threading.Lock()
        self._who: dict[tuple[str, str], tuple[float, dict]] = {}
        self._usage: dict | None = None  # kind -> account -> {tokens, usd, sessions, last}: usage.json
        self._usage_saved = 0.0
        self._usage_lock = threading.Lock()

    # -- usage per account (what each subscription's helpers spent), and the names the user gives accounts
    def _usage_data(self) -> dict:
        if self._usage is None:
            try:
                self._usage = json.loads((self.root / "usage.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._usage = {}
        return self._usage

    def _save_usage(self, force: bool = False) -> None:
        if not force and time.time() - self._usage_saved < 10:  # a session reports often: at most every 10 s
            return
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "usage.json.tmp"
        tmp.write_text(json.dumps(self._usage_data(), indent=1), encoding="utf-8")
        tmp.replace(self.root / "usage.json")
        self._usage_saved = time.time()

    def record(self, kind: str, account: str | None, usd: float = 0.0, tokens: int = 0, session: bool = False) -> None:
        """Add what a helper session on `account` spent (or that one started) to the account's usage."""
        if kind not in PROVIDERS:
            return
        with self._usage_lock:
            row = self._usage_data().setdefault(kind, {}).setdefault(account or MAIN, {})
            row["tokens"] = int(row.get("tokens", 0)) + int(tokens or 0)
            row["usd"] = round(float(row.get("usd", 0.0)) + float(usd or 0.0), 6)
            row["sessions"] = int(row.get("sessions", 0)) + (1 if session else 0)
            row["last"] = time.time()
            with contextlib.suppress(OSError):
                self._save_usage(force=session)

    def flush(self) -> None:
        with self._usage_lock:
            if self._usage is not None:
                with contextlib.suppress(OSError):
                    self._save_usage(force=True)

    def usage(self, kind: str, account: str) -> dict:
        with self._usage_lock:
            row = dict((self._usage_data().get(kind) or {}).get(account) or {})
        return {"tokens": int(row.get("tokens", 0)), "usd": float(row.get("usd", 0.0)),
                "sessions": int(row.get("sessions", 0)), "last": row.get("last")}

    def name(self, kind: str, account: str) -> str:
        return ((self._state().get("names") or {}).get(kind) or {}).get(account, "")

    def rename(self, kind: str, account: str, name: str) -> None:
        if account not in self.ids(kind):
            raise ValueError(f"no {PROVIDERS[kind].name} account {account!r}")
        name = " ".join(name.split())[:40]
        data = self._state()
        names = data.setdefault("names", {}).setdefault(kind, {})
        if name:
            names[account] = name
        else:
            names.pop(account, None)
        self._save_state(data)

    def exe(self, kind: str):
        return self.exes.get(kind) or self.which({"claude": "claude", "codex": "codex", "antigravity": "agy"}[kind])

    # -- what there is
    def _state(self) -> dict:
        try:
            return json.loads((self.root / "accounts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "accounts.json").write_text(json.dumps(data, indent=1), encoding="utf-8")

    def home(self, kind: str, account: str) -> Path:
        return PROVIDERS[kind].main_home() if account == MAIN else self.root / kind / account

    def ids(self, kind: str) -> list[str]:
        d = self.root / kind
        extra = sorted((p.name for p in d.glob("a*") if p.is_dir() and re.fullmatch(r"a\d+", p.name)),
                       key=lambda n: int(n[1:])) if d.exists() else []
        return [MAIN, *extra]

    def _env(self, kind: str, account: str) -> dict:
        env = dict(os.environ, NO_COLOR="1")
        if account != MAIN:
            env |= self.env(kind, account, force=True)
        return env

    def who(self, kind: str, account: str, fresh: bool = False) -> dict:
        key = (kind, account)
        if not fresh and key in self._who and time.time() - self._who[key][0] < 60:
            return self._who[key][1]
        exe = self.exe(kind) if kind == "claude" else None
        out = identity(kind, self.home(kind, account), self._env(kind, account), exe)
        self._who[key] = (time.time(), out)
        return out

    def list(self, kind: str) -> list[dict]:
        off = set((self._state().get("off") or {}).get(kind) or [])
        busy = self.login if self.login and self.login.kind == kind and self.login.state not in ("done", "failed") else None
        return [{"id": a, "main": a == MAIN, "enabled": a not in off, "name": self.name(kind, a),
                 "usage": self.usage(kind, a), "quota_until": self.quota_until(kind, a), **self.who(kind, a)}
                for a in self.ids(kind) if not (busy and busy.new and busy.account == a)]

    def usable(self, kind: str) -> list[str]:
        """Accounts helpers can start on: switched on, signed in, and not out of quota."""
        return [a["id"] for a in self.list(kind) if a["enabled"] and a["signed_in"] and not a["quota_until"]]

    # -- quota: an account whose usage allowance is spent, until when
    def quota_until(self, kind: str, account: str | None) -> float | None:
        until = ((self._state().get("quota") or {}).get(kind) or {}).get(account or MAIN)
        return float(until) if until and float(until) > time.time() else None

    def mark_out(self, kind: str, account: str | None, until: float) -> None:
        with self._lock:
            data = self._state()
            q = data.setdefault("quota", {}).setdefault(kind, {})
            q[account or MAIN] = until
            for a in [a for a, t in q.items() if float(t) <= time.time()]:  # what came back is forgotten
                q.pop(a, None)
            self._save_state(data)

    def mark_back(self, kind: str, account: str | None) -> None:
        with self._lock:
            data = self._state()
            if ((data.get("quota") or {}).get(kind) or {}).pop(account or MAIN, None) is not None:
                self._save_state(data)

    def with_quota(self, kind: str, busy: dict[str, int], exclude: str | None = None) -> str | None:
        """Another account of `kind` that can take a session now (switched on, signed in, with quota), the least
        busy first; None when there is none."""
        order = self.ids(kind)
        left = [a for a in self.usable(kind) if a != (exclude or None)]
        return min(left, key=lambda a: (busy.get(a, 0), order.index(a))) if left else None

    def set_enabled(self, kind: str, account: str, on: bool) -> None:
        if account not in self.ids(kind):
            raise ValueError(f"no {PROVIDERS[kind].name} account {account!r}")
        data = self._state()
        off = data.setdefault("off", {})
        now = [a for a in off.get(kind) or [] if a != account and a in self.ids(kind)]
        off[kind] = now if on else [*now, account]
        self._save_state(data)

    def remove(self, kind: str, account: str) -> None:
        """An added account goes away with its folder; the main one is signed out of the CLI itself."""
        if account == MAIN:
            self.sign_out_main(kind)
            return
        if account not in self.ids(kind):
            raise ValueError(f"no {PROVIDERS[kind].name} account {account!r}")
        shutil.rmtree(self.root / kind / account, ignore_errors=True)
        self._who.pop((kind, account), None)
        data = self._state()
        off = data.setdefault("off", {})
        off[kind] = [a for a in off.get(kind) or [] if a in self.ids(kind)]
        (data.get("names") or {}).get(kind, {}).pop(account, None)  # its number may be a new account's later:
        self._save_state(data)
        with self._usage_lock:  # neither its name nor its usage carry over
            (self._usage_data().get(kind) or {}).pop(account, None)
            self._save_usage(force=True)

    def sign_out_main(self, kind: str) -> None:
        if kind == "antigravity":
            (self.home(kind, MAIN) / PROVIDERS[kind].token).unlink(missing_ok=True)
        else:
            exe = self.exe(kind)
            if not exe:
                raise ValueError(f"{PROVIDERS[kind].name} is not installed")
            args = ("auth", "logout") if kind == "claude" else ("logout",)
            subprocess.run([*_cmd(exe), *args], env=self._env(kind, MAIN), capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        self._who.pop((kind, MAIN), None)

    # -- the sessions
    def pick(self, kind: str, busy: dict[str, int]) -> str:
        """The account for a new session: of those switched on and signed in, the one running the fewest sessions;
        the main one when no other is usable."""
        usable = self.usable(kind) or [MAIN]
        order = self.ids(kind)
        return min(usable, key=lambda a: (busy.get(a, 0), order.index(a)))

    def env(self, kind: str, account: str | None, force: bool = False) -> dict:
        """What a session on `account` adds to its environment (nothing for the main account). The main account's
        settings are copied in first when they are newer, so only the sign-in differs."""
        if not account or account == MAIN or kind not in PROVIDERS:
            return {}
        p = PROVIDERS[kind]
        home = self.root / kind / account
        if not force and not (home / p.token).exists():
            return {}
        main = p.main_home()
        for rel in p.copy:
            src, dst = main / rel, home / rel
            try:
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                elif src.exists() and (not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            except OSError:
                pass
        out = {p.env: str(home)}
        if kind == "antigravity":  # another HOME: the `decomp mcp` bridge still has to find this app
            out["DECOMP_HOME"] = os.environ.get("DECOMP_HOME") or str(Path.home() / "Decomp")
        return out

    def display(self, kind: str, account: str | None) -> str:
        """How the page names an account: its name and e-mail ("work · me@x.com"), or what's known of it."""
        account = account or MAIN
        who = self._who.get((kind, account), (0, {}))[1] or {}  # never waits for a CLI: the last known
        if not who and kind != "claude":
            who = self.who(kind, account)
        parts = [p for p in (self.name(kind, account), who.get("email")) if p]
        return " · ".join(parts) or ("main account" if account == MAIN else f"account {account}")

    def label(self, kind: str, account: str | None) -> str:
        if named := self.name(kind, account or MAIN):
            return named
        if len(self.usable(kind)) < 2:
            return ""
        email = self.who(kind, account or MAIN).get("email") or account or MAIN
        return email.split("@", 1)[0]

    # -- signing in
    def start_login(self, kind: str, account: str | None = None) -> dict:
        """Sign in: a new account (`account` None) or again on one there is."""
        if kind not in PROVIDERS:
            raise ValueError(f"accounts aren't managed for {kind!r}")
        p = PROVIDERS[kind]
        with self._lock:
            if self.login and self.login.state in ("starting", "waiting", "checking"):
                self.cancel_login()
            new = account is None
            if new:
                account = f"a{1 + max((int(a[1:]) for a in self.ids(kind) if a != MAIN), default=0)}"
            elif account not in self.ids(kind):
                raise ValueError(f"no {p.name} account {account!r}")
            exe = self.exe(kind)
            if not exe:
                raise ValueError(f"{p.name} is not installed: install it above first")
            home = self.home(kind, account)
            home.mkdir(parents=True, exist_ok=True)
            login = Login(kind, account, new)
            login.proc = subprocess.Popen([*_cmd(exe), *p.login], env=self._env(kind, account),
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          cwd=str(home))
            self.login = login
        threading.Thread(target=self._follow, args=(login, home), daemon=True, name=f"{kind}-login").start()
        deadline = time.time() + 25  # the link comes within a few seconds
        while time.time() < deadline and login.state == "starting":
            time.sleep(0.2)
        return login.snapshot()

    def submit_code(self, code: str) -> dict:
        login = self.login
        if not login or not login.paste or login.state != "waiting" or not login.proc or not login.proc.stdin:
            raise ValueError("no sign-in is waiting for a code")
        code = code.strip()
        if not code or "\n" in code:
            raise ValueError("paste the code the sign-in page showed")
        login.proc.stdin.write((code + "\n").encode())
        login.proc.stdin.flush()
        login.state = "checking"
        for _ in range(100):  # the CLI trades the code for its tokens in a moment
            if login.state != "checking":
                break
            time.sleep(0.2)
        return login.snapshot()

    def _follow(self, login: Login, home: Path) -> None:
        p = PROVIDERS[login.kind]
        token = home / p.token
        before = token.stat().st_mtime if token.exists() else 0
        assert login.proc is not None and login.proc.stdout is not None
        buf, tail = "", []
        fd = login.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            text = _ESC.sub("", buf)  # prompts end without a newline: look at everything read so far
            if not login.url and (m := _URL.search(text)) and m.end() < len(text):  # whole: something follows it
                login.url = m.group(0).rstrip(".,)")
            if not login.paste and not login.code and (m := _DEVICE_CODE.search(text)):
                login.code = m.group(1)
            if login.state == "starting" and login.url and (login.paste or login.code):
                login.state = "waiting"
            tail = [line.strip() for line in text.splitlines() if line.strip()][-6:]
        rc = login.proc.wait()
        now = token.stat().st_mtime if token.exists() else 0
        self._who.pop((login.kind, login.account), None)
        if rc == 0 and now and now != before:
            login.state = "done"
        else:
            login.state = "failed"
            login.error = next((t for t in reversed(tail) if re.search(r"error|expired|fail|timed out|invalid", t, re.IGNORECASE)),
                               tail[-1] if tail else f"the sign-in ended with code {rc}")[:300]
            if login.new:
                shutil.rmtree(home, ignore_errors=True)

    def cancel_login(self) -> None:
        login = self.login
        if login and login.proc and login.state in ("starting", "waiting", "checking"):
            login.proc.terminate()
