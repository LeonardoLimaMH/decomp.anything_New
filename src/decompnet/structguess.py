"""The likely struct behind a pointer, from the loads and stores of MIPS code (guess_struct).

An agent writing C for a function that takes a struct pointer has to work out the struct: every offset used with the
pointer, each access's width and signedness, gaps as padding. The same pointer usually reaches other functions too
(it's passed on as an argument, or handed in by callers), and each of them touches some fields: together they show
more of the layout than one function does, and where they disagree (a field read as s16 here and u16 there) is
worth knowing.

This follows one pointer through a function's code and through every function it reaches: callees it is passed to
(as a0-a3), and callers that pass their own argument on to it. It tracks the pointer through register moves, pointer
arithmetic (a pointer into the middle of the struct), stack spills and reloads, and "+0x14" follows the pointer stored
at 0x14 to the struct it points at. The result is C with offsets, how many functions agree on each field, the
other types a field is read as, and where the pointer flows.

The scan is linear (branches aren't followed), so it's inference: the judge still decides. Instructions come as
objdiff prints them (`lw v0, 0x1c(a0)`) or with $-prefixed registers (`lw $v0, 0x1C($a0)`); `%lo(sym)(reg)` is a
global's address.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

ARGS = ("a0", "a1", "a2", "a3")
# registers a call leaves undefined (the callee may change them): a pointer kept there is gone after a jal
CALLER_SAVED = {"v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9", "at"}
LOADS = {"lb": ("s8", 1), "lbu": ("u8", 1), "lh": ("s16", 2), "lhu": ("u16", 2), "lw": ("s32", 4),
         "lwc1": ("f32", 4), "ldc1": ("f64", 8), "ld": ("s64", 8)}
STORES = {"sb": ("u8", 1), "sh": ("s16", 2), "sw": ("s32", 4), "swc1": ("f32", 4), "sdc1": ("f64", 8), "sd": ("s64", 8)}
UNALIGNED = {"lwl", "lwr", "swl", "swr"}
MOVES = {"move", "addu", "or", "daddu"}
BRANCH = re.compile(r"^(?:b\w*|j|jr|jalr)$")
_MEM = re.compile(r"^(-?(?:0x[0-9a-fA-F]+|\d+))?\((\w+)\)$")
_LO = re.compile(r"^%lo\((\w+)(?:\s*\+\s*(0x[0-9a-fA-F]+|\d+))?\)\((\w+)\)$")
_HI = re.compile(r"^%hi\((\w+)(?:\s*\+\s*(0x[0-9a-fA-F]+|\d+))?\)$")
_LO_IMM = re.compile(r"^%lo\((\w+)(?:\s*\+\s*(0x[0-9a-fA-F]+|\d+))?\)$")


def _int(s: str) -> int:
    return int(s, 0)


def parse(line: str) -> tuple[str, list[str]]:
    """`lw $v0, 0x1C($a0)` -> ("lw", ["v0", "0x1C(a0)"])."""
    line = line.split("#", 1)[0].split(";", 1)[0].strip()
    line = re.sub(r"^[0-9a-fA-F]+:\s+", "", line)  # a ROM project's listing: "  1c:  lw $v0, 0x1c($a0)"
    m = re.match(r"^(?:/\*.*?\*/\s*)*([a-z][\w.]*)\s*(.*)$", line)
    if not m:
        return "", []
    ops = [o.strip().replace("$", "") for o in m.group(2).split(",")] if m.group(2).strip() else []
    return m.group(1).lower(), ops


@dataclass
class Access:
    offset: int
    type: str  # s8 u8 s16 u16 s32 f32 ... "ptr" when the value loaded is used as a pointer
    size: int
    function: str
    store: bool


@dataclass
class Result:
    accesses: list[Access] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)  # every function where the pointer was seen
    flows: set[tuple[str, str]] = field(default_factory=set)  # (function, "arg1") it's handed to or comes from
    pointers: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))  # offset -> functions using it as a pointer
    strides: set[int] = field(default_factory=set)


@dataclass
class Spec:
    """What to follow: `arg0`..`arg3`, `return` or a global, then `+0x14` steps through stored pointers."""

    root: str
    chain: tuple[int, ...] = ()

    @staticmethod
    def of(text: str) -> Spec:
        parts = [p.strip() for p in text.replace(" ", "").split("+")]
        root = parts[0] or "arg0"
        if root.lower() in ("this", "a0"):
            root = "arg0"
        if re.fullmatch(r"a[0-3]", root.lower()):
            root = "arg" + root[1]
        try:
            chain = tuple(_int(p) for p in parts[1:] if p)
        except ValueError:
            raise ValueError(f"'{text}': give arg0-arg3, return or a global's name, then +0x14 to follow the "
                             "pointer stored at 0x14") from None
        return Spec(root if not root.lower().startswith(("arg", "return")) else root.lower(), chain)


class Tracker:
    """Follows one pointer through one function, linearly."""

    def __init__(self, fn: str, lines: list[str], level: int, result: Result, chain: tuple[int, ...]):
        self.fn, self.lines, self.chain, self.res = fn, lines, chain, result
        self.regs: dict[str, tuple[int, int]] = {}  # reg -> (level along the chain, offset into the struct)
        self.stack: dict[int, tuple[int, int]] = {}  # spilled copies: sp offset -> (level, delta)
        self.loaded: dict[str, tuple[int, int]] = {}  # reg -> (offset, level) of the field it was loaded from
        self.his: dict[str, tuple[str, int]] = {}  # reg -> %hi(symbol + addend)
        self.calls: list[tuple[str, dict[str, tuple[int, int]]]] = []  # (callee, its argument registers that carry it)
        self.level = level

    def target(self) -> int:
        return len(self.chain)

    def kill(self, reg: str) -> None:
        self.regs.pop(reg, None)
        self.loaded.pop(reg, None)
        self.his.pop(reg, None)

    def record(self, offset: int, typ: str, size: int, store: bool) -> None:
        self.res.accesses.append(Access(offset, typ, size, self.fn, store))
        self.res.functions.add(self.fn)

    def mem(self, op: str, ops: list[str]) -> None:
        if len(ops) < 2:
            return
        rt = ops[0]
        m = _MEM.match(ops[1])
        if not m:  # %lo(sym + off)(reg): a field of a global struct, when sym is the one followed
            lo = _LO.match(ops[1])
            if lo and getattr(self, "global_root", None) == lo.group(1) and self.target() == 0:
                off = _int(lo.group(2)) if lo.group(2) else 0
                if op in LOADS:
                    self.record(off, *LOADS[op], store=False)
                elif op in STORES:
                    self.record(off, *STORES[op], store=True)
            if op in LOADS or (op in UNALIGNED and op.startswith("l")):
                self.kill(rt)
                if lo and op == "lw" and getattr(self, "global_root", None) == lo.group(1):
                    off = _int(lo.group(2)) if lo.group(2) else 0
                    self.loaded[rt] = (off, 0)
                    if self.chain and off == self.chain[0]:
                        self.regs[rt] = (1, 0)
            return
        off = _int(m.group(1)) if m.group(1) else 0
        base = m.group(2)
        if base == "sp":
            if op == "sw":
                if rt in self.regs:
                    self.stack[off] = self.regs[rt]
                else:
                    self.stack.pop(off, None)
            elif op == "lw":
                self.kill(rt)
                if off in self.stack:
                    self.regs[rt] = self.stack[off]
            elif op in LOADS:
                self.kill(rt)
            return
        here = self.regs.get(base)
        is_load = op in LOADS or op in ("lwl", "lwr")
        if here is not None:
            lvl, delta = here
            if lvl == self.target():
                if op in LOADS:
                    typ, size = LOADS[op]
                    self.record(off + delta, typ, size, False)
                elif op in STORES:
                    typ, size = STORES[op]
                    if op == "sw" and rt in self.regs:
                        typ = "ptr"
                    self.record(off + delta, typ, size, True)
                elif op in UNALIGNED:
                    self.record(off + delta, "u8[]", 1, op.startswith("s"))
            if is_load:
                self.kill(rt)
                if op == "lw":
                    self.loaded[rt] = (off + delta, lvl)
                    if lvl < self.target() and off + delta == self.chain[lvl]:
                        self.regs[rt] = (lvl + 1, 0)  # the pointer stored at chain[lvl]: one step further
            return
        if is_load:
            # the loaded value: a pointer read out of the tracked struct becomes the "ptr" field when it is used
            self.kill(rt)

    def used_as_pointer(self, reg: str) -> None:
        if reg in self.loaded:
            off, lvl = self.loaded.pop(reg)
            if lvl == self.target():
                self.res.pointers[off].add(self.fn)

    def run(self, start: dict[str, tuple[int, int]]) -> None:
        self.regs = dict(start)
        pending_call: str | None = None
        for raw in self.lines:
            op, ops = parse(raw)
            if not op:
                continue
            delayed = pending_call
            pending_call = None
            for o in ops:  # any register used as a base: the field it came from holds a pointer
                if (m := _MEM.match(o)) and m.group(2) != "sp":
                    self.used_as_pointer(m.group(2))
            if op in LOADS or op in STORES or op in UNALIGNED:
                self.mem(op, ops)
            elif op in MOVES and len(ops) == 3 and "zero" in ops[1:]:
                src = ops[2] if ops[1] == "zero" else ops[1]
                self._copy(ops[0], src, 0)
            elif op == "move" and len(ops) == 2:
                self._copy(ops[0], ops[1], 0)
            elif op in ("addiu", "daddiu", "addi") and len(ops) == 3:
                if (lo := _LO_IMM.match(ops[2])) and ops[1] in self.his:
                    self.kill(ops[0])
                    self._global(ops[0], lo.group(1), _int(lo.group(2)) if lo.group(2) else 0)
                else:
                    try:
                        imm = _int(ops[2])
                    except ValueError:
                        self.kill(ops[0])
                        continue
                    self._copy(ops[0], ops[1], imm)
            elif op == "lui" and len(ops) == 2:
                self.kill(ops[0])
                if hi := _HI.match(ops[1]):
                    self.his[ops[0]] = (hi.group(1), _int(hi.group(2)) if hi.group(2) else 0)
            elif op in ("addu", "daddu") and len(ops) == 3:
                # pointer + index: an element of an array of these structs (or a field reached by a computed offset)
                a, b = self.regs.get(ops[1]), self.regs.get(ops[2])
                keep = a or b
                self.kill(ops[0])
                if keep is not None:
                    self.regs[ops[0]] = keep
            elif op in ("jal", "bal"):
                pending_call = ops[0] if ops else None
                continue
            elif (op in ("mult", "multu") and len(ops) >= 2) or BRANCH.match(op):
                pass  # no register written (hi/lo aside)
            elif ops:
                self.kill(ops[0])
            if delayed:
                self._call(delayed)
        if pending_call:
            self._call(pending_call)

    def _copy(self, dst: str, src: str, delta: int) -> None:
        here = self.regs.get(src)
        loaded = self.loaded.get(src)
        self.kill(dst)
        if here is not None:
            self.regs[dst] = (here[0], here[1] + delta)
        elif loaded is not None and delta == 0:
            self.loaded[dst] = loaded

    def _global(self, dst: str, sym: str, addend: int) -> None:
        """`lui r, %hi(sym)` + `addiu r, r, %lo(sym)`: the global's address, which is the pointer when sym is it."""
        if getattr(self, "global_root", None) == sym:
            self.regs[dst] = (0, addend)

    def _call(self, callee: str) -> None:
        carried = {r: self.regs[r] for r in ARGS if r in self.regs}
        if carried:
            self.calls.append((callee, carried))
        for r in list(self.regs):
            if r in CALLER_SAVED:
                self.kill(r)
        for r in list(self.loaded):
            if r in CALLER_SAVED:
                self.loaded.pop(r, None)


