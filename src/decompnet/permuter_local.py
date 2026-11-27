"""Local hypotheses the permuter tries before its random rewrites: a handful of cheap, deterministic changes that
often decide register allocation and scheduling in old GCCs without changing what the code does.

- two consecutive stores of constants through the same pointer, in the other order (`p->a = 0; p->b = 1;`): the
  compiler schedules the stores as written;
- the operands of a commutative operator between two plain operands (`a + b`, `x * 3`, `m & k`), swapped: which
  value ends in the destination register follows the operand order;
- two consecutive declarations of locals, in the other order: GCC 2.x hands out registers in declaration order.

Each variant keeps the statements' meaning and leaves everything else as written; the judge alone decides whether one
is closer. Statements across a call, a branch, a label or a preprocessor line are never reordered.
"""

from __future__ import annotations

import itertools
import re

from .forks import csource

MAX = 12

_CONST_STORE = re.compile(r"^([ \t]+)([A-Za-z_]\w*(?:->|\.)[A-Za-z_]\w*(?:\[[^\]\n]*\])?)\s*=\s*"
                          r"(-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)[uUlLfF]*)\s*;[ \t]*$", re.MULTILINE)
_DECL = re.compile(r"^([ \t]+)((?:register\s+|unsigned\s+|signed\s+|const\s+|volatile\s+)*[A-Za-z_]\w*\s*\**\s*"
                   r"[A-Za-z_]\w*(?:\[[^\]\n]*\])?\s*;)[ \t]*$", re.MULTILINE)
_OPERAND = r"(?:[A-Za-z_]\w*(?:->[A-Za-z_]\w*|\.[A-Za-z_]\w*|\[[A-Za-z_0-9]+\])*|\d+|0[xX][0-9a-fA-F]+)"
_COMMUTE = re.compile(rf"(?<![\w.>\]\)])({_OPERAND})\s*([+*&|^])\s*({_OPERAND})(?![\w(\[]|\s*[*/%(\[])")
_NOT_A_TYPE = {"return", "goto", "break", "continue", "case", "default", "else", "do", "if", "while", "for", "switch"}


def _base(lvalue: str) -> str:
    return re.split(r"->|\.", lvalue, maxsplit=1)[0]


def variants(source: str, name: str) -> list[tuple[str, str]]:
    """[(what changed, the whole source with it)]: at most MAX, each one change to `name`'s body."""
    span = csource.find(source, name.split("@")[0])
    if not span:
        return []
    body_at = source.find("{", span[0], span[1])
    if body_at < 0:
        return []
    body = source[body_at:span[1]]
    if re.search(r"^\s*#", body, re.MULTILINE) or "asm" in body:
        return []
    out: list[tuple[str, str]] = []

    def put(kind: str, start: int, end: int, text: str) -> None:
        start, end = body_at + start, body_at + end
        new = source[:start] + text + source[end:]
        if new != source and all(new != v for _, v in out):
            out.append((kind, new))

    # 1. consecutive constant stores through one pointer
    stores = list(_CONST_STORE.finditer(body))
    for a, b in itertools.pairwise(stores):
        between = body[a.end():b.start()]
        if between.strip() or a.group(1) != b.group(1) or a.group(2) == b.group(2) or _base(a.group(2)) != _base(b.group(2)):
            continue
        put("constant stores swapped", a.start(), b.end(), body[b.start():b.end()] + between + body[a.start():a.end()])
    # 2. consecutive local declarations
    decls = [m for m in _DECL.finditer(body) if m.group(2).split()[0] not in _NOT_A_TYPE]
    for a, b in itertools.pairwise(decls):
        between = body[a.end():b.start()]
        if between.strip() or a.group(1) != b.group(1):
            continue
        put("declarations swapped", a.start(), b.end(), body[b.start():b.end()] + between + body[a.start():a.end()])
    # 3. commutative operands, only where `a OP b` is a whole expression (precedence can't regroup it): right after
    #    `=` (not ==, !=, <=, >=), `(`, `,`, `[` or `return`, and right before `;`, `)`, `,` or `]`
    for m in _COMMUTE.finditer(body):
        before = body[:m.start()].rstrip()
        after = body[m.end():].lstrip()
        opens = (before.endswith(("(", ",", "[")) or re.search(r"\breturn$", before)
                 or (before.endswith("=") and not before.endswith(("==", "!=", "<=", ">="))))
        if not opens or not after[:1] or after[0] not in ";),]" or m.group(1) == m.group(3):
            continue
        put(f"operands of {m.group(2)} swapped", m.start(), m.end(), f"{m.group(3)} {m.group(2)} {m.group(1)}")
        if len(out) >= MAX:
            break
    return out[:MAX]
