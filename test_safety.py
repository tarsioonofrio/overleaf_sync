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


def test_atomic_write_replaces_without_leftovers(tmp_path):
    p = tmp_path / "a.tex"
    p.write_text("old")
    overleaf_sync.atomic_write(p, "new")
    assert p.read_text() == "new"
    assert [f.name for f in tmp_path.iterdir()] == ["a.tex"]


def test_untracked_local_file_is_warned_not_uploaded(tmp_path, capsys):
    sync = _sync(tmp_path)
    (tmp_path / "main.tex").write_text("x")
    (tmp_path / "new.tex").write_text("y")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("z")
    sync._warn_untracked()
    out = capsys.readouterr().out
    assert "new.tex is not an Overleaf document" in out
    assert "main.tex" not in out and ".git" not in out


def test_dry_run_never_pushes(tmp_path):
    import threading
    sync = _sync(tmp_path, "hello")
    sync.dry_run = True
    sync.POLL_INTERVAL = 0.02
    sync._running = True
    sync._ws_ready.set()
    sync._ws_ready_at = -100
    (tmp_path / "main.tex").write_text("hello world")
    sync._push_change = lambda *a: pytest.fail("pushed in dry-run")
    t = threading.Thread(target=sync._watch_files, daemon=True)
    t.start()
    import time; time.sleep(0.3)
    sync._running = False
    t.join(timeout=2)
    assert sync._docs["d1"]["content"] == "hello"



# --- hostile document paths (any collaborator can name a doc) ---------------

from pathlib import Path


@pytest.mark.parametrize("rel", [
    "../evil.tex", "a/../../evil.tex", "/etc/passwd", "a//b.tex", "./a.tex", "a/./b.tex",
    "", "a\x00b.tex", "a\\..\\b.tex", ".overleaf-sync-backup/x.tex", "x.tex.sync-tmp",
    "a/" + "b" * 256, "c" * 5000,
])
def test_unsafe_paths_rejected(rel):
    assert not overleaf_sync.is_safe_relpath(rel)


@pytest.mark.parametrize("rel", ["main.tex", "sub/dir/ref.bib", "ção/é.tex", "a b.tex", "..a.tex", "a..b/x.tex"])
def test_normal_paths_accepted(rel):
    assert overleaf_sync.is_safe_relpath(rel)


