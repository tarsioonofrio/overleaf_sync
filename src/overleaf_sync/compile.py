"""Compile the project on Overleaf and turn the LaTeX log into diagnostics.

Compiling never changes the project: Overleaf builds what its servers already hold (the sync keeps them equal to
the editor) and returns the log and the PDF.  Everything here is independent of the editor; the language server
publishes the result as diagnostics.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import client as _client

ERROR, WARNING, INFO, HINT = 1, 2, 3, 4          # LSP DiagnosticSeverity

PROJECT_FILE = re.compile(r"^(?:\./)?[^\s/][^\s]*\.(?:tex|sty|cls|bib|bbl|def|cfg|clo|ltx|dtx|ist|bst)$", re.I)
FILE_LINE_ERROR = re.compile(r"^(\.?/?[^\s:()]+\.(?:tex|sty|cls|bib|ltx)):(\d+): (.*)$")
WARNING_START = re.compile(r"^(?:LaTeX|Package|Class)(?: [\w.-]+)? Warning: (.*)$")
BOX = re.compile(r"^(Overfull|Underfull) \\[hv]box .*?(?:at|in) lines? (\d+)(?:--(\d+))?")
INPUT_LINE = re.compile(r"on input line (\d+)")
LINE_CONTEXT = re.compile(r"^l\.(\d+) ?(.*)$")
MAX_PRINT_LINE = 79                               # TeX hard-wraps its log at this width


@dataclass
class Diag:
    file: str | None       # path as in the project ("main.tex"), None when unknown
    line: int               # 1-based
    severity: int
    message: str
    column: int = 0         # 0-based start column when the log tells where the error stopped


def _unwrap(text: str) -> list[str]:
    """TeX breaks log lines at 79 characters; put them back together."""
    out: list[str] = []
    carry = ""
    for raw in text.splitlines():
        line = carry + raw
        if len(raw) == MAX_PRINT_LINE:
            carry = line
            continue
        carry = ""
        out.append(line)
    if carry:
        out.append(carry)
    return out


def _paren_events(line: str, stack: list[str | None]) -> None:
    """Follow the '(file ... )' nesting that TeX prints around every file it reads."""
    i = 0
    while i < len(line):
        c = line[i]
        if c == "(":
            j = i + 1
            while j < len(line) and line[j] not in " ()\t":
                j += 1
            token = line[i + 1:j]
            stack.append(token if PROJECT_FILE.match(token) or token.startswith("./") else None)
            i = j
            continue
        if c == ")" and stack:
            stack.pop()
        i += 1


def _project_path(token: str | None) -> str | None:
    if not token:
        return None
    return token[2:] if token.startswith("./") else token


def parse_log(text: str, root_file: str = "main.tex") -> list[Diag]:
    """Errors and warnings of a pdfTeX/XeTeX/LuaTeX log, with the project file and line when the log says them."""
    lines = _unwrap(text)
    stack: list[str | None] = []
    diags: list[Diag] = []

    def current_file() -> str | None:
        for token in reversed(stack):
            path = _project_path(token)
            if path:
                return path
        return root_file

    i = 0
    while i < len(lines):
        line = lines[i]
        m = FILE_LINE_ERROR.match(line)
        if m:
            diags.append(Diag(_project_path(m.group(1)), int(m.group(2)), ERROR, m.group(3).strip().rstrip(".")))
            i += 1
            continue
        if line.startswith("! "):
            message = line[2:].strip().rstrip(".")
            file, lineno, column = current_file(), 1, 0
            j = i + 1
            while j < len(lines) and j <= i + 25:
                c = LINE_CONTEXT.match(lines[j])
                if c:
                    lineno, column = int(c.group(1)), len(c.group(2))
                    j += 2                                   # the context has a second line: the rest of the input
                    break
                j += 1
            else:
                j = i + 1
            diags.append(Diag(file, lineno, ERROR, message, column))
            i = max(j, i + 1)                                # no parentheses are read inside an error block
            continue
        w = WARNING_START.match(line)
        if w:
            message = w.group(1)
            j = i + 1
            while j < len(lines) and (re.match(r"^\(\S+\)\s", lines[j]) or lines[j].startswith(" " * 8)):
                message += " " + lines[j].strip()
                j += 1
            ln = INPUT_LINE.search(message)
            diags.append(Diag(current_file(), int(ln.group(1)) if ln else 1, WARNING,
                              INPUT_LINE.sub("", message).strip().rstrip(".").strip()))
            i = j
            continue
        b = BOX.match(line)
        if b:
            diags.append(Diag(current_file(), int(b.group(2)), HINT, line.split(" at lines")[0].split(" in ")[0].strip()))
            i += 1
            continue
        _paren_events(line, stack)
        i += 1
    return diags


@dataclass
class CompileResult:
    status: str                         # success | failure | ... as Overleaf says
    log: str
    diagnostics: list[Diag]
    pdf_path: Path | None = None
    log_path: Path | None = None

    @property
    def errors(self) -> int:
        return sum(d.severity == ERROR for d in self.diagnostics)

    @property
    def warnings(self) -> int:
        return sum(d.severity == WARNING for d in self.diagnostics)


class CompileError(RuntimeError):
    pass


class Compiler:
    """One compile at a time on one project."""

    BUILD_DIR = ".overleaf-sync-build"

    def __init__(self, project_id: str, cookie: str, output_dir: Path, save_pdf: bool = True):
        self.project_id, self.cookie, self.output_dir, self.save_pdf = project_id, cookie, Path(output_dir), save_pdf
        self._session: _client.OverleafSession | None = None

    def _session_ok(self) -> _client.OverleafSession:
        if self._session is None:
            s = _client.OverleafSession(self.cookie)
            s.get_project_bootstrap(self.project_id)
            self._session = s
        return self._session

    def compile(self) -> CompileResult:
        try:
            s = self._session_ok()
            data = self._post_compile(s)
        except _client.AuthError as e:
            self._session = None
            raise CompileError(str(e))
        except _client.RateLimited:
            raise CompileError("Overleaf is rate limiting compiles; wait a little and try again")
        except Exception as e:
            self._session = None                                # the CSRF token or the cookie may be stale: start over next time
            raise CompileError(f"could not start the compile: {e}")
        status = data.get("status", "unknown")
        files = {f.get("path"): f for f in data.get("outputFiles", [])}
        if status not in ("success", "failure") and "output.log" not in files:
            raise CompileError({"clsi-maintenance": "Overleaf's compile servers are in maintenance",
                                "too-recently-compiled": "compiled too recently; try again in a moment",
                                "compile-in-progress": "a compile is already running on Overleaf",
                                "timedout": "the compile timed out on Overleaf",
                                "autocompile-backoff": "Overleaf asked to slow down compiling"}.get(status, f"compile ended with status {status!r}"))
        log = self._download(s, data, files.get("output.log"))
        result = CompileResult(status, log, parse_log(log))
        if self.save_pdf or log:
            build = self.output_dir / self.BUILD_DIR
            build.mkdir(parents=True, exist_ok=True)
            if log:
                (build / "output.log").write_text(log, encoding="utf-8")
                result.log_path = build / "output.log"
            if self.save_pdf and "output.pdf" in files:
                try:
                    pdf = self._download(s, data, files["output.pdf"], binary=True)
                    (build / "output.pdf").write_bytes(pdf)
                    result.pdf_path = build / "output.pdf"
                except Exception:
                    pass                                         # the diagnostics are the main result
        return result

    def _post_compile(self, s: _client.OverleafSession) -> dict:
        body = {"rootDoc_id": None, "draft": False, "check": "silent", "incrementalCompilesEnabled": True,
                "stopOnFirstError": False, "editorId": str(uuid.uuid4())}
        r = s.session.post(f"{_client.BASE_URL}/project/{self.project_id}/compile?auto_compile=true&enable_pdf_caching=true",
                           json=body, headers=s._json_headers(), timeout=180)
        if r.status_code == 429:
            raise _client.RateLimited(r.headers.get("Retry-After"))
        if r.status_code in (401, 403):
            raise _client.AuthError(f"Overleaf refused the compile (HTTP {r.status_code}): expired session, or no access to this project")
        r.raise_for_status()
        return r.json()

    def _download(self, s: _client.OverleafSession, data: dict, entry: dict | None, binary: bool = False):
        if not entry:
            return b"" if binary else ""
        params = {k: v for k, v in (("compileGroup", data.get("compileGroup")), ("clsiserverid", data.get("clsiServerId"))) if v}
        r = s.session.get(_client.BASE_URL + entry["url"], params=params, timeout=120)
        r.raise_for_status()
        return r.content if binary else r.content.decode("utf-8", errors="replace")
