"""Scopes: parts of a game to follow on their own, each with its own % (the overview's Scopes panel).

Everything comes from what the project already has, without a build:
- code: all of it, and (PlayStation) the game's own code apart from the console's SDK;
- binary: the first folder of a unit's name (yuugi/…, etc/…): one per executable or overlay of a multi-binary decomp;
- area: what the code does, from the SDK functions it calls (graphics, sound, CD…) and, for text, from the code itself
  (it tells Shift-JIS lead bytes apart); a helper only that area's code calls belongs to it too;
- the project's own: the manifest's "scopes", and the ones the user adds on the page (a name and the units or
  functions it covers, as names or globs).
Areas are hints, not proof: each says how it was found. A function can be in several scopes.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

# PlayStation SDK (PsyQ) names -> area; the first area a name matches is its area. A prefixed copy of a library
# function (slpm_PadRead, raise_SpuInit: one per executable) counts as the function.
PS_AREAS = (
    ("graphics", "Graphics (GPU)", r"(?:Load|Store|Move|Clear)Image2?|(?:Clear|Draw)OTag\w*|DrawPrim|AddPrims?|"
     r"SetPoly\w+|SetSprt\w*|SetLine\w+|SetTile\w*|(?:Put|Get|SetDef)(?:Draw|Disp)Env|SetDraw\w+|ResetGraph|"
     r"DrawSync\w*|GetTPage|GetClut|LoadTPage|LoadClut2?|SetDispMask|SetSemiTrans|SetShadeTex|SetTexWindow|"
     r"(?:Open|Read)TIM|[SG]etGraph\w+|GetODE|[SG]etVideoMode|Fnt\w+|SetDumpFnt|Krom2\w+|Kanji\w+"),
    ("3d", "3D math (GTE)", r"RotTrans\w*|MulMatrix\w*|RotMatrix\w*|ScaleMatrix\w*|TransMatrix|CompMatrix|"
     r"Set(?:Rot|Trans|Light|Color)Matrix|ReadRotMatrix|(?:Push|Pop)Matrix|InitGeom|SetGeom\w+|NormalClip|"
     r"NormalColor\w*|Average\w+|Square\w+|OuterProduct\w+|Dpq\w*|Color(?:Col|Dpq)|LocalLight|LightColor|"
     r"SetBackColor|Intpl|Lzc|(?:Open|Read)TMD|RotAverage\w+|rsin|rcos|ratan2|sin_1|_patch_gte"),
    ("sound", "Sound (SPU)", r"_?Spu\w+|_?spu_\w+|_?Ss\w+"),
    ("cd", "CD and files", r"Cd\w+|CD_\w+|Ds\w+|firstfile|PC(?:open|creat|lseek|read|write|close)|_96_\w+"),
    ("video", "Video (MDEC, streaming)", r"DecDCT\w*|St(?:SetRing|ClearRing|FreeRing|GetBackloc|GetNext|SetStream|"
     r"UnSetRing|CdInterrupt)|init_ring_status"),
    ("input", "Controller", r"\w*PAD\w*|Pad\w+|_patch_pad|\w*InitPadFlag"),
    ("card", "Memory card", r"\w*CARD\w*|_card_\w+|_patch_card\w*|_copy_memcard_patch|_ExitCard"),
    ("system", "System (timers, interrupts)", r"VSync\w*|\w*Callback\w*|\w*RCnt|\w*IntrMask|ER_\w+|setjmp|longjmp"),
)
# the C library and compiler support the SDK links in: not the game's code either
PS_LIBC = (r"(?:mem|str)\w+|s?printf|puts|putchar|_putchar\w*|prnt|ato[il]|bcopy|bzero|s?rand|abs|to(?:upper|lower)|"
           r"__\w+|_mainasu|_err_math|_dbl_shift\w*|_\w+_mant\w*|_SN_\w+|exit|malloc|free|calloc|realloc|InitHeap\w*")
_PS = [(k, label, re.compile(rf"(?:{rx})")) for k, label, rx in PS_AREAS]
_PS_LIBC = re.compile(rf"(?:{PS_LIBC})")

_CALL = re.compile(r"^(?:jal|j|bal)\s+([A-Za-z_]\w*)\b")
_SLT = re.compile(r"^slti?u?\s+\S+,\s*\S+,\s*(0x[0-9a-fA-F]+|\d+)\s*$")
_LB = re.compile(r"^lbu?\s")
DECODABLE = ("matched", "open")  # what the % is over (as the focus counts: not asm, library or code no file covers)


def sdk_name(name: str) -> str:
    """The library function a name is (slpm_PadRead is PadRead)."""
    m = re.match(r"^[a-z][a-z0-9]*_(?=[A-Z_])", name)
    return name[m.end():] if m else name


def ps_area(name: str) -> str | None:
    for n in {name, sdk_name(name)}:
        for key, _label, rx in _PS:
            if rx.fullmatch(n):
                return key
    return None


def ps_library(name: str) -> bool:
    return ps_area(name) is not None or any(_PS_LIBC.fullmatch(n) for n in {name, sdk_name(name)})


def decodes_sjis(insns: list[str]) -> bool:
    """Code that tells Shift-JIS lead bytes apart (a byte below 0xA0, then below 0xE0): text."""
    if not any(_LB.match(i) for i in insns):
        return False
    seen = {int(m.group(1), 0) for i in insns if (m := _SLT.match(i.strip()))}
    return 0xA0 in seen and (0xE0 in seen or 0xF0 in seen)


@dataclass
class Scope:
    key: str
    label: str
    group: str  # code | binary | area | project | mine
    how: str
    names: set[str] = field(default_factory=set)
    removable: bool = False


def _helpers(members: set[str], calls: dict[str, set[str]], callers: dict[str, set[str]], own: set[str]) -> set[str]:
    """`members` plus every function of the game only they call (and so on down)."""
    out = set(members)
    todo = [c for m in members for c in calls.get(m, ())]
    while todo:
        f = todo.pop()
        if f in out or f not in own:
            continue
        by = callers.get(f) or set()
        if by and by <= out:
            out.add(f)
            todo.extend(calls.get(f, ()))
    return out


def _matches(spec: dict, row: dict) -> bool:
    names = (row["name"], row["symbol"])
    return (any(fnmatch.fnmatchcase(row["unit"] or "", g) for g in spec.get("units") or ())
            or any(fnmatch.fnmatchcase(n, g) for g in spec.get("functions") or () for n in names))


def compute(rows: list[dict], asm: dict[str, list[str]], *, platform: str = "", project: list[dict] | None = None,
            binaries: dict[str, str] | None = None, mine: list[dict] | None = None,
            focus: dict | None = None, names_of: str | None = None) -> list[dict]:
    """Every scope with its numbers. rows: the project's functions (name, symbol, unit, size, status); asm: symbol ->
    target instructions (what's been read so far); project/mine: {"name", "units": [glob], "functions": [glob],
    "helpers": bool} from the manifest and from the user; focus: the project's focus {"name", "functions"}."""
    by_sym = {r["symbol"]: r for r in rows}
    ps = platform == "ps"
    calls: dict[str, set[str]] = {}
    callers: dict[str, set[str]] = {}
    for sym, insns in asm.items():
        if sym not in by_sym or not isinstance(insns, list):
            continue
        out = {m.group(1) for i in insns if (m := _CALL.match(i.strip()))} - {sym}
        calls[sym] = out
        for c in out:
            callers.setdefault(c, set()).add(sym)
    sdk = {s for s in by_sym if ps and (by_sym[s]["status"] == "library" or ps_library(s))}
    game = set(by_sym) - sdk
    scopes = [Scope("all", "All code", "code", "every function the project knows", set(by_sym))]
    if focus and focus.get("functions"):
        wanted = set(focus["functions"])
        scopes.append(Scope("focus", str(focus.get("name") or "Focus"), "focus",
                            "the project's focus: the functions you chose to see done first",
                            {s for s, r in by_sym.items() if r["name"] in wanted or s in wanted}))
    if ps:
        scopes.append(Scope("game", "Game code (no SDK)", "code",
                            "without the PsyQ SDK and C library functions (by name)", game))
        scopes.append(Scope("sdk", "PsyQ SDK", "code", "the console's libraries, linked into each executable", sdk))
    folders: dict[str, set[str]] = {}
    for s, r in by_sym.items():
        unit = r["unit"] or ""
        if "/" in unit:
            folders.setdefault(unit.split("/", 1)[0], set()).add(s)
    if len(folders) > 1:
        for f in sorted(folders):
            scopes.append(Scope(f"bin:{f}", (binaries or {}).get(f) or f, "binary", f"the units under {f}/", folders[f]))
    if ps:
        for key, label, _rx in _PS:
            direct = {f for f in game if any(ps_area(c) == key for c in calls.get(f, ()))}
            if direct:
                scopes.append(Scope(key, label, "area", f"{len(direct)} functions call its SDK functions; "
                                    "with the helpers only they call", _helpers(direct, calls, callers, game)))
    seeds = {s for s in game if decodes_sjis(asm.get(s) or [])}
    if seeds:
        users = {c for s in seeds for c in callers.get(s, ()) if c in game}
        scopes.append(Scope("text", "Text (Shift-JIS)", "area",
                            f"{len(seeds)} functions decode Shift-JIS characters, the {len(users)} that call them, "
                            "and the helpers only they call", _helpers(seeds | users, calls, callers, game)))
    for group, specs in (("project", project or []), ("mine", mine or [])):
        for spec in specs:
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            names = {s for s, r in by_sym.items() if _matches(spec, r)}
            if spec.get("helpers"):
                names = _helpers(names, calls, callers, game)
            what = ", ".join([*(spec.get("units") or []), *(spec.get("functions") or [])])
            scopes.append(Scope(f"{group}:{spec['name']}", str(spec["name"]), group, what or "(empty)", names,
                                removable=group == "mine"))
    out = [_numbers(s, by_sym) | ({"names": sorted(by_sym[n]["name"] for n in s.names)} if s.key == names_of else {})
           for s in scopes]
    return [o for o in out if o["functions"] or o["group"] in ("project", "mine", "focus")]


def _numbers(s: Scope, by_sym: dict[str, dict]) -> dict:
    rows = [by_sym[n] for n in s.names if n in by_sym]
    todo = [r for r in rows if r["status"] in DECODABLE]
    done = [r for r in todo if r["status"] == "matched"]
    nbytes, mbytes = sum(r["size"] or 0 for r in todo), sum(r["size"] or 0 for r in done)
    left = sorted((r for r in todo if r["status"] != "matched"), key=lambda r: -(r["size"] or 0))
    return {"key": s.key, "label": s.label, "group": s.group, "how": s.how, "removable": s.removable,
            "functions": len(todo), "matched": len(done), "bytes": nbytes, "matched_bytes": mbytes,
            "percent": round(100 * mbytes / nbytes, 2) if nbytes else None,
            "left": len(todo) - len(done), "left_bytes": nbytes - mbytes,
            "other": len(rows) - len(todo),  # asm, library or not yet in a file: not counted
            "open": [{"name": r["name"], "size": r["size"] or 0} for r in left[:200]]}