def analyse(asm: dict[str, list[str]], function: str, spec: Spec, depth: int = 3) -> Result:
    """Follow `spec` from `function` through every function it reaches in `asm` (function -> instructions)."""
    res = Result()
    if function not in asm:
        raise KeyError(function)
    callers: dict[str, list[str]] = defaultdict(list)
    for name, lines in asm.items():
        for line in lines:
            op, ops = parse(line)
            if op in ("jal", "bal") and ops:
                callers[ops[0]].append(name)
    seen: set[tuple[str, str, int, int]] = set()
    # (function, entry: argN | ret:<callee> | global, level, delta, depth, reached from a caller of the root)
    queue: list[tuple[str, str, int, int, int, bool]] = []
    if spec.root.startswith("arg"):
        queue.append((function, spec.root, 0, 0, 0, False))
    elif spec.root == "return":
        for caller in callers.get(function, []):
            queue.append((caller, f"ret:{function}", 0, 0, 1, False))
        res.flows.add((function, "return"))
    else:
        users = [n for n, lines in asm.items() if any(spec.root in line for line in lines)]
        for n in users:
            queue.append((n, f"global:{spec.root}", 0, 0, 0, False))
    while queue:
        fn, entry, level, delta, d, up = queue.pop(0)
        key = (fn, entry, level, delta)
        if key in seen or fn not in asm:
            continue
        seen.add(key)
        t = Tracker(fn, asm[fn], level, res, spec.chain)
        start: dict[str, tuple[int, int]] = {}
        lines = asm[fn]
        if entry.startswith("arg"):
            start["a" + entry[3:]] = (level, delta)
        elif entry.startswith("ret:"):
            lines = _after_call(lines, entry[4:])
            start["v0"] = (level, delta)
        elif entry.startswith("global:"):
            t.global_root = entry[7:]  # type: ignore[attr-defined]
        t.lines = lines
        t.run(start)
        if fn != function or not entry.startswith("arg"):
            res.flows.add((fn, entry.replace("ret:", "returned by ").replace("global:", "")))
        # A caller reached from the root shows how the root's argument is used there, and stops: its other callees
        # get whatever it has, which is where unrelated structs would come in.
        if d >= depth or up:
            continue
        for callee, carried in t.calls:  # handed on: the callee's argument is the same struct
            for reg, (lvl, dl) in carried.items():
                if dl != 0:
                    continue
                if len(set(callers.get(callee, []))) > SHARED:  # memcpy-like: takes every kind of pointer
                    res.flows.add((callee, f"arg{reg[1]} (shared by many callers: not followed)"))
                    continue
                queue.append((callee, "arg" + reg[1], lvl, 0, d + 1, False))
        if d == 0 and entry.startswith("arg") and level == 0:  # handed in: callers that pass their own argument on
            for caller in callers.get(fn, []):
                for arg in _passes_own_arg(asm[caller], fn, entry):
                    queue.append((caller, arg, 0, 0, d + 1, True))
    return res


