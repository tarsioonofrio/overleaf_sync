"""
Tests for decoding joinDoc lines (Overleaf sends them as UTF-8 bytes packed
one per char, i.e. JS `unescape(encodeURIComponent(line))`).
"""

from __future__ import annotations

import pytest

from overleaf_sync import decode_doc_line


def _server_encode(line: str) -> str:
    """What the Overleaf real-time server does to each line on joinDoc."""
    return line.encode("utf-8").decode("latin-1")


@pytest.mark.parametrize("line", [
    "  title={Do NFTs’ owners really possess their assets?},",
    "Smith, T’ai and Finucane",
    "é à ü ß — “quotes” …",
    "emoji 🎉 outside the BMP",
    "plain ascii",
    "",
])
def test_roundtrip(line):
    assert decode_doc_line(_server_encode(line)) == line


def test_real_server_payload():
    # Captured from a live joinDoc response.
    raw = "  title={Do NFTsâ\x80\x99 owners}"
    assert decode_doc_line(raw) == "  title={Do NFTs’ owners}"


def test_already_decoded_line_is_untouched():
    # Chars > U+00FF cannot come from the encoded form: leave as-is.
    assert decode_doc_line("NFTs’ owners") == "NFTs’ owners"
