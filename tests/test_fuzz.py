"""Randomized property tests for the OT / diff / merge primitives."""

from __future__ import annotations

import random

import pytest

from overleaf_sync import (
    apply_sharejs_ops, apply_wire_ops, compute_ot_ops, merge3, ops_to_wire,
    sanitize_non_bmp, transform_x,
)

ALPHABET = "abc \n\tçã漢é​."          # BMP only (non-BMP has its own test)
ROUNDS = 400


def rnd_text(r: random.Random, n: int) -> str:
    return "".join(r.choice(ALPHABET) for _ in range(n))


def rnd_edit(r: random.Random, doc: str) -> str:
    """Apply 1-3 random insert/delete/replace edits."""
    for _ in range(r.randint(1, 3)):
        p = r.randint(0, len(doc))
        kind = r.choice("idr")
        if kind in "dr" and doc:
            q = min(len(doc), p + r.randint(1, 6))
            doc = doc[:p] + (rnd_text(r, r.randint(1, 5)) if kind == "r" else "") + doc[q:]
        else:
            doc = doc[:p] + rnd_text(r, r.randint(1, 5)) + doc[p:]
    return doc


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_diff_roundtrip(seed):
    r = random.Random(seed)
    old = rnd_text(r, r.randint(0, 60))
    new = rnd_edit(r, old) if r.random() < 0.8 else rnd_text(r, r.randint(0, 60))
    assert apply_sharejs_ops(old, compute_ot_ops(old, new)) == new


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_wire_roundtrip_with_non_bmp(seed):
    r = random.Random(seed)
    pool = ALPHABET + "😀𝔸"
    old = "".join(r.choice(pool) for _ in range(r.randint(0, 40)))
    new = "".join(r.choice(pool) for _ in range(r.randint(0, 40)))
    new = sanitize_non_bmp(new)   # what Overleaf will store
    old_s = sanitize_non_bmp(old)
    ops = compute_ot_ops(old_s, new)
    assert apply_wire_ops(old_s, ops_to_wire(old_s, ops)) == new


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_transform_converges(seed):
    """TP1: apply(apply(d, a), b') == apply(apply(d, b), a')."""
    r = random.Random(seed)
    doc = rnd_text(r, r.randint(0, 50))
    a = compute_ot_ops(doc, rnd_edit(r, doc))
    b = compute_ot_ops(doc, rnd_edit(r, doc))
    a_t, b_t = transform_x(a, b)
    left = apply_sharejs_ops(apply_sharejs_ops(doc, a), b_t)
    right = apply_sharejs_ops(apply_sharejs_ops(doc, b), a_t)
    assert left == right


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_merge3_keeps_pure_insertions_of_both_sides(seed):
    r = random.Random(seed)
    base = rnd_text(r, r.randint(5, 50))
    p1, p2 = r.randint(0, len(base)), r.randint(0, len(base))
    x, y = "<<LOCAL>>", "<<SERVER>>"
    mine = base[:p1] + x + base[p1:]
    theirs = base[:p2] + y + base[p2:]
    merged = merge3(base, mine, theirs)
    assert x in merged and y in merged
    assert merged.replace(x, "").replace(y, "") == base


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_merge3_identity_cases(seed):
    r = random.Random(seed)
    base = rnd_text(r, r.randint(0, 50))
    changed = rnd_edit(r, base)
    assert merge3(base, base, changed) == changed     # only server changed
    assert merge3(base, changed, base) == changed     # only local changed
    # (both sides making the same edit is NOT idempotent: OT cannot tell it is the same)


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_merge3_never_loses_untouched_text(seed):
    """Text that neither side touched must survive any merge."""
    r = random.Random(seed)
    head, tail = "HEAD-", "-TAIL"   # outside ALPHABET, so diff boundaries are unambiguous
    mid = rnd_text(r, r.randint(0, 30))
    base = head + mid + tail
    mine = head + rnd_text(r, r.randint(0, 20)) + tail
    theirs = head + rnd_text(r, r.randint(0, 20)) + tail
    merged = merge3(base, mine, theirs)
    assert merged.startswith(head) and merged.endswith(tail)


