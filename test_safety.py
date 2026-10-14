"""Safeguards against destroying the Overleaf document."""

from __future__ import annotations

import sys
import types

import pytest

try:
    import websocket  # noqa: F401
except ImportError:  # only needed to import overleaf_cli, not exercised here
    sys.modules["websocket"] = types.ModuleType("websocket")

import overleaf_sync
from overleaf_cli import SocketIOClient
from overleaf_sync import OverleafSync


def _sync(tmp_path, content="x" * 500):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": content, "version": 5, "path": "main.tex"}
    return sync


def test_change_needs_two_consecutive_identical_polls(tmp_path):
    sync = _sync(tmp_path, "hello")
    assert sync._settled([("d1", "hello", "hello world")]) == []
    assert sync._settled([("d1", "hello", "hello world")]) == [("d1", "hello", "hello world")]


def test_changing_content_resets_stability(tmp_path):
    sync = _sync(tmp_path, "hello")
    sync._settled([("d1", "hello", "hello a")])
    assert sync._settled([("d1", "hello", "hello ab")]) == []


def test_empty_file_is_held_then_pushed(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    now = [100.0]
    monkeypatch.setattr(overleaf_sync._time, "monotonic", lambda: now[0])
    change = [("d1", sync._docs["d1"]["content"], "")]
    assert sync._settled(change) == []
    now[0] += 1.0
    assert sync._settled(change) == []          # truncated file: still held
    now[0] += sync.BIG_DELETE_SECS
    assert sync._settled(change) == change      # deliberate wipe: pushed


def test_truncate_then_rewrite_never_pushes_empty(tmp_path, monkeypatch):
    sync = _sync(tmp_path)
    old = sync._docs["d1"]["content"]
    now = [0.0]
    monkeypatch.setattr(overleaf_sync._time, "monotonic", lambda: now[0])
    assert sync._settled([("d1", old, "")]) == []
    now[0] += 0.5
    assert sync._settled([("d1", old, old + "y")]) == []   # rewritten, new candidate
    now[0] += 0.5
    assert sync._settled([("d1", old, old + "y")]) == [("d1", old, old + "y")]


def test_big_partial_deletion_is_held(tmp_path):
    old = "a" * 1000
    assert OverleafSync._is_big_delete(old, "a" * 100)
    assert not OverleafSync._is_big_delete(old, "a" * 900)
    assert not OverleafSync._is_big_delete("short", "s")


def test_join_doc_timeout_is_an_error_not_an_empty_doc(monkeypatch):
    c = SocketIOClient.__new__(SocketIOClient)
    c.send_event = lambda *a, **k: 1
    class Never:
        def wait(self, timeout=None): return False
        def set(self): pass
    monkeypatch.setattr("overleaf_cli.threading.Event", Never)
    with pytest.raises(RuntimeError):
        c.join_doc("d1")


def test_join_doc_error_ack_raises():
    c = SocketIOClient.__new__(SocketIOClient)
    c.send_event = lambda name, args, callback=None: callback([{"message": "nope"}]) or 1
    with pytest.raises(RuntimeError):
        c.join_doc("d1")


def test_join_doc_empty_doc_is_fine():
    c = SocketIOClient.__new__(SocketIOClient)
    c.send_event = lambda name, args, callback=None: callback([None, [], 3, []]) or 1
    assert c.join_doc("d1") == ([], 3)


def test_version_gap_raises_instead_of_corrupting_state(tmp_path):
    sync = _sync(tmp_path, "abc")
    (tmp_path / "main.tex").write_text("abc")
    with pytest.raises(RuntimeError, match="gap"):
        sync._on_ot_update([{"doc": "d1", "v": 9, "op": [{"p": 0, "i": "x"}]}])
    assert sync._docs["d1"]["content"] == "abc"


def test_backup_of_local_file_overwritten_by_initial_sync(tmp_path):
    sync = _sync(tmp_path)
    sync._backup_local("sub/main.tex", "my offline edits")
    saved = list((tmp_path / ".overleaf-sync-backup").rglob("main.tex"))
    assert [p.read_text() for p in saved] == ["my offline edits"]
