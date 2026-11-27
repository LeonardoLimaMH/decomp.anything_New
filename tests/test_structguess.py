"""guess_struct: the struct behind a pointer, from MIPS loads and stores across the functions it reaches."""

import pytest

from decompnet import structguess as sg

# objdiff's spelling (no $), as a fork's index has it
ACTOR_UPDATE = """\
addiu sp, sp, -0x20
sw s0, 0x10(sp)
addu s0, a0, zero
sw ra, 0x14(sp)
lh v0, 0x0(s0)
lhu v1, 0x2(s0)
lwc1 f0, 0x4(s0)
lw a0, 0x1c(s0)
jal actor_draw
addu a1, s0, zero
lw v0, 0x18(s0)
lbu v1, 0x4(v0)
sw zero, 0x1c(s0)
lw ra, 0x14(sp)
lw s0, 0x10(sp)
jr ra
addiu sp, sp, 0x20""".splitlines()

# the callee gets the struct as a1, and reads another field (and 0x2 as signed)
ACTOR_DRAW = """\
lh v0, 0x2(a1)
lw v1, 0x8(a1)
jr ra
nop""".splitlines()

# a caller that hands its own argument on: its accesses count too
GAME_TICK = """\
addiu sp, sp, -0x18
sw ra, 0x10(sp)
sw a0, 0x14(sp)
jal actor_update
nop
lw a0, 0x14(sp)
lh v0, 0x20(a0)
lw ra, 0x10(sp)
jr ra
addiu sp, sp, 0x18""".splitlines()


def _asm():
    return {"actor_update": ACTOR_UPDATE, "actor_draw": ACTOR_DRAW, "game_tick": GAME_TICK}


def _fields(res):
    out = {}
    for a in res.accesses:
        out.setdefault(a.offset, set()).add((a.type, a.function))
    return out


def test_fields_come_from_the_function_its_callees_and_its_callers():
    res = sg.analyse(_asm(), "actor_update", sg.Spec.of("arg0"))
    f = _fields(res)
    assert ("s16", "actor_update") in f[0x0]
    assert ("u16", "actor_update") in f[0x2] and ("s16", "actor_draw") in f[0x2]  # passed on as a1
    assert ("f32", "actor_update") in f[0x4]
    assert ("s32", "actor_draw") in f[0x8]
    assert ("s16", "game_tick") in f[0x20]  # the caller's own argument, reloaded from the stack after the call
    assert res.functions == {"actor_update", "actor_draw", "game_tick"}
    assert 0x18 in res.pointers  # loaded, then used as a base: a pointer field
    assert ("actor_draw", "arg1") in res.flows and ("game_tick", "arg0") in res.flows


def test_the_layout_is_c_with_offsets_padding_votes_and_conflicts():
    res = sg.analyse(_asm(), "actor_update", sg.Spec.of("arg0"))
    text = sg.render(res, "actor_update", "arg0")
    assert "/* 0x00 */ s16 unk_00;" in text
    assert "/* 0x02 */ u16 unk_02;" in text or "/* 0x02 */ s16 unk_02;" in text
    assert "also read as" in text  # 0x2: u16 in one function, s16 in the other
    assert "/* 0x04 */ f32 unk_04;" in text
    assert "char pad0C[0xC];" in text  # 0x0C..0x18 never touched
    assert "/* 0x18 */ void *unk_18;" in text and 'guess_struct("actor_update", "arg0+0x18")' in text
    assert "size >= 0x22" in text
    assert "game_tick arg0" in text


def test_plus_offset_follows_the_pointer_stored_there():
    res = sg.analyse(_asm(), "actor_update", sg.Spec.of("arg0+0x18"))
    assert {(a.offset, a.type) for a in res.accesses} == {(0x4, "u8")}


def test_registers_with_dollar_signs_and_a_pointer_into_the_middle():
    asm = {"f": ["addiu $v0, $a0, 0x10", "lw $v1, 0x4($v0)", "sh $zero, 0x0($v0)", "jr $ra", "nop"]}
    res = sg.analyse(asm, "f", sg.Spec.of("arg0"))
    assert {(a.offset, a.type, a.store) for a in res.accesses} == {(0x14, "s32", False), (0x10, "s16", True)}


def test_a_call_clobbers_what_it_may_change_and_saved_registers_survive():
    asm = {"f": ["addu s1, a0, zero", "addu t0, a0, zero", "jal g", "nop", "lw v0, 0x8(t0)", "lw v1, 0xc(s1)",
                 "jr ra", "nop"], "g": ["jr ra", "nop"]}
    res = sg.analyse(asm, "f", sg.Spec.of("arg0"))
    assert {a.offset for a in res.accesses} == {0xC}


def test_return_and_a_global():
    asm = {"alloc": ["jr ra", "nop"],
           "user": ["jal alloc", "nop", "sw zero, 0x8(v0)", "lbu v1, 0x1(v0)", "jr ra", "nop"],
           "glob": ["lui v0, %hi(D_80001000)", "addiu v0, v0, %lo(D_80001000)", "lhu v1, 0x6(v0)", "jr ra", "nop"]}
    res = sg.analyse(asm, "alloc", sg.Spec.of("return"))
    assert {(a.offset, a.type) for a in res.accesses} == {(0x8, "s32"), (0x1, "u8")}
    res = sg.analyse(asm, "glob", sg.Spec.of("D_80001000"))
    assert {(a.offset, a.type) for a in res.accesses} == {(0x6, "u16")}


def test_nothing_found_says_so_and_a_bad_spec_is_refused():
    res = sg.analyse({"f": ["jr ra", "nop"]}, "f", sg.Spec.of("arg2"))
    assert "No loads or stores" in sg.render(res, "f", "arg2")
    with pytest.raises(ValueError):
        sg.Spec.of("arg0+banana")
    with pytest.raises(KeyError):
        sg.analyse({}, "missing", sg.Spec.of("arg0"))


