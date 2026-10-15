"""Edits made while the sync was stopped are merged at the next start; an emptied file needs confirmation."""

from __future__ import annotations

import overleaf_sync.sync as overleaf_sync
from overleaf_sync.sync import OverleafSync

BASE = "line one\nline two\nline three\nline four\n"


class FakeWS:
    def __init__(self, text, v=5):
        self.text, self.v = text, v

    def join_project(self):
        return {"_public_id": "pub"}

    def join_doc(self, doc_id):
        return self.text.split("\n"), self.v


def start(tmp_path, monkeypatch, server_text, project="pid"):
    monkeypatch.setattr(overleaf_sync, "collect_docs_from_project", lambda data: {"d1": "main.tex"})
    sync = OverleafSync(project, "cookie", str(tmp_path))
    sync._sync_all_docs(FakeWS(server_text), initial=True)
    return sync


def first_run_then_stop(tmp_path, monkeypatch):
    sync = start(tmp_path, monkeypatch, BASE)
    assert sync._collect_local_changes() == []        # in sync: the poll loop records the base
    assert (tmp_path / OverleafSync.BASE_DIR / "d1").read_text() == BASE
    return sync


def test_local_edit_made_while_stopped_is_kept_and_pushed(tmp_path, monkeypatch):
    first_run_then_stop(tmp_path, monkeypatch)
    (tmp_path / "main.tex").write_text(BASE + "typed offline\n")
    sync = start(tmp_path, monkeypatch, BASE)
    assert (tmp_path / "main.tex").read_text() == BASE + "typed offline\n"
    assert sync._collect_local_changes() == [("d1", BASE, BASE + "typed offline\n")]


def test_edits_on_both_sides_are_merged(tmp_path, monkeypatch):
    first_run_then_stop(tmp_path, monkeypatch)
    local = BASE.replace("line one", "LOCAL one")
    server = BASE.replace("line four", "SERVER four")
    (tmp_path / "main.tex").write_text(local)
    sync = start(tmp_path, monkeypatch, server)
    merged = (tmp_path / "main.tex").read_text()
    assert "LOCAL one" in merged and "SERVER four" in merged and merged.count("line") == 2
    assert sync._docs["d1"]["content"] == server                      # the poll loop pushes server → merged
    backups = list((tmp_path / ".overleaf-sync-backup").rglob("main.tex"))
    assert [b.read_text() for b in backups] == [local]                # the pre-merge file is kept too


def test_untouched_local_file_just_follows_the_server(tmp_path, monkeypatch):
    first_run_then_stop(tmp_path, monkeypatch)
    server = BASE + "coauthor added this\n"
    start(tmp_path, monkeypatch, server)
    assert (tmp_path / "main.tex").read_text() == server
    assert not (tmp_path / ".overleaf-sync-backup").exists()


def test_without_a_base_the_server_wins_and_the_local_file_is_backed_up(tmp_path, monkeypatch):
    (tmp_path / "main.tex").write_text("something written before any sync\n")
    start(tmp_path, monkeypatch, BASE)
    assert (tmp_path / "main.tex").read_text() == BASE
    backups = list((tmp_path / ".overleaf-sync-backup").rglob("main.tex"))
    assert [b.read_text() for b in backups] == ["something written before any sync\n"]


def test_a_base_from_another_project_is_ignored(tmp_path, monkeypatch):
    first_run_then_stop(tmp_path, monkeypatch)
    (tmp_path / "main.tex").write_text(BASE + "local\n")
    start(tmp_path, monkeypatch, BASE + "server\n", project="other")
    assert (tmp_path / "main.tex").read_text() == BASE + "server\n"
    assert list((tmp_path / ".overleaf-sync-backup").rglob("main.tex"))


# ── an emptied file is never pushed on its own ───────────────────────────────────────────────

