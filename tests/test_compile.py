"""Compiling on Overleaf: the log parser and the way the language server reports the result."""
from __future__ import annotations

import io
import json
import threading
import time

import pytest

from overleaf_sync import compile as oc
from overleaf_sync.lsp import Server

LOG = """\
This is pdfTeX, Version 3.141592653 (TeX Live 2026)
(./main.tex
LaTeX2e <2026-06-01>
(/usr/share/texlive/texmf-dist/tex/latex/base/article.cls
Document Class: article 2025/01/22 v1.4n Standard LaTeX document class
(/usr/share/texlive/texmf-dist/tex/latex/base/size10.clo))
(./sections/intro.tex
! Undefined control sequence.
l.12 \\foo
          bar
? 
)
LaTeX Warning: Reference `fig:x' on page 1 undefined on input line 15.

Package natbib Warning: Citation `a' on page 1 undefined on input line 20.

Overfull \\hbox (5.0pt too wide) in paragraph at lines 30--31
[]\\OT1/cmr/m/n/10 text
)
"""


def by(diags, severity):
    return [d for d in diags if d.severity == severity]


def test_error_in_an_input_file_gets_that_file_the_line_and_the_column():
    (e,) = by(oc.parse_log(LOG), oc.ERROR)
    assert (e.file, e.line, e.column, e.message) == ("sections/intro.tex", 12, 4, "Undefined control sequence")


def test_warnings_after_a_file_closed_belong_to_the_file_that_includes_it():
    warns = by(oc.parse_log(LOG), oc.WARNING)
    assert [(w.file, w.line) for w in warns] == [("main.tex", 15), ("main.tex", 20)]
    assert warns[0].message == "LaTeX Warning: Reference `fig:x' on page 1 undefined".replace("LaTeX Warning: ", "")
    assert "Citation `a' on page 1 undefined" in warns[1].message


def test_overfull_boxes_are_only_hints():
    (b,) = by(oc.parse_log(LOG), oc.HINT)
    assert (b.file, b.line) == ("main.tex", 30) and b.message.startswith("Overfull")


def test_file_line_error_style():
    (e,) = oc.parse_log("(./main.tex\n./main.tex:7: Undefined control sequence.\nl.7 \\foo\n")
    assert (e.file, e.line, e.severity) == ("main.tex", 7, oc.ERROR)


def test_a_warning_wrapped_at_79_characters_is_read_whole():
    text = "LaTeX Warning: Reference `" + "x" * 40 + "' on page 1 undefined on input"
    text = text[:79] + "\n" + text[79:] + " line 9.\n"
    assert len(text.splitlines()[0]) == 79
    (w,) = oc.parse_log("(./main.tex\n" + text)
    assert w.line == 9 and "undefined" in w.message


def test_a_log_without_problems_gives_nothing():
    assert oc.parse_log("This is pdfTeX\n(./main.tex)\nOutput written on main.pdf (1 page).\n") == []


def test_unbalanced_parentheses_in_text_do_not_break_the_file_tracking():
    log = "(./main.tex\nsome text with an open ( paren\nLaTeX Warning: Reference `a' on page 1 undefined on input line 3.\n"
    (w,) = oc.parse_log(log)
    assert (w.file, w.line) == ("main.tex", 3)


# ── the server side ──────────────────────────────────────────────────────────────────────────────

class FakeCompiler:
    def __init__(self, results, gate=None):
        self.results, self.gate, self.calls = list(results), gate, 0

    def compile(self):
        self.calls += 1
        if self.gate:
            self.gate.wait(5)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def server_with(compiler, tmp_path, **kw):
    out = io.BytesIO()
    srv = Server(io.BytesIO(b""), out)
    srv.root, srv.compiler = tmp_path, compiler
    for k, v in kw.items():
        setattr(srv, k, v)
    return srv, out


def sent(out):
    data, msgs = out.getvalue(), []
    while data:
        head, _, rest = data.partition(b"\r\n\r\n")
        n = int(head.split(b":")[1])
        msgs.append(json.loads(rest[:n]))
        data = rest[n:]
    return msgs


