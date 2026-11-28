"""Scopes (app/scopes.py): each part of the game with its own %, from the units, the SDK calls and the code."""

from __future__ import annotations

import json

from decompnet.app import scopes

from .test_quota_failover import W, app  # noqa: F401  (the app fixture)


def _row(name, unit, size=100, status="open"):
    return {"name": name, "symbol": name, "unit": unit, "size": size, "status": status}


def _game():
    rows = [_row("main", "yuugi/main", 40, "matched"), _row("draw_menu", "etc/menu", 200, "matched"),
            _row("menu_helper", "etc/menu", 100), _row("shared", "yuugi/util", 50, "matched"),
            _row("play_sfx", "yuugi/sound", 300), _row("print_text", "yuugi/text", 120),
            _row("text_glyph", "yuugi/text", 80, "matched"), _row("boot", "slpm/boot", 60, "asm"),
            _row("PadRead", "yuugi/sdk", 30, "library"), _row("slpm_SpuSetKey", "slpm/sdk", 30, "matched")]
    asm = {
        "main": ["jal draw_menu", "jal play_sfx", "jal shared", "jal print_text"],
        "draw_menu": ["jal DrawPrim", "jal menu_helper", "jal shared", "jal PadRead"],
        "menu_helper": ["jal ClearOTagR"],
        "shared": ["jr ra"],
        "play_sfx": ["jal slpm_SpuSetKey", "jal shared"],
        "print_text": ["jal text_glyph"],
        # Shift-JIS: a byte below 0xA0 is a one-byte character, then the 0xE0.. lead bytes
        "text_glyph": ["lbu v0, 0x0(a0)", "sltiu v1, v0, 0xa0", "sltiu v1, v0, 0xe0", "jal LoadImage"],
    }
    return rows, asm


def _by_key(out):
    return {s["key"]: s for s in out}


def test_every_scope_counts_what_can_become_c():
    rows, asm = _game()
    out = _by_key(scopes.compute(rows, asm, platform="ps"))
    # all: matched and open functions (not asm, not library status), the SDK's C included; 5 of 8 matched
    assert (out["all"]["matched"], out["all"]["functions"]) == (5, 8)
    assert out["all"]["percent"] == round(100 * (40 + 200 + 50 + 80 + 30) / (40 + 200 + 100 + 50 + 300 + 120 + 80 + 30), 2)
    assert out["all"]["other"] == 2 and out["all"]["left"] == 3  # boot (asm) and PadRead (library)
    assert [f["name"] for f in out["all"]["open"]] == ["play_sfx", "print_text", "menu_helper"]  # largest first
    # the game's code leaves the SDK (by name, also a copy prefixed per executable) apart
    assert out["game"]["functions"] == 7 and "slpm_SpuSetKey" not in str(out["game"])


def test_binaries_come_from_the_units_first_folder():
    rows, asm = _game()
    out = _by_key(scopes.compute(rows, asm, platform="ps", binaries={"etc": "ETC.BIN (menus)"}))
    assert out["bin:etc"]["label"] == "ETC.BIN (menus)" and out["bin:etc"]["functions"] == 2
    assert out["bin:etc"]["percent"] == round(100 * 200 / 300, 2)
    assert out["bin:yuugi"]["label"] == "yuugi"
    assert "bin:slpm" in out  # only asm/library there, but the binary counts them as "other"... when it has C
    assert out["bin:slpm"]["functions"] == 1 and out["bin:slpm"]["other"] == 1


def test_areas_follow_the_sdk_calls_and_the_helpers_only_they_call():
    rows, asm = _game()
    out = _by_key(scopes.compute(rows, asm, platform="ps"))
    names = lambda k: {f["name"] for f in out[k]["open"]}  # noqa: E731
    # graphics: who calls DrawPrim/ClearOTagR/LoadImage; `shared` is called from outside too, so it isn't theirs
    assert out["graphics"]["functions"] == 3 and names("graphics") == {"menu_helper"}
    assert out["sound"]["functions"] == 1 and names("sound") == {"play_sfx"}  # slpm_SpuSetKey is SpuSetKey
    assert out["input"]["functions"] == 2  # draw_menu calls PadRead (a library function), and its own helper
    assert "3d" not in out  # nothing calls the GTE: no empty scope


