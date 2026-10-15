"""Files created / renamed / deleted on disk are mirrored in Overleaf, deletions only once confirmed."""

from __future__ import annotations

import json

import pytest

import overleaf_sync
from overleaf_sync import OverleafSync

A = "alpha text\nsecond line\n"
B = "beta text\n"


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.fail = None

    def _do(self, *call):
        if self.fail:
            raise RuntimeError(self.fail)
        self.calls.append(call)
        return {"_id": "new-" + str(len(self.calls))}

    def create_doc(self, pid, name, folder): return self._do("create_doc", name, folder)
    def create_folder(self, pid, name, parent): return self._do("create_folder", name, parent)
    def rename_doc(self, pid, doc, name): return self._do("rename_doc", doc, name)
    def move_doc(self, pid, doc, folder): return self._do("move_doc", doc, folder)
    def delete_doc(self, pid, doc): return self._do("delete_doc", doc)


class FakeWS:
    def disconnect(self): pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(overleaf_sync._time, "monotonic", lambda: clock[0])
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._http = FakeHTTP()
    sync._docs["d1"] = {"content": A, "version": 1, "path": "main.tex"}
    sync._docs["d2"] = {"content": B, "version": 1, "path": "chapters/two.tex"}
    sync._folders = {"": "root", "chapters": "f-ch"}
    sync._server_paths = {"main.tex", "chapters", "chapters/two.tex", "logo.png"}
    (tmp_path / "chapters").mkdir()
    (tmp_path / "main.tex").write_text(A)
    (tmp_path / "chapters" / "two.tex").write_text(B)
    sync._ws_ready.set()

    def scan(seconds=4, steps=4):
        for _ in range(steps):
            clock[0] += seconds / steps + 1.0
            sync._tree_scan(FakeWS())
    sync.scan, sync.clock = scan, clock
    return sync


def calls(sync):
    return sync._http.calls


def test_nothing_changes_nothing_is_sent(env):
    env.scan(10)
    assert calls(env) == []


def test_a_new_text_file_is_created_after_it_has_been_stable(env, tmp_path):
    (tmp_path / "intro.tex").write_text("hello\n")
    env.clock[0] += 1.1
    env._tree_scan(FakeWS())
    assert calls(env) == []                          # first sighting: held
    env.scan(10)
    assert calls(env) == [("create_doc", "intro.tex", "root")]


