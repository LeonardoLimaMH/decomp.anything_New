"""The permuter, in the background: the decomp community's way through the last few percent.

A function at 95% usually differs in register allocation or instruction order, which comes down to how the C
is written, not what it does: the order of statements and declarations, a temporary, a cast, `x += 1` for
`x++`, `register`. decomp-permuter tries thousands of such rewrites and keeps what scores better; agents doing
it by hand spend turns (and tokens) on it, and stall. Here it runs on its own, on spare CPU, on the near misses
agents left (or autopilot's drafts reached): it holds a function for a minute at a time, so an agent that
wants it gets it next, records every improvement as the function's best attempt (agents start from it), and
a match as a match. Rewrites keep the meaning of the code (see permuter.py); what matches is still judged
byte for byte, like everything else.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field

from .. import permuter_local, scoring
from ..forks import csource
from ..network import NetworkError
from ..permuter import mutate

log = logging.getLogger("decomp.wtf")
NEAR = 0.06  # a near miss: at most this much of the function differs (best_score <= 6% of n_insns * 100)
SECONDS = 60  # per function per turn
MAX_TURNS = 4  # turns per function before it's left to agents


def _mutate_function(source: str, name: str, rng: random.Random) -> str | None:
    """`mutate` on the function alone: a best attempt can come with what it needs before it (macros, types, static
    helpers: see forkagents._attempt), and a rewrite belongs in the function's body, not in a struct's."""
    span = csource.find(source, name.split("@")[0])
    if span is None or csource.is_stub(source[span[0]:span[1]]):
        return mutate(source, rng)
    out = mutate(source[span[0]:span[1]], rng)
    return None if out is None else source[:span[0]] + out + source[span[1]:]


@dataclass
class State:
    state: str = "running"
    tried: int = 0  # functions permuted
    variants: int = 0
    improved: int = 0
    matched: int = 0
    current: str = ""
    started_at: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)
    best: int | None = None  # the function's best score so far (a deep search shows it)
    before: int | None = None
    deadline: float = 0.0
    error: str = ""


class Permuter:
    def __init__(self, net):
        self.net = net
        self.jobs: dict[str, State] = {}
        self.deeps: dict[str, State] = {}  # slug -> the user's deep search on one function
        self._turns: dict[int, int] = {}  # function id -> turns spent on it

    def status(self, slug: str) -> dict | None:
        st = self.jobs.get(slug)
        return None if st is None else {k: v for k, v in st.__dict__.items() if k != "stop"}

    def start(self, slug: str) -> State | None:
        """Run on the project's near misses until there are none (looking again every few minutes while the
        app runs: agents make new ones)."""
        if (st := self.jobs.get(slug)) is not None and st.state == "running":
            return st
        if slug in self.net.links:  # a room member: the host's copy permutes, and its matches come back here
            return None
        st = self.jobs[slug] = State()
        threading.Thread(target=self._loop, args=(slug, st), daemon=True, name=f"permuter-{slug}").start()
        return st

    def stop(self, slug: str) -> None:
        if (st := self.jobs.get(slug)) is not None:
            st.stop.set()

    def candidates(self, proj, limit: int = 20) -> list:
        from . import assistance

        return [r for r in self.net.store.all(
            "SELECT * FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL "
            "AND best_score > 0 AND best_score <= n_insns * 100 * ? AND n_insns <= 400 "
            "AND json_extract(meta,'$.assistance.fingerprint')=? "
            "AND COALESCE(json_extract(meta,'$.assistance.summary'),'')!='' "
            "AND COALESCE(json_extract(meta,'$.assistance.fallback_reason'),'')!='' "
            "AND id NOT IN (SELECT function_id FROM claims) ORDER BY CAST(best_score AS REAL) / (n_insns * 100) LIMIT ?",
            proj["id"], NEAR, assistance.fingerprint(self.net, proj), limit * 4) if self._turns.get(r["id"], 0) < MAX_TURNS
            and assistance.reviewed(self.net, proj, r, permutation=True)][:limit]

    def _loop(self, slug: str, st: State) -> None:
        agent, _ = self.net.local_agent("permuter")
        idle = 0
        try:
            while not st.stop.is_set():
                try:
                    proj = self.net.project_row(slug)
                except NetworkError:
                    return  # the project is gone
                todo = self.candidates(proj)
                if not todo:
                    idle += 1
                    if idle > 20:  # nothing for an hour: done until agents start again
                        return
                    st.stop.wait(180)
                    continue
                idle = 0
                for fn in todo:
                    if st.stop.is_set():
                        break
                    self._turns[fn["id"]] = self._turns.get(fn["id"], 0) + 1
                    try:
                        self._permute(agent, proj, fn, st)
                    except Exception as e:  # noqa: BLE001 one odd function must not stop the rest
                        log.info("permuter: %s: %s", fn["name"], e)
        finally:
            st.state = "stopped" if st.stop.is_set() else "done"
            st.current = ""

    def _hold(self, agent, fn, seconds: float = SECONDS, renew: bool = False) -> bool:
        now = time.time()
        with self.net.store.tx() as db:
            self.net._expire_claims(db)
            if renew:
                db.execute("UPDATE claims SET expires_at=? WHERE function_id=? AND agent_id=?",
                           (now + min(seconds, 60) + 120, fn["id"], agent.id))
                return True
            if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                return False
            # (a long search renews its hold each minute: an app that stops leaves the function free soon after)
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + min(seconds, SECONDS) + 120))
        return True

    # -- a deep search: one function, the user's budget
    def deep(self, slug: str, function: str, minutes: float) -> dict:
        """Search one function's rewrites for `minutes` (1 to 60), started by the user: the same rewrites, judge and
        recording of a better attempt as the background permuter, with a bigger budget. One per project at a time."""
        minutes = max(1.0, min(float(minutes), 60.0))
        proj = self.net.project_row(slug)
        fn = self.net._function(function, slug)
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} already matches")
        if not fn["best_source"] or fn["best_score"] is None:
            raise NetworkError(f"{fn['name']} has no attempt yet to start from: compile some C for it first")
        if (cur := self.deeps.get(slug)) is not None and cur.state == "running":
            raise NetworkError(f"a deep search is already running on {cur.current}: stop it first")
        st = self.deeps[slug] = State()
        st.current, st.best, st.deadline = fn["name"], fn["best_score"], time.time() + minutes * 60
        st.before = fn["best_score"]
        agent, _ = self.net.local_agent("permuter")

        def run():
            try:
                self._permute(agent, proj, fn, st, seconds=minutes * 60, user=True)
                st.state = "matched" if st.matched else "stopped" if st.stop.is_set() else "done"
            except Exception as e:  # noqa: BLE001 the page says why
                st.state, st.error = "failed", str(e)[:400]

        threading.Thread(target=run, daemon=True, name=f"deep-permuter-{slug}").start()
        return self.deep_status(slug) or {}

    def deep_status(self, slug: str) -> dict | None:
        st = self.deeps.get(slug)
        if st is None:
            return None
        return {"state": st.state, "function": st.current, "variants": st.variants, "improved": st.improved,
                "before": st.before, "best": st.best, "matched": bool(st.matched), "error": st.error,
                "left": max(0, int(st.deadline - time.time())) if st.state == "running" else 0}

    def deep_stop(self, slug: str) -> None:
        if (st := self.deeps.get(slug)) is not None:
            st.stop.set()

    def _let_go(self, agent, fn) -> None:
        with self.net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    def _permute(self, agent, proj, fn, st: State, seconds: float = SECONDS, user: bool = False) -> None:
        """One turn on one function: the local hypotheses first (permuter_local.py), then random rewrites until
        `seconds` run out. `user`: started by the user for this function (a deep search), so it doesn't wait for
        an agent's review of the function first."""
        from . import assistance

        proj = self.net.project_row(proj["slug"])
        fn = self.net._function(fn["name"], proj["slug"])
        if not user and not assistance.reviewed(self.net, proj, fn, permutation=True):
            return
        if not self._hold(agent, fn, seconds):
            if user:
                raise NetworkError(f"{fn['name']} is being worked on by an agent right now: try again when it's free")
            return
        st.current, st.tried = fn["name"], st.tried + 1
        fork = self.net.fork_slug(proj["slug"])
        rng = random.Random(f"{fn['id']}-{self._turns.get(fn['id'])}")
        try:
            best_src = cur_src = self._fit(agent, proj, fn, fn["best_source"], fn["best_score"], fork)
            if best_src is None:
                self._turns[fn["id"]] = MAX_TURNS  # can't be placed in its file: leave it to agents
                return
            best = cur = fn["best_score"]
            st.best = best
            started = time.monotonic()
            deadline = started + seconds
            batch_size = min(8, max(2, (os.cpu_count() or 4) - 2))
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
                # cheap ordering hypotheses before random drift; only the normal judge accepts one
                local = permuter_local.variants(best_src, fn["name"]) if not st.stop.is_set() else []
                if local:
                    st.variants += len(local)
                    futures = [pool.submit(self._score, agent, proj, fn, src, best, fork) for _, src in local]
                    for (_, src), f in zip(local, futures, strict=False):
                        score = f.result()
                        if score is not None and score < best:
                            best, best_src, cur, cur_src = score, src, score, src
                            st.improved += 1
                            st.best = best
                            if score == 0:
                                st.matched += 1
                                log.info("permuter: %s matched (local hypothesis)", fn["name"])
                                return
                while time.monotonic() < deadline and best > 0 and not st.stop.is_set():
                    if user and time.monotonic() - started > 60:  # a long search keeps its hold on the function
                        self._hold(agent, fn, deadline - time.monotonic(), renew=True)
                        started = time.monotonic()
                    cands = []
                    for _ in range(batch_size):
                        cand = _mutate_function(cur_src if rng.random() < 0.85 else best_src, fn["name"], rng)
                        if cand is not None and cand != cur_src:
                            cands.append(cand)
                    if not cands:
                        continue
                    st.variants += len(cands)

                    futures = [pool.submit(self._score, agent, proj, fn, c, best, fork) for c in cands]
                    for cand_src, f in zip(cands, futures, strict=False):
                        score = f.result()
                        if score is None:
                            continue
                        if score < best:
                            best, best_src = score, cand_src
                            st.improved += 1
                            st.best = best
                            if score == 0:
                                st.matched += 1
                                log.info("permuter: %s matched", fn["name"])
                                return
                        if score <= cur or rng.random() < 0.02:  # mostly downhill, now and then sideways off a plateau
                            cur, cur_src = score, cand_src
        finally:
            self._let_go(agent, fn)

    def _fit(self, agent, proj, fn, source: str, best: int, fork: str | None) -> str | None:
        """The best source as it can be placed in its file. A best attempt can carry a type its file now defines
        differently (the file was synced from upstream after the attempt); every variant would then be refused
        the same way. Such a type is renamed in the source (`T` -> `T_<function>`), which keeps the function's
        code the same, until it places; None when it still can't."""
        if not fork:
            return source
        for _ in range(6):
            try:
                self._score(agent, proj, fn, source, best, fork)
                return source
            except NetworkError as e:
                m = re.search(r"already defines `(\w+)`, differently", str(e))
                if not m:
                    log.info("permuter: %s: %s", fn["name"], e)
                    return None
                name = m.group(1)
                source = re.sub(rf"\b{re.escape(name)}\b", f"{name}_{fn['name'].rsplit('_', 1)[-1]}", source)
        return None

    def _score(self, agent, proj, fn, source: str, best: int, fork: str | None) -> int | None:
        """A variant's score; recorded (as the function's best attempt, or its match) only when it beats `best`."""
        if fork:
            res = self.net.forks.attempt(agent, fork, fn["name"], source, keep=False, quiet=best)
            return res.get("score")
        ctx = self.net._must_context(proj["id"], fn["unit"])
        result = self.net._compile(proj, ctx["header"], source)
        if not result.ok:
            return None
        cand, verdict, _ = self.net._evaluate(fn, proj, result.obj)
        if cand is None:
            return None
        d = scoring.diff(self.net._target_obj(fn, proj), cand)
        if verdict is not None:
            d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
        if d.score < best:
            self.net._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match, via="permuter")
        return 0 if d.bytes_match else max(1, d.score)
