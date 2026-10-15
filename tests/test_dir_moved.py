"""
Tests that OverleafSync stops hard when its output directory is moved/replaced.

Real incident (2026-10-08): the sync dir was renamed to `<dir>.bak` and a
fresh git worktree created at `<dir>`; an editor whose cwd was inside the old
dir kept writing to `.bak`, and those edits silently never reached Overleaf.
"""

from __future__ import annotations

import threading
import time

import pytest

from overleaf_sync import OverleafSync


class _FakeWS:
    def is_connected(self):
        return True

    def leave_doc(self, doc_id):
        pass

    def disconnect(self):
        pass


def _sync(out):
    sync = OverleafSync("pid", "cookie", str(out))
    sync.POLL_INTERVAL = 0.05
    sync._docs["d1"] = {"content": "abc", "version": 1, "path": "main.tex"}
    (out / "main.tex").write_text("abc")
    return sync


def _swap_later(out, delay=0.2):
    def swap():
        time.sleep(delay)
        out.rename(out.with_name(out.name + ".bak"))
        out.mkdir()
        (out / "main.tex").write_text("other content")
    threading.Thread(target=swap).start()


def test_watch_stops_when_dir_replaced(tmp_path):
    out = tmp_path / "proj"
    out.mkdir()
    sync = _sync(out)
    sync._running = True
    pushed = []
    sync._push_change = lambda *a: pushed.append(a)
    _swap_later(out)
    t = threading.Thread(target=sync._watch_files, daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    assert sync._running is False
    assert "was moved, deleted or replaced" in sync._fatal
    assert pushed == []  # new dir's content must not be pushed


def test_watch_stops_when_dir_deleted(tmp_path):
    out = tmp_path / "proj"
    out.mkdir()
    sync = _sync(out)
    sync._running = True

    def delete():
        time.sleep(0.2)
        (out / "main.tex").unlink()
        out.rmdir()
    threading.Thread(target=delete).start()
    t = threading.Thread(target=sync._watch_files, daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    assert sync._fatal


def test_run_exits_1_with_message(tmp_path, monkeypatch, capsys):
    out = tmp_path / "proj"
    out.mkdir()
    sync = _sync(out)

    def fake_connect(initial):
        sync._ws = _FakeWS()
    monkeypatch.setattr(sync, "_connect_and_sync_state", fake_connect)
    _swap_later(out)
    with pytest.raises(SystemExit) as exc:
        sync.run()
    assert exc.value.code == 1
    assert "was moved, deleted or replaced" in capsys.readouterr().err


def test_watch_keeps_running_when_dir_unchanged(tmp_path):
    out = tmp_path / "proj"
    out.mkdir()
    sync = _sync(out)
    sync._running = True
    t = threading.Thread(target=sync._watch_files, daemon=True)
    t.start()
    time.sleep(0.3)
    assert t.is_alive()
    assert sync._fatal is None
    sync._running = False
    t.join(timeout=2)