def test_a_new_file_in_a_new_folder_creates_the_folders_first(env, tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x.bib").write_text("@a{}\n")
    env.scan(10)
    assert [c[0] for c in calls(env)] == ["create_folder", "create_folder", "create_doc"]
    assert calls(env)[0][1:] == ("a", "root")
    assert calls(env)[2][1] == "x.bib"


@pytest.mark.parametrize("name", ["fig.png", "main.pdf", "x.aux", "notes", "a.tex~", "x.swp", "4913", ".hidden.tex"])
def test_files_that_are_not_editable_documents_are_left_alone(env, tmp_path, name):
    (tmp_path / name).write_bytes(b"data")
    env.scan(10)
    assert calls(env) == []


def test_a_file_that_already_exists_in_overleaf_as_a_binary_is_not_created(env, tmp_path):
    env._server_paths.add("logo.tex")
    (tmp_path / "logo.tex").write_text("x")
    env.scan(10)
    assert calls(env) == []


def test_non_utf8_text_is_not_created(env, tmp_path):
    (tmp_path / "bad.tex").write_bytes(b"\xff\xfe\x00bad")
    env.scan(10)
    assert calls(env) == []


def test_a_renamed_file_is_renamed_not_recreated(env, tmp_path):
    (tmp_path / "main.tex").rename(tmp_path / "paper.tex")
    env.scan(10)
    assert calls(env) == [("rename_doc", "d1", "paper.tex")]


def test_a_moved_file_is_moved(env, tmp_path):
    (tmp_path / "main.tex").rename(tmp_path / "chapters" / "main.tex")
    env.scan(10)
    assert calls(env) == [("move_doc", "d1", "f-ch")]


def test_moved_and_renamed_does_both(env, tmp_path):
    (tmp_path / "main.tex").rename(tmp_path / "chapters" / "one.tex")
    env.scan(10)
    assert [c[0] for c in calls(env)] == ["move_doc", "rename_doc"]


def test_a_rename_onto_an_existing_server_name_is_refused(env, tmp_path):
    env._server_paths.add("paper.tex")           # e.g. a binary with that name
    (tmp_path / "main.tex").rename(tmp_path / "paper.tex")
    env.scan(10)
    assert calls(env) == []


def test_a_save_that_deletes_and_recreates_is_not_a_delete(env, tmp_path):
    f = tmp_path / "main.tex"
    f.unlink()
    env.clock[0] += 1.1
    env._tree_scan(FakeWS())
    f.write_text(A + "more\n")                    # an editor's save-by-delete-and-write
    env.scan(10)
    assert calls(env) == []


def test_a_deleted_file_is_not_deleted_in_overleaf_without_confirmation(env, tmp_path, capsys):
    (tmp_path / "main.tex").unlink()
    env.scan(30, 12)
    assert calls(env) == []
    out = capsys.readouterr().out
    assert out.count("NOT changing them in Overleaf") == 1                 # warned once, not every second
    assert f"touch {tmp_path}/.overleaf-sync-confirm-wipe" in out


def test_a_confirmed_delete_keeps_a_copy_first_and_deletes_once(env, tmp_path):
    (tmp_path / "main.tex").unlink()
    env.scan(10)
    (tmp_path / env.WIPE_SENTINEL).touch()
    env.scan(10)
    assert calls(env) == [("delete_doc", "d1")]
    assert not (tmp_path / env.WIPE_SENTINEL).exists()
    backup = next((tmp_path / ".overleaf-sync-backup").rglob("deleted-docs/main.tex"))
    assert backup.read_text() == A
    rec = [json.loads(l) for l in next((tmp_path / ".overleaf-sync-backup").rglob("deleted.jsonl")).read_text().splitlines()]
    assert rec[0]["path"] == "main.tex" and rec[0]["deleted_document"] is True


def test_a_confirmation_given_before_the_warning_does_not_count(env, tmp_path):
    import os, time
    s = tmp_path / env.WIPE_SENTINEL
    s.touch()
    os.utime(s, (time.time() - 100, time.time() - 100))
    (tmp_path / "main.tex").unlink()
    env.scan(10)
    assert calls(env) == [] and s.exists()


def test_no_copy_no_delete(env, tmp_path, monkeypatch):
    (tmp_path / "main.tex").unlink()
    env.scan(10)
    monkeypatch.setattr(env, "_record_doc_deletion", lambda *a: False)
    (tmp_path / env.WIPE_SENTINEL).touch()
    env.scan(10)
    assert calls(env) == []


def test_allow_wipe_skips_the_question_for_deletes(env, tmp_path):
    env.allow_wipe = True
    (tmp_path / "main.tex").unlink()
    env.scan(10)
    assert calls(env) == [("delete_doc", "d1")]


def test_a_big_batch_needs_confirmation(env, tmp_path):
    for i in range(25):
        (tmp_path / f"c{i}.tex").write_text(f"{i}\n")
    env.scan(10)
    assert calls(env) == []
    (tmp_path / env.WIPE_SENTINEL).touch()
    env.scan(10)
    assert len([c for c in calls(env) if c[0] == "create_doc"]) == 25


def test_two_docs_with_the_same_text_are_not_guessed_as_a_rename(env, tmp_path):
    env._docs["d3"] = {"content": A, "version": 1, "path": "copy.tex"}
    env._server_paths.add("copy.tex")
    (tmp_path / "copy.tex").write_text(A)
    (tmp_path / "main.tex").rename(tmp_path / "x.tex")
    (tmp_path / "copy.tex").rename(tmp_path / "y.tex")
    env.scan(10)
    assert [c[0] for c in calls(env)] == ["create_doc", "create_doc"]    # and nothing is deleted unconfirmed


def test_a_failure_is_reported_and_retried_later_not_every_poll(env, tmp_path, capsys):
    env._http.fail = "HTTP 500"
    (tmp_path / "intro.tex").write_text("x")
    env.scan(10)
    env.scan(10)
    assert capsys.readouterr().out.count("could not create") == 1
    env._http.fail = None
    env.clock[0] += 70
    env.scan(10)
    assert calls(env) == [("create_doc", "intro.tex", "root")]


def test_dry_run_sends_nothing(env, tmp_path):
    env.dry_run = True
    (tmp_path / "intro.tex").write_text("x")
    (tmp_path / "main.tex").unlink()
    env.scan(10)
    assert calls(env) == []


def test_a_push_in_flight_postpones_the_scan(env, tmp_path):
    env._docs["d1"]["inflight"] = {"base": A, "ops": []}
    (tmp_path / "intro.tex").write_text("x")
    env.scan(10)
    assert calls(env) == []


def test_collect_tree_lists_folders_files_and_docs():
    data = {"rootFolder": [{"_id": "r", "name": "rootFolder", "docs": [{"_id": "a", "name": "main.tex"}],
                            "fileRefs": [{"_id": "f", "name": "logo.png"}],
                            "folders": [{"_id": "c", "name": "ch", "docs": [{"_id": "b", "name": "x.tex"}], "fileRefs": [], "folders": []}]}]}
    folders, paths = overleaf_sync.collect_tree(data)
    assert folders == {"": "r", "ch": "c"}
    assert paths == {"main.tex", "logo.png", "ch", "ch/x.tex"}


def test_tree_sync_can_be_turned_off(tmp_path):
    sync = OverleafSync("pid", "cookie", str(tmp_path), propagate_tree=False)
    assert sync.propagate_tree is False


def test_a_reconnect_does_not_bring_back_a_file_that_was_moved_or_deleted(tmp_path, monkeypatch):
    class WS:
        def join_project(self): return {"_public_id": "p"}
        def join_doc(self, doc_id): return A.split("\n"), 1
    monkeypatch.setattr(overleaf_sync, "collect_docs_from_project", lambda d: {"d1": "main.tex"})
    for tree_sync in (True, False):
        d = tmp_path / str(tree_sync); d.mkdir()
        sync = OverleafSync("pid", "cookie", str(d), propagate_tree=tree_sync)
        sync._docs["d1"] = {"content": A, "version": 1, "path": "main.tex"}
        sync._sync_all_docs(WS(), initial=False)             # main.tex is missing on disk
        assert (d / "main.tex").exists() is (not tree_sync)  # tree sync: the scan decides; off: restored as before


def test_move_survives_a_reconnect_in_between(env, tmp_path, monkeypatch):
    class WS:
        def join_project(self): return {"_public_id": "p", "rootFolder": [{"_id": "root", "name": "rootFolder", "docs": [{"_id": "d1", "name": "main.tex"}], "fileRefs": [], "folders": []}]}
        def join_doc(self, doc_id): return A.split("\n"), 1
    env._docs.pop("d2")
    (tmp_path / "chapters" / "two.tex").unlink()
    (tmp_path / "main.tex").rename(tmp_path / "chapters" / "main.tex")
    env._sync_all_docs(WS(), initial=False)                  # the reconnect happens first
    assert not (tmp_path / "main.tex").exists()
    env.scan(10)
    assert calls(env) == [("create_folder", "chapters", "root"), ("move_doc", "d1", "new-1")]


def test_a_rename_in_overleaf_plus_a_local_move_is_not_resurrected(tmp_path, monkeypatch):
    class WS:
        def join_project(self): return {"_public_id": "p"}
        def join_doc(self, doc_id): return A.split("\n"), 1
    monkeypatch.setattr(overleaf_sync, "collect_docs_from_project", lambda d: {"d1": "new-name.tex"})
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": A, "version": 1, "path": "old-name.tex"}
    sync._sync_all_docs(WS(), initial=False)
    assert not (tmp_path / "new-name.tex").exists() and not (tmp_path / "old-name.tex").exists()


def test_one_touch_confirms_a_wipe_and_a_delete_waiting_together(env, tmp_path):
    (tmp_path / "chapters" / "two.tex").unlink()                       # a delete waiting for confirmation
    env.scan(10)
    assert calls(env) == []
    wipe = [("d1", A, "")]                                             # and an emptied file waiting too
    env.clock[0] += 1
    assert env._settled(wipe) == []
    env.scan(2, 2)
    (tmp_path / env.WIPE_SENTINEL).touch()
    env.scan(10)                                                       # the tree scan takes the sentinel first…
    assert calls(env) == [("delete_doc", "d2")]
    released = []
    for _ in range(12):                                                # …and the wipe still gets its confirmation
        env.clock[0] += 1
        released += env._settled(wipe)
        if released:
            break
    assert released == wipe


BIG = "".join(f"line {i} of a longer chapter\n" for i in range(40))


def test_moved_and_edited_is_a_rename_with_a_copy_kept(env, tmp_path):
    env._docs["d1"]["content"] = BIG
    (tmp_path / "main.tex").unlink()
    (tmp_path / "paper.tex").write_text(BIG.replace("line 7 of", "LINE SEVEN of") + "one more line\n")
    env.scan(10)
    assert calls(env) == [("rename_doc", "d1", "paper.tex")]
    assert next((tmp_path / ".overleaf-sync-backup").rglob("deleted-docs/main.tex")).read_text() == BIG


def test_unrelated_new_file_is_not_taken_for_an_edited_move(env, tmp_path):
    env._docs["d1"]["content"] = BIG
    (tmp_path / "main.tex").unlink()
    (tmp_path / "other.tex").write_text("something\nentirely\ndifferent\n")
    env.scan(10)
    assert [c[0] for c in calls(env)] == ["create_doc"]          # the delete still waits for confirmation


def test_two_equally_similar_candidates_are_not_guessed(env, tmp_path):
    env._docs["d1"]["content"] = BIG
    (tmp_path / "main.tex").unlink()
    (tmp_path / "x.tex").write_text(BIG + "x\n")
    (tmp_path / "y.tex").write_text(BIG + "y\n")
    env.scan(10)
    assert sorted(c[0] for c in calls(env)) == ["create_doc", "create_doc"]


def test_huge_texts_are_not_compared(env):
    assert OverleafSync._similarity("a\n" * 200_000, "a\n" * 200_000 + "b") == 0.0
    assert OverleafSync._similarity("a\nb\nc\nd\n", "a\nb\nc\nd\ne\n") > 0.85
