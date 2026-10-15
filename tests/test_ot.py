"""
Exhaustive tests for compute_ot_ops and apply_sharejs_ops.

The invariant: applying compute_ot_ops(old, new) to old one-op-at-a-time
(simulating the Overleaf server receiving each applyOtUpdate in sequence)
must yield exactly new.
"""
from __future__ import annotations

import pytest
from overleaf_sync import compute_ot_ops, apply_sharejs_ops


def server_apply(content: str, ops: list[dict]) -> str:
    """Simulate Overleaf server applying ops one by one in order."""
    for op in ops:
        p = op["p"]
        if "i" in op:
            content = content[:p] + op["i"] + content[p:]
        elif "d" in op:
            content = content[:p] + content[p + len(op["d"]):]
    return content


def roundtrip(old: str, new: str):
    """Assert that ops computed from old→new reproduce new when applied by server."""
    ops = compute_ot_ops(old, new)
    result = server_apply(old, ops)
    assert result == new, (
        f"FAIL: expected {new!r}, got {result!r}\n"
        f"  old={old!r}\n"
        f"  ops={ops}"
    )


# ── pure inserts ────────────────────────────────────────────────────────────

def test_insert_at_beginning():
    roundtrip("world", "hello world")

def test_insert_at_end():
    roundtrip("hello", "hello world")

def test_insert_in_middle():
    roundtrip("helloworld", "hello beautiful world")

def test_insert_empty_old():
    roundtrip("", "hello")

def test_insert_newline():
    roundtrip("line1\nline3", "line1\nline2\nline3")


# ── pure deletes ─────────────────────────────────────────────────────────────

def test_delete_at_beginning():
    roundtrip("hello world", "world")

def test_delete_at_end():
    roundtrip("hello world", "hello")

def test_delete_in_middle():
    roundtrip("hello beautiful world", "hello world")

def test_delete_all():
    roundtrip("hello", "")

def test_delete_newline():
    roundtrip("line1\nline2\nline3", "line1\nline3")


# ── replace (delete + insert at same position) ───────────────────────────────
# This is the class of operations that triggers the ordering bug.

def test_replace_at_beginning():
    roundtrip("Hello World", "Hi World")

def test_replace_at_end():
    roundtrip("Hello World", "Hello Earth")

def test_replace_in_middle():
    roundtrip("Hello World", "Hello Beautiful World")

def test_replace_longer_with_shorter():
    roundtrip("Hello World", "Hi W")

def test_replace_shorter_with_longer():
    roundtrip("Hi W", "Hello World")

def test_replace_whole_word():
    roundtrip("foo bar baz", "foo qux baz")

def test_replace_latex_command():
    old = r"\section{Introduction}"
    new = r"\section{Related Work}"
    roundtrip(old, new)

def test_replace_multiline():
    old = "line1\nold content here\nline3"
    new = "line1\nnew content here\nline3"
    roundtrip(old, new)

def test_replace_grows():
    roundtrip("Hello World", "Hi Earth and Beyond")

def test_replace_shrinks():
    roundtrip("Hi Earth and Beyond", "Hello World")


# ── realistic LaTeX edits ────────────────────────────────────────────────────

SAMPLE = r"""\documentclass{article}
\begin{document}
\section{Introduction}
This is an introduction.
\end{document}
"""

def test_latex_change_section_title():
    new = SAMPLE.replace("Introduction", "Background")
    roundtrip(SAMPLE, new)

def test_latex_add_paragraph():
    new = SAMPLE.replace(
        "This is an introduction.",
        "This is an introduction.\n\nMore text here."
    )
    roundtrip(SAMPLE, new)

def test_latex_delete_sentence():
    new = SAMPLE.replace("\nThis is an introduction.", "")
    roundtrip(SAMPLE, new)

def test_latex_replace_sentence():
    new = SAMPLE.replace("This is an introduction.", "This section motivates the work.")
    roundtrip(SAMPLE, new)

def test_latex_add_usepackage():
    new = SAMPLE.replace(r"\begin{document}", r"\usepackage{amsmath}" + "\n" + r"\begin{document}")
    roundtrip(SAMPLE, new)


# ── edge cases ───────────────────────────────────────────────────────────────

def test_identical():
    assert compute_ot_ops("abc", "abc") == []

def test_single_char_replace():
    roundtrip("a", "b")

def test_single_char_insert():
    roundtrip("", "x")

def test_single_char_delete():
    roundtrip("x", "")

def test_trailing_newline_add():
    roundtrip("hello", "hello\n")

def test_trailing_newline_remove():
    roundtrip("hello\n", "hello")

def test_only_newlines():
    roundtrip("\n\n", "\n\n\n")

def test_unicode():
    roundtrip("café", "café latte")

def test_unicode_replace():
    roundtrip("naïve", "naive")


# ── git backup comparison ────────────────────────────────────────────────────

def test_git_backup_roundtrip():
    """Verify ops roundtrip on the actual project file from git backup."""
    import subprocess, pathlib
    backup = pathlib.Path.home() / "workspace/overleaf_backup/69d7ba24e392a5121b52a73f/main.tex"
    if not backup.exists():
        pytest.skip("git backup not present")
    content = backup.read_text(encoding="utf-8")
    # Simulate a realistic edit: change a word in the content
    if "Introduction" in content:
        edited = content.replace("Introduction", "Background", 1)
        roundtrip(content, edited)
    # Simulate appending a line
    roundtrip(content, content + "\n% added line\n")
    # Simulate deleting first line
    lines = content.split("\n")
    if len(lines) > 1:
        roundtrip(content, "\n".join(lines[1:]))