def test_known_type_from_matched_code_is_named():
    res = sg.analyse(_asm(), "actor_update", sg.Spec.of("arg0"))
    text = sg.render(res, "actor_update", "arg0", known=[("Actor *", "actor_draw's parameter 2")])
    assert "`Actor *` (actor_draw's parameter 2)" in text


def test_fields_of_a_global_struct_reached_directly():
    """%lo(D_x + 0x20)(at): a field of the global, as a ROM listing and objdiff both write it."""
    asm = {"hurt": ["  0:  lui $at, %hi(D_8004AE34+0x20)", "  4:  lh $v0, %lo(D_8004AE34+0x20)($at)",
                    "  8:  lui $v1, %hi(D_8004AE34+0x1C)", "  c:  lw $v1, %lo(D_8004AE34+0x1C)($v1)",
                    " 10:  sh $a0, %lo(D_8004AE34 + 0x22)($at)", " 14:  jr $ra", " 18:  nop"]}
    res = sg.analyse(asm, "hurt", sg.Spec.of("D_8004AE34"))
    assert {(a.offset, a.type) for a in res.accesses} == {(0x20, "s16"), (0x1C, "s32"), (0x22, "s16")}


def test_the_brief_points_at_guess_struct_for_fields_read_through_an_argument():
    from decompnet.app.forkagents import struct_args

    assert struct_args(["lw v0, 0x4(a0)", "lh v1, 0x2(a2)", "jr ra", "nop"]) == ["arg0", "arg2"]
    assert struct_args(["addiu a0, a0, 0x1", "lw v0, 0x4(a0)"]) == []  # a0 changed first: not the argument any more
    assert struct_args(["jal f", "nop", "lw v0, 0x4(a0)"]) == []  # after a call a0 is the callee's
    assert struct_args(["lwz r3, 0x4(r3)", "blr"]) == []  # not MIPS


def test_the_type_matched_code_gives_a_parameter():
    src = "void actor_draw(s32 mode, Actor *actor) {\n    actor->x = 0;\n}"
    assert sg.param_type(src, "actor_draw", 1) == "Actor *"
    assert sg.param_type(src, "actor_draw", 0) is None  # not a pointer
    assert sg.param_type("void f(u8 *buf) {}", "f", 0) is None  # a primitive's pointer names no struct
    assert sg.param_type("void f(struct Foo* p, s32 n) {}", "f", 0) == "struct Foo *"


class _ForkStub:
    """Just what ForkIndexMixin.guess_struct reaches."""

    def __init__(self, asm, sources):
        import threading

        from decompnet.app.forkindex import ForkIndexMixin

        self.asm, self.sources = asm, sources
        self._ilock = threading.Lock()
        self.guess_struct = ForkIndexMixin.guess_struct.__get__(self)

    def get(self, slug):
        class FP:
            def unit(self, name):
                return name

            def units(self):
                import pathlib
                import types

                here = pathlib.Path(__file__)  # two units with target objects, one without (nothing to read)
                return [types.SimpleNamespace(target=here), types.SimpleNamespace(target=here),
                        types.SimpleNamespace(target=None)]
        return {"id": 1}, FP(), {}

    def _fn(self, slug, name):
        if name not in self.asm:
            from decompnet.network import NetworkError
            raise NetworkError(f"no function {name}")
        return {"name": name, "unit": "u1", "meta": "{}", "status": "matched" if name in self.sources else "open"}

    def _meta(self, row):
        return {"symbol": row["name"]}

    def _unit_index(self, *a):
        return None

    def start_indexing(self, slug):
        pass

    def _index_of(self, proj, fp):
        return {"u1": {"asm": self.asm}}  # u2 not read yet

    def _current_source(self, fp, row):
        return self.sources.get(row["name"])


def test_guess_struct_on_a_fork_uses_the_index_and_names_the_matched_type():
    stub = _ForkStub(_asm(), {"actor_draw": "void actor_draw(s32 mode, Actor *a) {}"})
    text = stub.guess_struct("p", "actor_update", "arg0")
    assert "/* 0x04 */ f32 unk_04;" in text
    assert "`Actor *` (actor_draw's parameter 2)" in text
    assert "1 of 2 source files" in text  # the rest still being read
    from decompnet.network import NetworkError
    with pytest.raises(NetworkError):
        stub.guess_struct("p", "actor_update", "arg0+x")


def test_a_general_helper_and_the_callers_other_callees_are_not_followed():
    """memcpy-like helpers take every kind of pointer, and a caller's other callees get other structs: following
    them mixes unrelated layouts in."""
    callers = {f"user{i}": ["jal helper", "nop", "jr ra", "nop"] for i in range(sg.SHARED + 1)}
    asm = {**callers,
           "root": ["lw v0, 0x4(a0)", "jal helper", "nop", "jr ra", "nop"],
           "helper": ["lbu v0, 0x30(a0)", "jr ra", "nop"],
           "parent": ["addu s0, a0, zero", "jal root", "addu a0, s0, zero", "jal other", "addu a0, s0, zero",
                      "lhu v0, 0x8(s0)", "jr ra", "nop"],
           "other": ["lw v0, 0x40(a0)", "jr ra", "nop"]}
    res = sg.analyse(asm, "root", sg.Spec.of("arg0"))
    offs = {a.offset for a in res.accesses}
    assert 0x30 not in offs and ("helper", "arg0 (shared by many callers: not followed)") in res.flows
    assert 0x8 in offs  # the caller's own use of it counts
    assert 0x40 not in offs  # but not what it hands to its other callees
