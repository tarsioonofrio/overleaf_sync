"""
Tests for UTF-16 position conversion at the Overleaf wire boundary.

The simulated server indexes strings in UTF-16 code units, like JS/ShareJS.
"""

from __future__ import annotations

import random

import pytest

from overleaf_sync import apply_wire_ops, compute_ot_ops, ops_to_wire


def _u16(s: str) -> bytes:
    return s.encode("utf-16-le", "surrogatepass")


def js_server_apply(content: str, ops: list[dict]) -> str:
    """Apply ops one by one with positions in UTF-16 code units."""
    buf = _u16(content)
    for op in ops:
        b = 2 * op["p"]
        if "i" in op:
            buf = buf[:b] + _u16(op["i"]) + buf[b:]
        elif "d" in op:
            removed = buf[b:b + len(_u16(op["d"]))]
            assert removed == _u16(op["d"]), f"server would reject: deleting {removed!r} != {op['d']!r}"
            buf = buf[:b] + buf[b + len(_u16(op["d"])):]
    return buf.decode("utf-16-le", "surrogatepass")


CASES = [
    ("plain ascii", "plain ASCII edit"),
    ("🎉 party", "🎉 big party"),
    ("a🎉b🎉c", "a🎉b🎉X🎉c"),
    ("𝔸 math 𝔹 here", "𝔸 math 𝔹 there"),
    ("emoji 🎉 then text", "emoji 🎉 then"),
    ("💥💥💥", "💥💥"),
    ("’ BMP only ’ é", "’ BMP only ’ è"),
    ("start 🎉", "🎉 start 🎉"),
    ("", "🎉"),
    ("🎉", ""),
]


@pytest.mark.parametrize("old,new", CASES)
def test_outgoing_ops_land_correctly_on_utf16_server(old, new):
    ops = ops_to_wire(old, compute_ot_ops(old, new))
    assert js_server_apply(old, ops) == new


@pytest.mark.parametrize("old,new", CASES)
def test_incoming_wire_ops_apply_correctly_locally(old, new):
    # Ops as the server would broadcast them (UTF-16 positions).
    wire = ops_to_wire(old, compute_ot_ops(old, new))
    assert apply_wire_ops(old, wire) == new


def test_old_behaviour_was_wrong():
    old, new = "🎉 party", "🎉 big party"
    raw = compute_ot_ops(old, new)
    assert js_server_apply(old, raw) != new


def test_incoming_multi_op_sequential():
    # Server sends two ops in one update; second sees the result of the first.
    content = "a🎉b"
    ops = [{"p": 3, "i": "🎉"}, {"p": 5, "i": "X"}]  # after "a🎉" (3 units), then after "a🎉🎉" (5)
    assert apply_wire_ops(content, ops) == js_server_apply(content, ops) == "a🎉🎉Xb"


@pytest.mark.parametrize("seed", range(200))
def test_random_roundtrip(seed):
    rng = random.Random(seed)
    alphabet = "ab \n’é🎉𝔸💥"
    old = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
    new = list(old)
    for _ in range(rng.randint(1, 4)):
        pos = rng.randint(0, len(new))
        if new and rng.random() < 0.5:
            del new[pos:pos + rng.randint(1, 3)]
        else:
            new[pos:pos] = rng.choices(alphabet, k=rng.randint(1, 3))
    new_s = "".join(new)
    wire = ops_to_wire(old, compute_ot_ops(old, new_s))
    assert js_server_apply(old, wire) == new_s
    assert apply_wire_ops(old, wire) == new_s


# ── non-BMP sanitization (Overleaf stores each non-BMP char as U+FFFD U+FFFD) ──

from overleaf_sync import OverleafSync, sanitize_non_bmp  # noqa: E402


def overleaf_server_store(content: str) -> str:
    """What the live server stores (verified 2026-10-07)."""
    return "".join("��" if ord(c) > 0xFFFF else c for c in content)


@pytest.mark.parametrize("text", ["plain", "a🎉b", "𝔸’é💥", "", "��"])
def test_sanitize_matches_server(text):
    assert sanitize_non_bmp(text) == overleaf_server_store(text)


def _sync_with_file(tmp_path, synced: str, local: str) -> tuple[OverleafSync, object]:
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    (tmp_path / "main.tex").write_text(local, encoding="utf-8")
    sync._docs["d1"] = {"content": synced, "version": 1, "path": "main.tex"}
    return sync, tmp_path / "main.tex"


def test_local_emoji_rewritten_and_pushed_as_server_form(tmp_path):
    sync, f = _sync_with_file(tmp_path, "hello", "hello 🎉 there")
    changed = sync._collect_local_changes()
    expected = "hello �� there"
    assert changed == [("d1", "hello", expected)]
    assert f.read_text(encoding="utf-8") == expected
    # Invariant: after the push the server holds exactly the local file.
    ops = ops_to_wire("hello", compute_ot_ops("hello", expected))
    assert overleaf_server_store(js_server_apply("hello", ops)) == f.read_text(encoding="utf-8")


def test_emoji_only_change_that_matches_synced_is_not_pushed(tmp_path):
    # Synced state already holds the server form; user re-pastes an emoji over it.
    sync, f = _sync_with_file(tmp_path, "x��y", "x🎉y")
    assert sync._collect_local_changes() == []
    assert f.read_text(encoding="utf-8") == "x��y"


def test_bmp_only_file_untouched(tmp_path):
    sync, f = _sync_with_file(tmp_path, "a", "a’é")
    assert sync._collect_local_changes() == [("d1", "a", "a’é")]
    assert f.read_text(encoding="utf-8") == "a’é"
