"""A failing local write (disk full, permissions) must never make the sync push 'deletions' of text it could not write."""

from __future__ import annotations

import errno
import os

import pytest

import overleaf_sync
from overleaf_sync import OverleafSync

BASE = "alpha\nbeta\ngamma\n"


def make(tmp_path, monkeypatch, server_text=None):
    monkeypatch.setattr(overleaf_sync, "collect_docs_from_project", lambda data: {"d1": "main.tex"})
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    (tmp_path / "main.tex").write_text(BASE)
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    return sync


def remote_insert(text, p=0):
    return [{"doc": "d1", "v": 5, "op": [{"p": p, "i": text}], "meta": {"source": "someone"}}]


def test_failed_write_of_a_remote_update_leaves_state_as_before(tmp_path, monkeypatch):
    sync = make(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(overleaf_sync, "update_file", boom)
    with pytest.raises(OSError):
        sync._on_ot_update(remote_insert("COAUTHOR\n"))
    st = sync._docs["d1"]
    assert st["content"] == BASE and st["version"] == 5       # nothing half-applied
    assert (tmp_path / "main.tex").read_text() == BASE
    assert sync._collect_local_changes() == []                # so no bogus push of "delete COAUTHOR"


def test_failed_write_keeps_the_in_flight_push_intact(tmp_path, monkeypatch):
    sync = make(tmp_path, monkeypatch)
    import threading
    infl = {"ops": [{"p": 5, "i": "X"}], "base": BASE, "done": threading.Event(), "v": 5}
    sync._docs["d1"]["inflight"] = infl
    snap = (list(infl["ops"]), infl["base"])
    monkeypatch.setattr(overleaf_sync, "update_file", lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")))
    with pytest.raises(PermissionError):
        sync._on_ot_update(remote_insert("COAUTHOR\n", 3))
    assert (infl["ops"], infl["base"]) == snap


# ── a deletion is only sent once its text could be recorded ─────────────────────────────────────

def test_deletion_waits_when_the_record_cannot_be_written(tmp_path, monkeypatch):
    sync = make(tmp_path, monkeypatch)
    real_open = open

    def deny(path, *a, **k):
        if "deleted.jsonl" in str(path):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", deny)
    sync._ws_ready.set()
    assert sync._push_change("d1", BASE, "alpha\ngamma\n") is False        # deletes "beta": refused
    st = sync._docs["d1"]
    assert st["content"] == BASE and "inflight" not in st                # nothing half-sent
    assert (tmp_path / "main.tex").read_text() == BASE


def test_additions_do_not_need_the_record(tmp_path, monkeypatch):
    sync = make(tmp_path, monkeypatch)
    assert sync._snapshot_before_delete("main.tex", BASE, [{"p": 0, "i": "x"}]) is True


# ── permissions ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_directory_does_not_loop_or_push(tmp_path, monkeypatch):
    sync = make(tmp_path, monkeypatch)
    (tmp_path / "main.tex").write_text(BASE + "emoji \U0001F600\n")      # needs sanitising: a rewrite is required
    tmp_path.chmod(0o555)
    try:
        assert sync._collect_local_changes() == []                       # not sent, and no exception
        assert sync._collect_local_changes() == []
        assert any(k.startswith("sanitize:") for k in sync._warned)
    finally:
        tmp_path.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_failed_write_in_a_readonly_dir_reverts_state(tmp_path, monkeypatch):
    sync = make(tmp_path, monkeypatch)
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(OSError):
            sync._on_ot_update(remote_insert("COAUTHOR\n"))
        assert sync._docs["d1"]["content"] == BASE and sync._docs["d1"]["version"] == 5
    finally:
        tmp_path.chmod(0o755)


# ── HTTP 429 ─────────────────────────────────────────────────────────────────────────────────────

def test_rate_limit_carries_retry_after():
    from overleaf_cli import RateLimited
    assert RateLimited("17").retry_after == 17
    assert RateLimited("999999").retry_after == 600          # capped
    assert RateLimited("Wed, 21 Oct 2026 07:28:00 GMT").retry_after is None
    assert RateLimited(None).retry_after is None


def test_bootstrap_429_raises_rate_limited(monkeypatch):
    import overleaf_cli

    class R:
        status_code, url, headers, text = 429, "https://x/project/y", {"Retry-After": "9"}, ""
        def raise_for_status(self): raise AssertionError("must not get here")

    s = overleaf_cli.OverleafSession.__new__(overleaf_cli.OverleafSession)
    s.session = type("S", (), {"get": lambda self, url: R()})()
    with pytest.raises(overleaf_cli.RateLimited) as e:
        s.get_project_bootstrap("a" * 24)
    assert e.value.retry_after == 9