SHARED = 12  # a callee with more callers than this is a general helper: a pointer handed to it isn't followed


def _after_call(lines: list[str], callee: str) -> list[str]:
    """The instructions after a call to `callee` (and its delay slot): where v0 holds what it returned."""
    for i, line in enumerate(lines):
        op, ops = parse(line)
        if op in ("jal", "bal") and ops and ops[0] == callee:
            return lines[i + 2:]
    return []


def _passes_own_arg(lines: list[str], callee: str, entry: str) -> list[str]:
    """Which of a caller's own arguments (arg0-arg3) it hands to `callee` as `entry` (argN)."""
    want = "a" + entry[3:]
    out = []
    for own in ARGS:
        res = Result()
        t = Tracker("?", lines, 0, res, ())
        t.run({own: (0, 0)})
        if any(c == callee and want in carried and carried[want] == (0, 0) for c, carried in t.calls):
            out.append("arg" + own[1])
    return out


TYPE_ORDER = ["ptr", "f64", "s64", "f32", "s32", "u16", "s16", "u8", "s8", "u8[]"]


def render(res: Result, function: str, spec_text: str, known: list[tuple[str, str]] | None = None) -> str:
    """The layout as C, with what backs each field."""
    if not res.accesses and not res.pointers:
        return (f"No loads or stores through {function}'s {spec_text} were found (the pointer may be handed straight "
                "to another function, or only compared). Read the assembly: get_function.")
    by_off: dict[int, list[Access]] = defaultdict(list)
    for a in res.accesses:
        by_off[a.offset].append(a)
    for off, fns in res.pointers.items():
        for f in fns:
            by_off[off].append(Access(off, "ptr", 4, f, False))
    fields = []
    for off in sorted(by_off):
        acc = by_off[off]
        # a store says the width, not the sign (sb/sh/sw): the loads decide the type when there are any
        telling = [a for a in acc if not a.store or a.type in ("ptr", "f32", "f64", "u8[]")] or acc
        votes: dict[str, set[str]] = defaultdict(set)
        for a in telling:
            votes[a.type].add(a.function)
        if "ptr" in votes and "s32" in votes:  # a word that's also used as a pointer is the pointer
            votes["ptr"] |= votes.pop("s32")
        width = {a.type: a.size for a in acc}
        best = min(votes, key=lambda t: (-len(votes[t]), TYPE_ORDER.index(t) if t in TYPE_ORDER else 99))
        others = [t for t in votes if t != best]
        fns = sorted({a.function for a in acc})
        fields.append((off, best, width.get(best, 4), fns, others, votes))
    lines = [f"Likely struct behind {function}'s {spec_text} (loads and stores in {len(res.functions)} "
             f"function{'s' if len(res.functions) != 1 else ''}; the judge still decides):", "", "typedef struct {"]
    # a field read inside another (a byte of a word): noted on the field it falls in, not laid out
    laid: list[list] = []
    at = 0
    for off, typ, size, fns, others, votes in fields:
        if off < at and laid:
            laid[-1][6].append(f"0x{off:X} ({typ})")
            continue
        laid.append([off, typ, size, fns, others, votes, []])
        at = off + (4 if typ == "u8[]" else size)
    at = 0
    for off, typ, size, fns, others, votes, inside in laid:
        if off > at:
            lines.append(f"    /* 0x{at:02X} */ char pad{at:02X}[0x{off - at:X}];")
        ctype = "void *" if typ == "ptr" else "u8" if typ == "u8[]" else typ + " "
        name = f"unk_{off:02X}" + ("[4]" if typ == "u8[]" else "")
        who = f"{len(fns)} function{'s' if len(fns) != 1 else ''}: " + ", ".join(fns[:3]) + (", ..." if len(fns) > 3 else "")
        extra = ""
        if others:
            extra = "; also read as " + ", ".join(f"{t} ({', '.join(sorted(votes[t])[:2])})" for t in others)
        if inside:
            extra += "; also accessed inside it at " + ", ".join(inside) + ": smaller fields, or a union"
        if typ == "ptr":
            extra += f"; a pointer: guess_struct(\"{function}\", \"{spec_text}+0x{off:X}\")"
        lines.append(f"    /* 0x{off:02X} */ {ctype}{name}; /* {who}{extra} */")
        at = off + (4 if typ == "u8[]" else size)
    lines += [f"}} Struct_{function.removeprefix('func_')}; /* size >= 0x{at:X} */"]
    flows = sorted(res.flows - {(function, spec_text)})
    if flows:
        lines += ["", "The same pointer: " + ", ".join(f"{f} {how}" for f, how in flows[:12])
                  + (f" (and {len(flows) - 12} more)" if len(flows) > 12 else "")]
    if known:
        lines += ["", "Matched code already has a type for it: "
                  + "; ".join(f"`{t}` ({where})" for t, where in known[:4]) + ". Use that type and add what's missing."]
    lines += ["", "Alignment: the compiler puts s16 on 2 bytes, s32/f32/pointers on 4: your struct must give the same "
                  "offsets. Put what you confirm in the shared header so every agent uses one type."]
    return "\n".join(lines)