# --- distant edits stay separate hunks (a coauthor's text must keep its place)

import time

from overleaf_sync import wire_to_cp  # noqa: F401


def _doc(n=300):
    return "".join(f"line {i} of the paper\n" for i in range(n))


def test_distant_edits_are_separate_ops_not_one_block():
    old = _doc()
    new = old.replace("line 10 of", "line 10 EDITED of").replace("line 280 of", "line 280 EDITED of")
    ops = compute_ot_ops(old, new)
    assert apply_sharejs_ops(old, ops) == new
    assert len(ops) == 2
    assert sum(len(o.get("i", "")) + len(o.get("d", "")) for o in ops) < 40


def test_coauthor_text_between_two_local_edits_keeps_its_place():
    base = _doc()
    mine = base.replace("line 10 of", "line 10 MINE of").replace("line 280 of", "line 280 MINE of")
    theirs = base.replace("line 150 of the paper\n", "line 150 of the paper\nCOAUTHOR SENTENCE\n")
    merged = merge3(base, mine, theirs)
    assert merged == theirs.replace("line 10 of", "line 10 MINE of").replace("line 280 of", "line 280 MINE of")
    lines = merged.split("\n")
    assert lines[lines.index("COAUTHOR SENTENCE") - 1] == "line 150 of the paper"


def test_repeated_lines_still_roundtrip():
    old = "\n".join(["", "\\item x", ""] * 400)
    new = old.replace("\\item x", "\\item y", 3) + "\nend"
    assert apply_sharejs_ops(old, compute_ot_ops(old, new)) == new


def test_large_document_diff_is_fast():
    old = _doc(40000)
    new = old.replace("line 100 of", "line 100 EDITED of").replace("line 39000 of", "line 39000 EDITED of")
    t0 = time.time()
    ops = compute_ot_ops(old, new)
    assert time.time() - t0 < 2.0
    assert len(ops) == 2 and apply_sharejs_ops(old, ops) == new


def test_large_document_with_many_blank_lines_is_fast():
    old = "\n\n".join(f"paragraph {i}" for i in range(20000))
    new = old.replace("paragraph 5\n", "paragraph 5 EDIT\n").replace("paragraph 19000", "paragraph 19000 EDIT")
    t0 = time.time()
    ops = compute_ot_ops(old, new)
    assert time.time() - t0 < 2.0
    assert apply_sharejs_ops(old, ops) == new


@pytest.mark.parametrize("seed", range(300))
def test_multiline_roundtrip_and_convergence(seed):
    r = random.Random(seed)
    words = ["alpha", "beta", "\\begin{x}", "\\end{x}", "", "gamma delta", "%c"]
    old = "\n".join(r.choice(words) for _ in range(r.randint(0, 40)))
    def edit(doc):
        lines = doc.split("\n")
        for _ in range(r.randint(1, 5)):
            k = r.randint(0, len(lines))
            if lines and r.random() < 0.4:
                del lines[min(k, len(lines) - 1)]
            elif lines and r.random() < 0.3:
                lines[min(k, len(lines) - 1)] += " tail"
            else:
                lines.insert(k, r.choice(words) + str(r.randint(0, 9)))
        return "\n".join(lines)
    a, b = edit(old), edit(old)
    oa, ob = compute_ot_ops(old, a), compute_ot_ops(old, b)
    assert apply_sharejs_ops(old, oa) == a and apply_sharejs_ops(old, ob) == b
    at, bt = transform_x(oa, ob)
    assert apply_sharejs_ops(apply_sharejs_ops(old, oa), bt) == apply_sharejs_ops(apply_sharejs_ops(old, ob), at)