def result(*diags, status="success"):
    return oc.CompileResult(status, "log", list(diags))


def wait_idle(srv):
    for _ in range(200):
        if not srv._compile_running:
            return
        time.sleep(0.01)
    raise AssertionError("compile never finished")


def test_diagnostics_are_published_per_file_and_cleared_when_the_file_is_clean_again(tmp_path):
    comp = FakeCompiler([result(oc.Diag("main.tex", 3, oc.ERROR, "boom", 2), oc.Diag("a.tex", 1, oc.WARNING, "w")),
                         result(oc.Diag("a.tex", 1, oc.WARNING, "w"))])
    srv, out = server_with(comp, tmp_path)
    srv.request_compile(); wait_idle(srv)
    srv.request_compile(); wait_idle(srv)
    pubs = [m["params"] for m in sent(out) if m.get("method") == "textDocument/publishDiagnostics"]
    main_uri = (tmp_path / "main.tex").resolve().as_uri()
    first = [p for p in pubs if p["uri"] == main_uri]
    assert first[0]["diagnostics"][0]["range"]["start"] == {"line": 2, "character": 2}
    assert first[0]["diagnostics"][0]["severity"] == oc.ERROR and first[0]["diagnostics"][0]["source"] == "overleaf"
    assert first[-1]["diagnostics"] == []                          # main.tex is clean in the second run


def test_the_message_counts_errors_and_warnings(tmp_path):
    comp = FakeCompiler([result(oc.Diag("main.tex", 1, oc.ERROR, "x"), oc.Diag("main.tex", 2, oc.WARNING, "y"), status="failure")])
    srv, out = server_with(comp, tmp_path)
    srv.request_compile(); wait_idle(srv)
    (m,) = [m for m in sent(out) if m.get("method") == "window/showMessage"]
    assert m["params"]["type"] == 1 and "1 error(s), 1 warning(s)" in m["params"]["message"]


def test_a_compile_that_cannot_start_is_reported_and_the_next_one_still_works(tmp_path):
    comp = FakeCompiler([oc.CompileError("Overleaf is rate limiting compiles"), result()])
    srv, out = server_with(comp, tmp_path)
    srv.request_compile(); wait_idle(srv)
    srv.request_compile(); wait_idle(srv)
    msgs = [m["params"]["message"] for m in sent(out) if m.get("method") == "window/showMessage"]
    assert "rate limiting" in msgs[0] and msgs[1].startswith("overleaf-sync: compiled")


def test_a_request_while_compiling_is_queued_once_and_runs_afterwards(tmp_path):
    gate = threading.Event()
    comp = FakeCompiler([result(), result()], gate)
    srv, _ = server_with(comp, tmp_path)
    assert srv.request_compile() == "started"
    assert srv.request_compile() == "queued" and srv.request_compile() == "queued"
    gate.set(); wait_idle(srv)
    assert comp.calls == 2                                          # three requests, two compiles


def test_the_command_and_the_code_action(tmp_path):
    srv, _ = server_with(FakeCompiler([result()]), tmp_path)
    assert srv.execute("overleaf.compile", []) == {"compile": "started"}
    wait_idle(srv)
    (action,) = srv.code_actions()
    assert action["command"]["command"] == "overleaf.compile"
    bare = Server(io.BytesIO(b""), io.BytesIO())
    assert bare.code_actions() == [] and "error" in bare.execute("overleaf.compile", [])


@pytest.mark.parametrize("enabled", [False, True])
def test_saving_compiles_only_when_asked_to(tmp_path, enabled):
    comp = FakeCompiler([result()])
    srv, _ = server_with(comp, tmp_path, compile_on_save=enabled)
    srv.handle({"jsonrpc": "2.0", "method": "textDocument/didSave", "params": {"textDocument": {"uri": (tmp_path / "main.tex").as_uri()}}})
    time.sleep(2.0 if enabled else 0.3)
    assert comp.calls == (1 if enabled else 0)
