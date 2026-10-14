"""Edits made while the sync was stopped are merged at the next start; an emptied file needs confirmation."""

from __future__ import annotations

import overleaf_sync
from overleaf_sync import OverleafSync

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


def test_restoring_the_file_cancels_the_hold(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 1, "path": "main.tex"}
    clock = clocked(monkeypatch)
    sync._settled([("d1", BASE, "")])
    assert sync._settled([]) == []                                    # file back to normal: nothing to push
    (tmp_path / sync.WIPE_SENTINEL).touch()                           # a later confirmation does not apply to a new wipe...
    assert sync._settled([("d1", BASE, "")]) == []                    # ...it is consumed by this one, but the 5 s hold still applies
    clock[0] += 10
    assert sync._settled([("d1", BASE, "")]) != []


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
