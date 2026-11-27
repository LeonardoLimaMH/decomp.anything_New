"""What agents learn about a game, reaching every agent once helpers running two different models agree on it.

Agents used to leave project-wide notes (add_note without a function) that every new session read as "what agents
learned": whatever one agent wrote reached all the others, guesses too. Now an agent proposes (share_knowledge), and
the proposal counts once helpers of two different models back it on their own: two Codex sessions on the same model
agreeing are one opinion, Codex and Claude agreeing are two. That's what running helpers of several models buys: they
check each other.

An agreed proposal reaches every agent's prompt at once when the project adds agreed knowledge by itself (its
setting, on by default), else when the user accepts it. The user can also be the second opinion and accept what one
model backs, or reject a proposal (it never reaches a prompt). Open proposals are listed in every prompt too, so
another model can back them.

Not for which compiler built what: that is settled by compiling (compiler_matrix), not by agents agreeing.
"""

from __future__ import annotations

import re
import time

from ..network import Agent, NetworkError

_EFFORT = re.compile(r"[-_ ](?:minimal|low|medium|high|xhigh|max|thinking)$")
_COMPILER = re.compile(r"\b(?:compiler|compiled with|gcc[- ]?\d|cc1|-O\d|2\.7\.2|2\.8\.1|cdk|psx profile)\b", re.IGNORECASE)
SIMILAR = 0.6  # proposals whose words overlap this much are the same one


def model_key(model: str) -> str:
    """One model, however it's spelled: anthropic/claude-opus-5-5, claude-opus-5.5 and opus are the same; a reasoning
    effort (gemini-4-neon-high, -xhigh) isn't another model."""
    m = model.strip().lower().rsplit("/", 1)[-1]
    while _EFFORT.search(m):
        m = _EFFORT.sub("", m)
    for family in ("opus", "sonnet", "haiku", "fable"):
        if re.search(rf"\b{family}\b", m.replace("-", " ").replace(".", " ")):
            return f"claude-{family}"
    return m


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 2}


def _alike(a: str, b: str) -> bool:
    x, y = _words(a), _words(b)
    return bool(x and y) and len(x & y) / len(x | y) >= SIMILAR