def test_safe_join_blocks_symlink_escape(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (out / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        overleaf_sync.safe_join(out, "link/x.tex")
    (out / "flink.tex").symlink_to(outside / "secret.tex")
    with pytest.raises(ValueError):
        overleaf_sync.safe_join(out, "flink.tex")
    assert overleaf_sync.safe_join(out, "ok/x.tex") == out / "ok/x.tex"


def test_filter_drops_unsafe_and_case_collisions(capsys):
    docs = {"1": "main.tex", "2": "../x.tex", "3": "Main.tex", "4": "sub/a.tex"}
    assert overleaf_sync.filter_safe_docs(docs) == {"3": "Main.tex", "4": "sub/a.tex"}


def test_symlinked_file_is_not_read_or_pushed(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    secret = tmp_path / "secret.txt"; secret.write_text("TOP SECRET")
    (out / "main.tex").symlink_to(secret)
    sync = OverleafSync("pid", "cookie", str(out))
    sync._docs["d1"] = {"content": "abc", "version": 1, "path": "main.tex"}
    assert sync._collect_local_changes() == []
    assert secret.read_text() == "TOP SECRET"


# --- update_file: never overwrite an editor save that races the merge -------

def test_update_file_preserves_write_that_lands_during_merge(tmp_path):
    p = tmp_path / "main.tex"
    p.write_text("base")
    calls = []

    def fn(local):
        calls.append(local)
        if len(calls) == 1:                      # editor saves while we compute
            tmp = tmp_path / ".swp"
            tmp.write_text("base + EDITOR TOKEN")
            tmp.replace(p)
        return local + " + REMOTE"

    new, seen = overleaf_sync.update_file(p, fn)
    assert calls == ["base", "base + EDITOR TOKEN"]
    assert p.read_text() == "base + EDITOR TOKEN + REMOTE" == new
    assert seen == "base + EDITOR TOKEN"
    assert [f.name for f in tmp_path.iterdir()] == ["main.tex"]


def test_update_file_creates_missing_file_from_default(tmp_path):
    p = tmp_path / "new.tex"
    overleaf_sync.update_file(p, lambda local: local + "!", default="abc")
    assert p.read_text() == "abc!"


def test_update_file_missing_without_default_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        overleaf_sync.update_file(tmp_path / "nope.tex", lambda t: t)


def test_update_file_noop_does_not_touch_file(tmp_path):
    p = tmp_path / "a.tex"; p.write_text("same")
    ino = p.stat().st_ino
    overleaf_sync.update_file(p, lambda t: t)
    assert p.stat().st_ino == ino


def test_update_file_keeps_permissions(tmp_path):
    p = tmp_path / "a.tex"; p.write_text("x"); p.chmod(0o640)
    overleaf_sync.update_file(p, lambda t: t + "y")
    assert p.read_text() == "xy" and (p.stat().st_mode & 0o777) == 0o640


# --- instance lock, cookie file, auth failures -------------------------------

def test_second_instance_on_same_dir_is_refused(tmp_path):
    first = OverleafSync("pid", "cookie", str(tmp_path))
    first._acquire_dir_lock()
    second = OverleafSync("pid", "cookie", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        second._acquire_dir_lock()
    assert e.value.code == 1


def test_lock_is_released_when_first_instance_goes_away(tmp_path):
    first = OverleafSync("pid", "cookie", str(tmp_path))
    first._acquire_dir_lock()
    first._lock_file.close()
    OverleafSync("pid", "cookie", str(tmp_path))._acquire_dir_lock()


def test_cookie_file_is_read_and_loose_permissions_warned(tmp_path, capsys):
    f = tmp_path / "c.txt"; f.write_text("s:abc\n"); f.chmod(0o644)
    assert overleaf_sync.read_cookie_file(str(f)) == "s:abc"
    assert "readable by other users" in capsys.readouterr().err
    f.chmod(0o600)
    overleaf_sync.read_cookie_file(str(f))
    assert capsys.readouterr().err == ""


def test_empty_cookie_file_is_an_error(tmp_path):
    f = tmp_path / "c.txt"; f.write_text("  \n"); f.chmod(0o600)
    with pytest.raises(SystemExit):
        overleaf_sync.read_cookie_file(str(f))


def test_login_redirect_is_reported_as_auth_error(monkeypatch):
    from overleaf_cli import AuthError, OverleafSession
    class Resp:
        status_code = 200; url = "http://x/login?redir=/project/1"; text = ""
        def raise_for_status(self): pass
    s = OverleafSession("bad")
    monkeypatch.setattr(s.session, "get", lambda *a, **k: Resp())
    with pytest.raises(AuthError):
        s.get_project_bootstrap("1")


def test_cookie_domain_for_dotless_hosts():
    from overleaf_cli import _cookie_domain
    assert _cookie_domain("localhost") == "localhost.local"
    assert _cookie_domain("overleaf") == "overleaf.local"
    assert _cookie_domain("www.overleaf.com") == "www.overleaf.com"
    assert _cookie_domain("127.0.0.1") == "127.0.0.1"


def test_session_cookie_is_sent_to_localhost():
    import overleaf_cli
    overleaf_cli.configure("http://localhost:8080")
    try:
        s = overleaf_cli.OverleafSession("s%3Aabc")
        req = s.session.prepare_request(__import__("requests").Request("GET", "http://localhost:8080/project/1"))
        assert "overleaf.sid=s%3Aabc" in req.headers.get("Cookie", "")
    finally:
        overleaf_cli.configure()


def test_firefox_cookie_in_the_wal_file_is_read(tmp_path, monkeypatch):
    """Recent cookies live in cookies.sqlite-wal until Firefox checkpoints; they must not be missed."""
    import sqlite3
    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, lastAccessed INTEGER)")
    con.execute("INSERT INTO moz_cookies VALUES ('overleaf_session2', 'OLD', '.overleaf.com', 1)")
    con.commit(); con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("UPDATE moz_cookies SET value='FRESH', lastAccessed=2")
    con.commit()                                   # now only in the -wal file
    assert (tmp_path / "cookies.sqlite-wal").stat().st_size > 0
    monkeypatch.delenv("OVERLEAF_COOKIE", raising=False)
    got = overleaf_sync.load_overleaf_cookie(cookie_db=str(db), url="https://www.overleaf.com", cookie_name="overleaf_session2")
    con.close()
    assert got == "FRESH"
