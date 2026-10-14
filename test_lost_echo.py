"""
The server applied our in-flight op but the echo was lost (connection dropped).
On reconnect the real _sync_all_docs must not re-apply it (that would
duplicate the text on the server), and must not lose it if the server lacks it.
"""

from __future__ import annotations

import pytest

from overleaf_sync import OverleafSync, apply_sharejs_ops, compute_ot_ops


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
