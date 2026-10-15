"""
Tests for echo suppression in OverleafSync._on_ot_update.

Payloads are copied from live otUpdateApplied events (2026-10-07).
"""

from __future__ import annotations

from overleaf_sync import OverleafSync


def _sync(tmp_path, content="abc", version=10):
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._public_id = "P.ours"
    sync._docs["d1"] = {"content": content, "version": version, "path": "main.tex"}
    return sync


def test_stripped_own_echo_is_silent(tmp_path, capsys):
    sync = _sync(tmp_path)
    sync._on_ot_update([{"v": 10, "doc": "d1"}])
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "main.tex").exists()
    assert sync._docs["d1"]["content"] == "abc"
    assert sync._docs["d1"]["version"] == 11


def test_stripped_echo_never_lowers_version(tmp_path):
    sync = _sync(tmp_path, version=12)
    sync._on_ot_update([{"v": 10, "doc": "d1"}])
    assert sync._docs["d1"]["version"] == 12


def test_echo_with_our_source_is_silent(tmp_path, capsys):
    sync = _sync(tmp_path)
    sync._on_ot_update([{"v": 10, "doc": "d1", "op": [{"p": 0, "i": "x"}], "meta": {"source": "P.ours"}}])
    assert capsys.readouterr().out == ""
    assert sync._docs["d1"]["content"] == "abc"


def test_external_update_is_applied_and_logged(tmp_path, capsys):
    sync = _sync(tmp_path)
    sync._on_ot_update([{"v": 10, "doc": "d1", "op": [{"p": 3, "i": "d"}], "meta": {"source": "P.other"}}])
    assert "overleaf→local" in capsys.readouterr().out
    assert sync._docs["d1"]["content"] == "abcd"
    assert (tmp_path / "main.tex").read_text(encoding="utf-8") == "abcd"
    assert sync._docs["d1"]["version"] == 11
