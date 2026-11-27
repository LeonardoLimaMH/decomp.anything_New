"""The permuter's local hypotheses (tried before random rewrites) and the user's deep search of one function."""

import json
import time

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.app.permute import Permuter
from decompnet.network import NetworkError
from decompnet.permuter_local import variants


def _kinds(src, name="f"):
    return [k for k, _ in variants(src, name)]


def test_consecutive_constant_stores_through_one_pointer_are_swapped():
    src = "void f(Actor *p) {\n    p->x = 0;\n    p->y = 1;\n    g(p);\n}"
    vs = dict(variants(src, "f"))
    assert vs["constant stores swapped"] == "void f(Actor *p) {\n    p->y = 1;\n    p->x = 0;\n    g(p);\n}"
    # another pointer, a non-constant, a call in between: left alone
    assert "constant stores swapped" not in _kinds("void f(A *p, A *q) {\n    p->x = 0;\n    q->y = 1;\n}")
    assert "constant stores swapped" not in _kinds("void f(A *p) {\n    p->x = n;\n    p->y = 1;\n}")
    assert "constant stores swapped" not in _kinds("void f(A *p) {\n    p->x = 0;\n    g();\n    p->y = 1;\n}")


def test_consecutive_declarations_are_swapped():
    src = "s32 f(void) {\n    s32 i;\n    u8 *p;\n    return 0;\n}"
    assert dict(variants(src, "f"))["declarations swapped"] == "s32 f(void) {\n    u8 *p;\n    s32 i;\n    return 0;\n}"
    assert "declarations swapped" not in _kinds("s32 f(void) {\n    return x;\n    goto y;\n}")


def test_commutative_operands_only_where_the_expression_is_whole():
    swapped = dict(variants("s32 f(s32 a, s32 b) {\n    return a + (b);\n}\ns32 g(s32 a) { s32 v = a * 3; return v; }", "g"))
    assert swapped["operands of * swapped"].endswith("s32 v = 3 * a; return v; }")
    assert dict(variants("void f(s32 a, s32 b) {\n    x = a + b;\n}", "f"))["operands of + swapped"] == \
        "void f(s32 a, s32 b) {\n    x = b + a;\n}"
    assert dict(variants("void f(void) {\n    x = a[i] + b;\n}", "f"))["operands of + swapped"].endswith("x = b + a[i];\n}")
    assert dict(variants("s32 f(s32 m) {\n    return m & k;\n}", "f"))["operands of & swapped"].endswith("return k & m;\n}")
    # precedence would regroup these: never touched
    for body in ("x = a - b + c;", "x = *p * 2;", "y = &x;", "if (a == b & c) {}", "x = a + b * c;", "x = a + b;\n#if 0\n#endif",
                 "x = f(a) + b;"):
        kinds = _kinds("void f(void) {\n    " + body + "\n}")
        assert not any(k.startswith("operands") for k in kinds), (body, kinds)


def test_inline_assembly_and_unknown_functions_give_nothing():
    assert variants('void f(void) {\n    p->x = 0;\n    p->y = 1;\n    __asm__("nop");\n}', "f") == []
    assert variants("void f(void) { p->x = 0; }", "missing") == []


def _net(tmp_path, best="void f(A *p) {\n    p->x = 0;\n    p->y = 1;\n}"):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom"}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,best_source,best_score) VALUES(?,?,?,?,?,?,?)",
                   (pid, "f", 40, 10, "open", best, 50))
    return net


class _Judge(Permuter):
    """The permuter with a judge that only accepts the stores the other way round."""

    def _score(self, agent, proj, fn, source, best, fork):
        self.seen.append(source)
        return 0 if source.index("p->y") < source.index("p->x") else 60


def test_a_local_hypothesis_is_judged_first_and_a_match_ends_the_turn(tmp_path):
    net = _net(tmp_path)
    p = _Judge(net)
    p.seen = []
    st = p.deeps["game"] = __import__("decompnet.app.permute", fromlist=["State"]).State()
    agent, _ = net.local_agent("permuter")
    p._permute(agent, net.project_row("game"), net._function("f", "game"), st, seconds=30, user=True)
    assert st.matched == 1 and len(p.seen) == 1  # the first thing tried: no random rewrites needed
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0  # the hold is let go


def test_a_deep_search_runs_in_the_background_and_reports(tmp_path):
    net = _net(tmp_path)
    p = _Judge(net)
    p.seen = []
    st = p.deep("game", "f", 1)
    assert st["function"] == "f" and st["before"] == 50
    for _ in range(100):
        if p.deep_status("game")["state"] != "running":
            break
        time.sleep(0.05)
    done = p.deep_status("game")
    assert done["state"] == "matched" and done["matched"] and done["left"] == 0
    with pytest.raises(NetworkError, match="no attempt yet"):
        (tmp_path / "x").mkdir()
        _net(tmp_path / "x", best=None)
        Permuter(RomNetwork(tmp_path / "x" / "app.db", tmp_path / "x")).deep("game", "f", 5)


def test_a_deep_search_needs_the_function_free_and_one_at_a_time(tmp_path):
    net = _net(tmp_path, best="void f(void) {\n    x = 1;\n}")  # nothing local: random rewrites until stopped
    p = _Judge(net)
    p.seen = []
    p._score = lambda *a: 60
    p.deep("game", "f", 1)
    with pytest.raises(NetworkError, match="already running"):
        p.deep("game", "f", 1)
    p.deep_stop("game")
    for _ in range(100):
        if p.deep_status("game")["state"] != "running":
            break
        time.sleep(0.05)
    assert p.deep_status("game")["state"] == "stopped"
