"""last_diff: every differing row of an agent's last compile, where compile_and_diff's answer was cut, at no cost."""

import json

import pytest

from decompnet.app import tools
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError
from decompnet.scoring import render_rows


def _net(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom"}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status) VALUES(?,?,?,?,?)",
                   (pid, "func_80001000", 800, 200, "open"))
    return net, pid


ROWS = [(" " if i % 3 else "|", f"addiu v0, v0, {i}", f"addiu v0, v0, {i if i % 3 else i + 1}") for i in range(200)]


def test_the_compile_answer_is_cut_and_last_diff_shows_every_row(tmp_path):
    net, pid = _net(tmp_path)
    agent = net.local_agent("codex-1")[0]
    cut = render_rows(ROWS)
    assert "more rows not shown" in cut and "last_diff" in cut
    net.remember_rows(agent, pid, "func_80001000", ROWS)
    text = tools.HANDLERS["last_diff"](net, agent, "game", function="func_80001000", context=0)
    assert "more rows not shown" not in text
    assert sum(1 for line in text.splitlines() if line.startswith(" |")) == sum(1 for r in ROWS if r[0] != " ")
    with_context = net.last_diff(agent, "func_80001000", "game", context=1)
    assert len(with_context.splitlines()) > len(text.splitlines())


def test_last_diff_is_each_agents_own_and_says_when_there_is_none(tmp_path):
    net, pid = _net(tmp_path)
    mine, other = net.local_agent("codex-1")[0], net.local_agent("claude-1")[0]
    net.remember_rows(mine, pid, "func_80001000", ROWS)
    with pytest.raises(NetworkError, match="compile it first"):
        net.last_diff(other, "func_80001000", "game")
    net.remember_rows(mine, pid, "func_80001000", [(" ", "nop", "nop")], matched=True)
    assert "no differing rows" in net.last_diff(mine, "func_80001000", "game")
    assert "last_diff" in tools.tool_names() and "last_diff" in tools.tool_names(fork=True)
    assert "last_diff" not in tools.tool_names(analysis=True)