def test_text_is_the_code_that_reads_shift_jis_and_who_calls_it():
    rows, asm = _game()
    out = _by_key(scopes.compute(rows, asm, platform="ps"))
    assert out["text"]["functions"] == 2 and {f["name"] for f in out["text"]["open"]} == {"print_text"}
    assert not scopes.decodes_sjis(["lw v0, 0xa0(a1)", "sw v0, 0xe0(a1)"])  # offsets aren't lead-byte tests
    assert not scopes.decodes_sjis(["sltiu v1, v0, 0xa0", "sltiu v1, v0, 0xe0"])  # no byte read


def test_other_platforms_get_code_and_binaries_but_no_sdk_areas():
    rows, asm = _game()
    out = _by_key(scopes.compute(rows, asm, platform="n64"))
    assert "game" not in out and "graphics" not in out and "bin:etc" in out
    assert "text" in out  # (the Shift-JIS test is the code's own, not the SDK's)


def test_the_focus_the_manifests_and_your_own_scopes():
    rows, asm = _game()
    out = scopes.compute(rows, asm, platform="ps", focus={"name": "Texto e menus", "functions": ["draw_menu", "print_text"]},
                         project=[{"name": "Menus", "units": ["etc/*"]}],
                         mine=[{"name": "Main", "functions": ["main"], "helpers": True}, {"name": "Nothing", "units": ["zzz/*"]}],
                         names_of="mine:Main")
    by = _by_key(out)
    assert by["focus"]["label"] == "Texto e menus" and (by["focus"]["matched"], by["focus"]["functions"]) == (1, 2)
    assert by["project:Menus"]["functions"] == 2 and not by["project:Menus"]["removable"]
    # main and everything only main's code calls: here the whole game (not the SDK)
    assert by["mine:Main"]["removable"] and set(by["mine:Main"]["names"]) == {
        "main", "draw_menu", "menu_helper", "shared", "play_sfx", "print_text", "text_glyph"}
    assert by["mine:Nothing"]["functions"] == 0  # your own stay, even empty
    assert "names" not in by["all"]


def test_sdk_names():
    assert scopes.sdk_name("slpm_PadRead") == "PadRead" and scopes.sdk_name("func_80012345") == "func_80012345"
    assert scopes.ps_area("raise_SpuInit") == "sound" and scopes.ps_area("CdReadCallback") == "cd"
    assert scopes.ps_area("DMACallback") == "system" and scopes.ps_area("func_80012345") is None
    assert scopes.ps_library("memcpy") and scopes.ps_library("__main") and not scopes.ps_library("main")


def test_the_page_lists_scopes_and_keeps_yours(app):  # noqa: F811
    client, net, _, _ = app
    pid = net.project_row("game")["id"]
    with net.store.tx() as db:
        for name, size, status in (("func_A", 100, "matched"), ("func_B", 300, "open"), ("func_C", 50, "asm")):
            db.execute("INSERT INTO functions(project_id,name,vram,size,status) VALUES(?,?,?,?,?)", (pid, name, 0, size, status))
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps({"kind": "rom", "focus": {"name": "Texto", "functions": ["func_B"]}}), pid))
    got = client.get("/api/scopes?slug=game", headers=W).json()
    by = {s["key"]: s for s in got["scopes"]}
    assert by["all"]["percent"] == 25.0 and by["all"]["other"] == 1 and by["focus"]["label"] == "Texto"
    r = client.post("/api/scopes", json={"slug": "game", "action": "add", "name": "Mine", "match": "func_A, func_C"}, headers=W)
    mine = {s["key"]: s for s in r.json()["scopes"]}["mine:Mine"]
    assert (mine["matched"], mine["functions"], mine["other"]) == (1, 1, 1)
    assert client.post("/api/scopes", json={"slug": "game", "action": "add", "name": "", "match": "x"}, headers=W).status_code >= 400
    r = client.post("/api/scopes", json={"slug": "game", "action": "remove", "key": "mine:Mine"}, headers=W)
    assert "mine:Mine" not in {s["key"] for s in r.json()["scopes"]}