class Knowledge:
    def __init__(self, net):
        self.net = net
        # the model a helper runs on, set by the app (agents.Manager.model_of); None: the app doesn't know, and the
        # agent counts as an opinion of its own
        self.model_of = lambda agent: None  # (the tables: store.py's schema)

    def _model(self, agent: Agent) -> str:
        found = self.model_of(agent)
        return model_key(found) if found else f"agent:{agent.name}"

    def auto(self, slug: str) -> bool:
        row = self.net.store.one("SELECT auto FROM knowledge_settings WHERE project_id=?", self.net._project(slug)["id"])
        return True if row is None else bool(row["auto"])

    def set_auto(self, slug: str, on: bool) -> None:
        proj = self.net._project(slug)
        with self.net.store.tx() as db:
            db.execute("INSERT INTO knowledge_settings(project_id, auto) VALUES(?,?) "
                       "ON CONFLICT(project_id) DO UPDATE SET auto=excluded.auto", (proj["id"], int(bool(on))))
            if on:  # what two models already agree on goes in now
                for r in db.execute("SELECT id FROM knowledge WHERE project_id=? AND state='open'", (proj["id"],)).fetchall():
                    if len(self._models(db, r["id"])) >= 2:
                        db.execute("UPDATE knowledge SET state='accepted', decided_at=? WHERE id=?", (time.time(), r["id"]))

    @staticmethod
    def _models(db, kid: int) -> set[str]:
        return {r["model"] for r in db.execute("SELECT model FROM knowledge_votes WHERE knowledge_id=?", (kid,)).fetchall()}

    def share(self, agent: Agent, slug: str, text: str = "", supports: int = 0) -> str:
        """An agent proposes something every agent on this game should know, or backs a proposal (supports=its id)."""
        proj = self.net._project(slug)
        text = " ".join((text or "").split())
        model = self._model(agent)
        with self.net.store.tx() as db:
            if not supports:
                if len(text) < 12:
                    raise NetworkError("say what every agent should know (a convention, what a struct is, how the "
                                       "code is organised), or back a proposal with supports=its number")
                if _COMPILER.search(text):
                    raise NetworkError("which compiler or flags built what isn't settled by agents agreeing: test it "
                                       "with compiler_matrix, and leave a note on the function")
                same = [r for r in db.execute("SELECT id, text FROM knowledge WHERE project_id=? AND state!='rejected'",
                                              (proj["id"],)).fetchall() if _alike(r["text"], text)]
                if same:
                    supports = same[0]["id"]  # said before: this backs it
                else:
                    kid = db.execute("INSERT INTO knowledge(project_id, text, agent_id, created_at) VALUES(?,?,?,?)",
                                     (proj["id"], text[:1500], agent.id, time.time())).lastrowid
                    db.execute("INSERT INTO knowledge_votes(knowledge_id, agent_id, model, created_at) VALUES(?,?,?,?)",
                               (kid, agent.id, model, time.time()))
                    self.net._event(db, proj["id"], agent.id, "proposal", f"#{kid} {text[:120]}")
                    return (f"Proposed as #{kid}. It reaches every agent once a helper running another model backs it "
                            "(or the user accepts it).")
            row = db.execute("SELECT * FROM knowledge WHERE id=? AND project_id=?", (int(supports), proj["id"])).fetchone()
            if row is None:
                raise NetworkError(f"no proposal #{supports} on this project")
            if row["state"] == "rejected":
                raise NetworkError(f"#{supports} was rejected by the user")
            db.execute("INSERT OR REPLACE INTO knowledge_votes(knowledge_id, agent_id, model, created_at) VALUES(?,?,?,?)",
                       (row["id"], agent.id, model, time.time()))
            models = self._models(db, row["id"])
            if row["state"] == "accepted":
                return f"#{row['id']} is already what every agent knows."
            if len(models) < 2:
                return (f"Backed #{row['id']}, but only one model is behind it so far ({', '.join(sorted(models))}): it "
                        "waits for a helper running another model.")
            if self._auto(db, proj["id"]):
                db.execute("UPDATE knowledge SET state='accepted', decided_at=? WHERE id=?", (time.time(), row["id"]))
                self.net._event(db, proj["id"], agent.id, "knowledge", f"#{row['id']} {row['text'][:120]}")
                return f"Backed #{row['id']}: two models agree ({', '.join(sorted(models))}), so every agent gets it now."
            self.net._event(db, proj["id"], agent.id, "knowledge", f"#{row['id']} agreed, waiting for the user")
            return f"Backed #{row['id']}: two models agree; it reaches every agent once the user accepts it."

    @staticmethod
    def _auto(db, project_id: int) -> bool:
        row = db.execute("SELECT auto FROM knowledge_settings WHERE project_id=?", (project_id,)).fetchone()
        return True if row is None else bool(row["auto"])

    def decide(self, slug: str, kid: int, accept: bool) -> None:
        """The user accepts a proposal (agreed by two models, or one model's the user vouches for), or rejects one
        (also taking back one accepted before)."""
        proj = self.net._project(slug)
        with self.net.store.tx() as db:
            if db.execute("UPDATE knowledge SET state=?, decided_at=? WHERE id=? AND project_id=?",
                          ("accepted" if accept else "rejected", time.time(), int(kid), proj["id"])).rowcount == 0:
                raise NetworkError(f"no proposal #{kid} on this project")

    def forget(self, project_id: int) -> None:
        with self.net.store.tx() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM knowledge WHERE project_id=?", (project_id,)).fetchall()]
            for kid in ids:
                db.execute("DELETE FROM knowledge_votes WHERE knowledge_id=?", (kid,))
            db.execute("DELETE FROM knowledge WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM knowledge_settings WHERE project_id=?", (project_id,))

    def listing(self, slug: str) -> dict:
        """For the page: every proposal not rejected, the models behind it, and the project's setting."""
        proj = self.net._project(slug)
        rows = self.net.store.all("SELECT k.*, a.name AS author FROM knowledge k LEFT JOIN agents a ON a.id=k.agent_id "
                                  "WHERE k.project_id=? AND k.state!='rejected' ORDER BY k.state='open', k.id DESC", proj["id"])
        out = []
        for r in rows:
            votes = self.net.store.all("SELECT v.model, a.name FROM knowledge_votes v LEFT JOIN agents a ON a.id=v.agent_id "
                                       "WHERE v.knowledge_id=?", r["id"])
            models = sorted({v["model"] for v in votes})
            out.append({"id": r["id"], "text": r["text"], "state": r["state"], "author": r["author"],
                        "models": models, "agents": sorted({v["name"] for v in votes if v["name"]}),
                        "agreed": len(models) >= 2, "created_at": r["created_at"]})
        return {"auto": self.auto(slug), "items": out}

    def for_prompt(self, slug: str, budget: int = 3000) -> str:
        """What every agent starts with: the accepted knowledge, then the open proposals it can back."""
        items = self.listing(slug)["items"]
        known = [i for i in items if i["state"] == "accepted"]
        open_ = [i for i in items if i["state"] == "open"]
        lines, used = [], 0
        if known:
            lines.append("What agents on this game agree on (two models, or the user, checked it):")
            for i in known:
                if used + len(i["text"]) > budget:
                    break
                lines.append(f"- {i['text']}")
                used += len(i["text"])
        if open_:
            lines.append("Open proposals (share_knowledge(supports=N) backs one you have seen to be true; don't back "
                         "what you only believe):")
            for i in open_[:12]:
                if used + len(i["text"]) > budget * 1.5:
                    break
                lines.append(f"- #{i['id']} ({', '.join(i['models'])}): {i['text'][:300]}")
                used += min(len(i["text"]), 300)
        return "\n".join(lines)
