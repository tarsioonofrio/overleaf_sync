"""
The server applied our in-flight op but the echo was lost (connection dropped).
On reconnect the real _sync_all_docs must not re-apply it (that would
duplicate the text on the server), and must not lose it if the server lacks it.
"""

from __future__ import annotations

import pytest

from overleaf_sync.sync import OverleafSync, apply_sharejs_ops, compute_ot_ops


class FakeWS:
    def __init__(self, server_text, version):
        self.server_text, self.version = server_text, version
    def join_project(self):
        return {"rootFolder": [{"_id": "r", "docs": [{"_id": "d1", "name": "main.tex"}], "folders": []}],
                "_public_id": "me"}
    def join_doc(self, doc_id):
        return self.server_text.split("\n"), self.version


BASE = "\n".join(f"line {i} of the document" for i in range(30))


def _setup(tmp_path, local, inflight_new, v_sent=5):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    (tmp_path / "main.tex").write_text(local)
    ops = compute_ot_ops(BASE, inflight_new)
    sync._docs["d1"] = {"content": inflight_new, "version": v_sent, "path": "main.tex",
                        "inflight": {"ops": ops, "base": BASE, "done": __import__("threading").Event(),
                                     "v": v_sent}}
    return sync


def _reconnect(sync, server_text, version):
    sync._sync_all_docs(FakeWS(server_text, version), initial=False)


MINE = "line 3 of the document"
MINE_NEW = "line 3 of the document, EDITED BY ME with a long insertion"


def test_op_applied_on_server_plus_coauthor_edit_is_not_duplicated(tmp_path):
    mine = BASE.replace(MINE, MINE_NEW)
    sync = _setup(tmp_path, mine, mine)
    server = mine.replace("line 20 of the document", "line 20 changed by coauthor")
    _reconnect(sync, server, 7)
    assert (tmp_path / "main.tex").read_text() == server
    assert server.count("EDITED BY ME") == 1


def test_op_not_applied_on_server_is_repushed(tmp_path):
    mine = BASE.replace(MINE, MINE_NEW)
    sync = _setup(tmp_path, mine, mine)
    server = BASE.replace("line 20 of the document", "line 20 changed by coauthor")
    _reconnect(sync, server, 6)
    merged = (tmp_path / "main.tex").read_text()
    assert merged.count("EDITED BY ME") == 1 and "changed by coauthor" in merged
    assert sync._docs["d1"]["content"] == server       # poll loop will push the diff


def test_op_not_applied_server_unchanged_is_repushed(tmp_path):
    mine = BASE.replace(MINE, MINE_NEW)
    sync = _setup(tmp_path, mine, mine)
    _reconnect(sync, BASE, 5)
    assert (tmp_path / "main.tex").read_text() == mine


def test_op_applied_and_local_edits_after_it(tmp_path):
    mine = BASE.replace(MINE, MINE_NEW)
    local = mine + "\nunsaved extra line typed later"
    sync = _setup(tmp_path, local, mine)
    server = mine.replace("line 20 of the document", "line 20 changed by coauthor")
    _reconnect(sync, server, 7)
    merged = (tmp_path / "main.tex").read_text()
    assert merged.count("EDITED BY ME") == 1
    assert "unsaved extra line" in merged and "changed by coauthor" in merged


def test_op_applied_deletion_is_not_reapplied(tmp_path):
    mine = BASE.replace("line 8 of the document\n", "")
    sync = _setup(tmp_path, mine, mine)
    server = mine.replace("line 20 of the document", "line 20 changed by coauthor")
    _reconnect(sync, server, 7)
    assert (tmp_path / "main.tex").read_text() == server


# --- the server rejects the op: {v, doc} first, then otUpdateError -----------

def test_rejected_op_keeps_local_file_and_is_not_resent(tmp_path):
    mine = BASE.replace(MINE, MINE_NEW)
    sync = _setup(tmp_path, mine, mine, v_sent=5)
    sync._ws = type("W", (), {"disconnect": lambda s: None})()
    # 1. the look-alike ack: stripped {v, doc}
    sync._on_ot_update([{"v": 5, "doc": "d1"}])
    assert "inflight" not in sync._docs["d1"]
    # 2. the real answer
    sync._on_ot_error(["Update takes doc over max doc size", {"doc_id": "d1"}])
    assert list((tmp_path / ".overleaf-sync-backup").rglob("main.tex"))[0].read_text() == mine
    # 3. reconnect: server still has the old doc
    _reconnect(sync, BASE, 6)
    assert (tmp_path / "main.tex").read_text() == mine            # not overwritten as "stale"
    assert sync._docs["d1"]["content"] == BASE
    # 4. the poll loop must not send the refused content again
    assert sync._collect_local_changes() == []
    (tmp_path / "main.tex").write_text(mine + "\nmore")            # a different content is retried
    assert len(sync._collect_local_changes()) == 1


def test_error_before_the_look_alike_ack_is_handled_too(tmp_path):
    mine = BASE.replace(MINE, MINE_NEW)
    sync = _setup(tmp_path, mine, mine, v_sent=5)
    sync._ws = type("W", (), {"disconnect": lambda s: None})()
    sync._on_ot_error(["boom", {"doc_id": "d1"}])
    _reconnect(sync, BASE, 5)
    assert (tmp_path / "main.tex").read_text() == mine


def test_over_limit_content_is_never_sent(tmp_path):
    import overleaf_sync.sync as o
    sync = _setup(tmp_path, "x", "x")
    sync._docs["d1"].pop("inflight")
    sync._docs["d1"]["content"] = "x"
    (tmp_path / "main.tex").write_text("y" * (o.MAX_DOC_LENGTH + 1))
    assert sync._collect_local_changes() == []
    (tmp_path / "main.tex").write_text("y" * 100)
    assert len(sync._collect_local_changes()) == 1


def test_rename_in_overleaf_carries_pending_local_edit(tmp_path):
    import threading
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    (tmp_path / "main.tex").write_text(BASE + "\nPENDING LOCAL EDIT")
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}

    class WS(FakeWS):
        def join_project(self):
            return {"rootFolder": [{"_id": "r", "docs": [{"_id": "d1", "name": "paper.tex"}], "folders": []}],
                    "_public_id": "me"}
    sync._sync_all_docs(WS(BASE, 6), initial=False)
    assert not (tmp_path / "main.tex").exists()
    assert (tmp_path / "paper.tex").read_text() == BASE + "\nPENDING LOCAL EDIT"
    assert sync._docs["d1"]["path"] == "paper.tex" and sync._docs["d1"]["content"] == BASE
    assert len(sync._collect_local_changes()) == 1               # the edit will be pushed
