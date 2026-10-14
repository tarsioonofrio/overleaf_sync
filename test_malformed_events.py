"""Garbage from the server (or a diverged copy) must raise — so the handler hook reconnects — never silently corrupt state."""

from __future__ import annotations

import copy
import random
import threading

import pytest

import overleaf_sync
from overleaf_sync import OverleafSync

BASE = "alpha\nbeta\ngamma\n"


def make(tmp_path, inflight=False):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    (tmp_path / "main.tex").write_text(BASE)
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    if inflight:
        sync._docs["d1"]["inflight"] = {"ops": [{"p": 5, "i": "X"}], "base": BASE, "done": threading.Event(), "v": 5}
    return sync


def snapshot(sync):
    st = sync._docs["d1"]
    infl = st.get("inflight")
    return (st["content"], st["version"], (list(infl["ops"]), infl["base"]) if infl else None)


BAD_OPS = [
    [{"p": -1, "i": "x"}],
    [{"p": 10**9, "i": "x"}],
    [{"p": 1.5, "i": "x"}],
    [{"p": "1", "i": "x"}],
    [{"p": None, "i": "x"}],
    [{"i": "x"}],
    [{"p": 0, "i": 5}],
    [{"p": 0, "i": None}],
    [{"p": 0, "d": 7}],
    [{"p": 0, "d": "NOT THERE"}],            # deletes text the doc does not have: our copy diverged
    [{"p": 3, "d": "zzzz"}],
    [{"p": 0, "d": "alpha\nbeta\ngamma\nextra"}],
    "not a list",
    [None],
    [[1, 2]],
]


@pytest.mark.parametrize("inflight", [False, True])
@pytest.mark.parametrize("ops", BAD_OPS, ids=[repr(o)[:40] for o in BAD_OPS])
def test_bad_ops_raise_and_leave_state_untouched(tmp_path, ops, inflight):
    sync = make(tmp_path, inflight)
    before = snapshot(sync)
    with pytest.raises(Exception):
        sync._on_ot_update([{"doc": "d1", "v": 5, "op": ops, "meta": {"source": "someone"}}])
    assert snapshot(sync) == before
    assert (tmp_path / "main.tex").read_text() == BASE


def test_valid_ops_still_apply(tmp_path):
    sync = make(tmp_path)
    sync._on_ot_update([{"doc": "d1", "v": 5, "op": [{"p": 0, "i": "Z"}, {"p": 7, "d": "beta"}], "meta": {"source": "s"}}])
    assert sync._docs["d1"]["content"] == "Zalpha\n\ngamma\n"


def test_comment_ops_without_text_are_ignored_not_rejected(tmp_path):
    sync = make(tmp_path)
    sync._on_ot_update([{"doc": "d1", "v": 5, "op": [{"c": "alpha", "p": 0, "t": "thread1"}], "meta": {"source": "s"}}])
    assert sync._docs["d1"]["content"] == BASE and sync._docs["d1"]["version"] == 6


def test_random_garbage_never_crashes_the_process_or_corrupts(tmp_path):
    rnd = random.Random(1234)
    junk = [None, 0, -3, 2**70, 1.5, "", "x" * 50, [], {}, [1], {"a": 1}, True]
    for i in range(3000):
        sync = make(tmp_path, inflight=bool(i % 2))
        before = snapshot(sync)
        payload = {rnd.choice(["doc", "v", "op", "meta", "zzz"]): rnd.choice(junk) for _ in range(rnd.randint(0, 5))}
        payload.setdefault("doc", rnd.choice(["d1", "d1", "nope", None]))
        try:
            sync._on_ot_update([payload] if rnd.random() < 0.9 else [])
        except Exception:
            assert snapshot(sync) == before, payload      # an error must not leave a half-applied update
        assert (tmp_path / "main.tex").exists()


def test_garbage_ot_error_does_not_raise(tmp_path):
    sync = make(tmp_path)
    for args in ([], [None], ["msg"], ["msg", None], ["msg", {"doc_id": "nope"}], [5, "x", {}], ["m", {"doc_id": None}]):
        sync._ws = None
        sync._on_ot_error(args)
