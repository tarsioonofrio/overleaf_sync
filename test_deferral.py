"""Pushes must not starve behind a writer that never pauses, nor skip the safety holds."""

from __future__ import annotations

import overleaf_sync
from overleaf_sync import OverleafSync

def test_a_writer_that_never_pauses_is_still_pushed(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": "base", "version": 1, "path": "main.tex"}
    clock = [1000.0]
    monkeypatch.setattr(overleaf_sync._time, "monotonic", lambda: clock[0])
    pushed = []
    for i in range(40):                         # the content changes at every poll
        clock[0] += 0.5
        pushed += sync._settled([("d1", "base", f"base + edit {i}")])
        if pushed:
            break
    assert pushed and clock[0] - 1000.0 <= sync.MAX_DEFER_SECS + 1.5


def test_a_changing_wipe_is_not_pushed_early(tmp_path, monkeypatch):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    old = "x" * 500
    sync._docs["d1"] = {"content": old, "version": 1, "path": "main.tex"}
    clock = [0.0]
    monkeypatch.setattr(overleaf_sync._time, "monotonic", lambda: clock[0])
    for i in range(20):
        clock[0] += 0.5
        assert sync._settled([("d1", old, "x" * i)]) == []     # shrinking file: truncation in progress


def _records(tmp_path):
    import json
    out = []
    for f in (tmp_path / ".overleaf-sync-backup").rglob("deleted.jsonl"):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_deleted_text_is_recorded_before_the_push(tmp_path):
    from overleaf_sync import compute_ot_ops
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    old = "keep this\ncoauthor sentence that must stay recoverable\ntail\n"
    new = "keep this\ntail\n"
    sync._snapshot_before_delete("main.tex", old, compute_ot_ops(old, new))
    rec = _records(tmp_path)
    assert [r["deleted"] for r in rec] == ["coauthor sentence that must stay recoverable\n"]
    assert rec[0]["path"] == "main.tex" and rec[0]["pos"] == len("keep this\n")
    assert rec[0]["lines"] == "coauthor sentence that must stay recoverable\n"
    assert not list((tmp_path / ".overleaf-sync-backup").rglob("*.before-push"))   # small: no full copy


def test_nothing_recorded_for_additions(tmp_path):
    from overleaf_sync import compute_ot_ops
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._snapshot_before_delete("main.tex", "abc", compute_ot_ops("abc", "abc more"))
    assert not (tmp_path / ".overleaf-sync-backup").exists()


def test_big_deletion_also_keeps_a_full_copy_and_copies_are_pruned(tmp_path):
    from overleaf_sync import compute_ot_ops
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync.SNAPSHOT_MAX_FILES = 3
    old = "x" * 3000
    for _ in range(6):
        sync._snapshot_before_delete("main.tex", old, compute_ot_ops(old, ""))
    snaps = list((tmp_path / ".overleaf-sync-backup").rglob("*.before-push"))
    assert len(snaps) == 3 and all(p.read_text() == old for p in snaps)
    assert len(_records(tmp_path)) == 6


def test_record_keeps_the_whole_line_when_only_part_differs(tmp_path):
    from overleaf_sync import compute_ot_ops
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    old = "<<TOKEN-30>>\n<<TOKEN-31>>\n"
    new = "<<TOKEN-31>>\n"                      # the diff may delete just "30>>\n<<TOKEN-" and keep the shared prefix
    sync._snapshot_before_delete("main.tex", old, compute_ot_ops(old, new))
    assert any("<<TOKEN-30>>" in r["lines"] for r in _records(tmp_path))