def clocked(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(overleaf_sync._time, "monotonic", lambda: clock[0])
    return clock


def test_an_emptied_file_is_held_until_confirmed(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    clock = clocked(monkeypatch)
    for _ in range(200):                                              # far longer than the 5 s hold
        clock[0] += 1.0
        assert sync._settled([("d1", BASE, "")]) == []
    (tmp_path / sync.WIPE_SENTINEL).touch()
    released = []
    for _ in range(20):
        clock[0] += 1.0
        released += sync._settled([("d1", BASE, "")])
        if released:
            break
    assert released == [("d1", BASE, "")]
    assert not (tmp_path / sync.WIPE_SENTINEL).exists()               # one confirmation, used once


def test_whitespace_only_counts_as_emptied(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    clock = clocked(monkeypatch)
    for _ in range(30):
        clock[0] += 1.0
        assert sync._settled([("d1", BASE, "\n  \n")]) == []


def test_restoring_the_file_cancels_the_hold_and_a_later_wipe_warns_again(tmp_path, monkeypatch, capsys):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    (tmp_path / "main.tex").write_text(BASE)
    clock = clocked(monkeypatch)
    sync._settled([("d1", BASE, "")])
    assert "NOT sending" in capsys.readouterr().out
    assert sync._collect_local_changes() == []           # file restored to the server text
    sync._settled([])
    sync._settled([("d1", BASE, "")])
    assert "NOT sending" in capsys.readouterr().out      # warned again, not silently held


def test_a_confirmation_made_before_the_warning_does_not_count(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    sentinel = tmp_path / sync.WIPE_SENTINEL
    sentinel.touch()
    import os, time
    os.utime(sentinel, (time.time() - 100, time.time() - 100))   # an old, stray sentinel
    clock = clocked(monkeypatch)
    for _ in range(30):
        clock[0] += 1.0
        assert sync._settled([("d1", BASE, "")]) == []
    assert sentinel.exists()                                      # left alone, not consumed
    sentinel.touch()                                              # a fresh one, after the warning
    out = []
    for _ in range(20):
        clock[0] += 1.0
        out += sync._settled([("d1", BASE, "")])
        if out:
            break
    assert out == [("d1", BASE, "")]


def test_the_confirm_command_quotes_the_path(tmp_path, monkeypatch, capsys):
    d = tmp_path / "my project"
    d.mkdir()
    sync = OverleafSync("pid", "cookie", str(d))
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    sync._settled([("d1", BASE, "")])
    assert f"touch '{d}/.overleaf-sync-confirm-wipe'" in capsys.readouterr().out


def test_a_coauthor_edit_cancels_a_held_wipe_instead_of_leaving_a_fragment(tmp_path):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    (tmp_path / "main.tex").write_text("")                          # emptied, waiting for confirmation
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    sync._on_ot_update([{"doc": "d1", "v": 5, "op": [{"p": 0, "i": "X"}], "meta": {"source": "c"}}])
    assert (tmp_path / "main.tex").read_text() == "X" + BASE          # the whole doc is back, not just "X"
    assert sync._collect_local_changes() == []


def test_a_new_doc_on_reconnect_keeps_an_existing_local_file(tmp_path, monkeypatch):
    monkeypatch.setattr(overleaf_sync, "collect_docs_from_project", lambda data: {"d1": "main.tex", "d2": "notes.tex"})
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    (tmp_path / "main.tex").write_text(BASE)
    (tmp_path / "notes.tex").write_text("my notes, written before the doc existed in Overleaf\n")

    class WS(FakeWS):
        def join_doc(self, doc_id):
            return (BASE if doc_id == "d1" else "").split("\n"), 5
    sync._sync_all_docs(WS(""), initial=False)
    assert "my notes" in (tmp_path / "notes.tex").read_text()         # merged, not overwritten by the empty doc
    assert sync._collect_local_changes()[0][0] == "d2"


def test_reserved_names_cannot_be_doc_paths():
    from overleaf_sync.sync import is_safe_relpath
    for bad in [".overleaf-sync-confirm-wipe", ".overleaf-sync-base/.project", "a/.overleaf-sync-base/x",
                ".overleaf-sync.lock", ".overleaf-sync-backup/x", "x.sync-tmp"]:
        assert not is_safe_relpath(bad), bad
    assert is_safe_relpath("chapters/intro.tex") and is_safe_relpath(".hidden.tex")


def test_failed_base_save_removes_the_old_base(tmp_path, monkeypatch):
    sync = first_run_then_stop(tmp_path, monkeypatch)
    assert sync._load_base("d1") == BASE
    monkeypatch.setattr(overleaf_sync, "atomic_write", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    sync._save_base("d1", BASE + "newer\n")
    assert sync._load_base("d1") is None                              # no stale base left to mislead the next start


def test_recording_a_deletion_happens_once_and_only_for_a_push_that_can_go(tmp_path):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    new = BASE.replace("line two\n", "")
    assert sync._push_change("d1", BASE, new) is False                # socket not ready: nothing recorded
    assert not (tmp_path / ".overleaf-sync-backup").exists()
    sync._docs["d1"]["content"] = BASE + "remote\n"                  # baseline moved: nothing recorded
    sync._ws_ready.set()
    assert sync._push_change("d1", BASE, new) is False
    assert not (tmp_path / ".overleaf-sync-backup").exists()


def test_wire_ops_with_astral_chars_keep_positions_straight():
    from overleaf_sync.sync import wire_to_cp
    doc = "a\U0001F600b\U0001F600c"                                    # each emoji is 2 UTF-16 units
    ops = [{"p": 3, "i": "X"}, {"p": 6, "d": "\U0001F600"}]           # after X: 'a😀bXc'... second op deletes the 2nd emoji
    cp = wire_to_cp(doc, [{"p": 3, "i": "X"}])
    assert cp == [{"p": 2, "i": "X"}]
    import pytest
    with pytest.raises(ValueError):
        wire_to_cp(doc, [{"p": 8, "i": "X"}])                         # 7 units long: 8 is out of range
    assert wire_to_cp(doc, [{"p": 7, "i": "X"}])[0]["p"] == 5          # end of doc


def test_allow_wipe_skips_the_confirmation_but_not_the_hold(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path), allow_wipe=True)
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    clock = clocked(monkeypatch)
    out = []
    for _ in range(12):
        clock[0] += 1.0
        out += sync._settled([("d1", BASE, "")])
        if out:
            break
    assert out == [("d1", BASE, "")]


def test_emptying_an_already_empty_doc_is_not_a_wipe(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": "", "version": 1, "path": "main.tex"}
    clock = clocked(monkeypatch)
    clock[0] += 1
    sync._settled([("d1", "", "x")])
    clock[0] += 1
    assert sync._settled([("d1", "", "x")]) == [("d1", "", "x")]