_PRIMITIVE = re.compile(r"^(?:const\s+|volatile\s+|unsigned\s+|signed\s+)*(?:void|char|short|int|long|float|double|"
                        r"[us](?:8|16|32|64)|f(?:32|64)|size_t)\b")


def param_type(source: str, name: str, index: int) -> str | None:
    """The type of parameter `index` (0-based) in `name`'s definition, when it's a pointer to a non-primitive type
    (`Actor *`): what matched code already calls the struct."""
    m = re.search(rf"\b{re.escape(name)}\s*\(", source)
    if not m:
        return None
    depth, i, start = 1, m.end(), m.end()
    while i < len(source) and depth:
        depth += {"(": 1, ")": -1}.get(source[i], 0)
        i += 1
    params, depth, cur = [], 0, ""
    for ch in source[start:i - 1]:
        if ch == "," and depth == 0:
            params.append(cur)
            cur = ""
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        cur += ch
    params.append(cur)
    if index >= len(params) or "*" not in params[index]:
        return None
    p = re.sub(r"\s+", " ", params[index]).strip()
    typ = re.sub(r"\s*\b[A-Za-z_]\w*\s*(?:\[[^\]]*\])?$", "", p).strip() if not p.endswith("*") else p
    typ = typ.replace(" *", "*").replace("*", " *").strip()
    if not typ or _PRIMITIVE.match(typ.removeprefix("struct ")) or typ.count("*") > 1:
        return None
    return typ
