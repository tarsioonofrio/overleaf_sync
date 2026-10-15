"""
Language server for Overleaf projects: `overleaf-sync-lsp` (or `overleaf-sync lsp`), speaks LSP on stdio.

It is the file-sync engine (`overleaf_sync.sync.OverleafSync`) running inside the language server,
with one difference: a document that is **open in the editor** is read from the editor's buffer
(`textDocument/didChange`, full-text sync) instead of from the file, and remote changes to it are
delivered with `workspace/applyEdit` instead of being written to the file.  Documents that are not
open keep syncing through their files, so agents and other tools still see an up-to-date folder.
Everything else (merging, reconnects, safeguards, backups) is the same code.

Keeping the editor and Overleaf consistent
------------------------------------------
Remote text that the editor has not applied yet must never be mistaken for a local deletion (the engine
would push the *reverse* of the remote edit).  So while an edit is on its way to the editor the engine
reads the text it has promised the editor ("pending"), not the buffer.  Each round:

  1. desired = merge3(base, buffer, pending), where `base` is the buffer text the editor is known to
     have had before our edit, so characters typed meanwhile are kept;
  2. send `workspace/applyEdit` (a minimal edit, guarded by the document version);
  3. on `applied: true` wait for the editor's own `didChange` that reports the new version, then go
     again if more remote text arrived meanwhile; on `applied: false` try again from the new buffer.

If that does not settle (a client whose `applied` answer is not trustworthy, repeated rejections) the
document is marked diverged: nothing more is pushed for it, the user is told, and
`overleaf.reloadDocument` puts Overleaf's text into the buffer (the buffer's own text is saved under
`.overleaf-sync-backup/` first).  Closing a document writes its last text to the file, so the file
and Overleaf agree again.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import sync as _sync
from .ot import merge3

MAX_TRIES = 6
ACK_TIMEOUT = 8.0          # seconds to wait for the editor's didChange after an applied edit
RETRY_DELAY = 0.25


# ── LSP text positions (UTF-16 code units, the default encoding) ──────────────────────────────────

def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def offset_to_position(text: str, i: int) -> dict:
    line = text.count("\n", 0, i)
    start = text.rfind("\n", 0, i) + 1
    return {"line": line, "character": utf16_len(text[start:i])}


def minimal_edit(old: str, new: str) -> dict:
    """One TextEdit turning old into new: everything between the common prefix and suffix."""
    p = 0
    n = min(len(old), len(new))
    while p < n and old[p] == new[p]:
        p += 1
    s = 0
    while s < n - p and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    return {"range": {"start": offset_to_position(old, p), "end": offset_to_position(old, len(old) - s)},
            "newText": new[p:len(new) - s]}


def uri_to_path(uri: str) -> Path | None:
    u = urlparse(uri)
    return Path(unquote(u.path)) if u.scheme == "file" else None


def path_to_uri(p: Path) -> str:
    from urllib.parse import quote
    return "file://" + quote(str(p))


# ── an open document ──────────────────────────────────────────────────────────────────────────────

class OpenDoc:
    def __init__(self, uri: str, rel: str, text: str, version: int):
        self.uri, self.rel, self.text, self.version = uri, rel, text, version
        self.pending: dict | None = None      # {base, text, sent, sent_version, await_version, tries, diverged}


class LspSync(_sync.OverleafSync):
    """OverleafSync whose open documents live in the editor."""

    def __init__(self, *a, send_request=None, notify=None, **kw):
        super().__init__(*a, **kw)
        self._send_request = send_request or (lambda method, params, cb: None)
        self._notify = notify or (lambda kind, msg: None)
        self._open: dict[str, OpenDoc] = {}
        self._olock = threading.RLock()

    # -- helpers ----------------------------------------------------------------------------------

    def _rel(self, path: Path) -> str | None:
        try:
            return Path(path).relative_to(self.output_dir).as_posix()
        except ValueError:
            return None

    def _doc(self, path: Path) -> OpenDoc | None:
        rel = self._rel(path)
        return self._open.get(rel) if rel is not None else None

    def _warn_once(self, key: str, msg: str) -> None:
        fresh = key not in self._warned
        super()._warn_once(key, msg)
        if fresh:
            self._notify("warning", msg)

    # -- the four hooks of the engine -------------------------------------------------------------

    def _read_local(self, path: Path) -> str:
        with self._olock:
            od = self._doc(path)
            if od is None:
                return super()._read_local(path)
            return od.pending["text"] if od.pending else od.text

    def _local_exists(self, path: Path) -> bool:
        return self._doc(path) is not None or super()._local_exists(path)

    def _update_local(self, path: Path, fn, default=None):
        with self._olock:
            od = self._doc(path)
            if od is None:
                return super()._update_local(path, fn, default)
            seen = od.pending["text"] if od.pending else od.text
            new = fn(seen)
            if new != seen:
                if od.pending is None:
                    od.pending = {"base": od.text, "text": new, "sent": None, "sent_version": None, "sent_text": None,
                                  "await_version": None, "tries": 0, "diverged": False}
                else:
                    od.pending["text"] = new
                self._pump(od)
            return new, seen

    def _write_local(self, path: Path, text: str) -> None:
        if self._doc(path) is None:
            return super()._write_local(path, text)
        self._update_local(path, lambda _: text)

    # -- delivering text to the editor ------------------------------------------------------------

    def _pump(self, od: OpenDoc) -> None:
        """Send the next edit if none is on its way (call with the lock held)."""
        p = od.pending
        if p is None or p["sent"] is not None or p["await_version"] is not None or p["diverged"]:
            return
        mine = od.text
        desired = merge3(p["base"], mine, p["text"]) if mine != p["base"] else p["text"]
        if desired == mine:
            od.pending = None                      # the editor already has everything
            return
        if p["tries"] >= MAX_TRIES:
            self._diverge(od, f"the editor kept refusing edits to {od.rel}")
            return
        # `sent_text` is the engine's view this edit was computed from: if it is applied, the editor holds
        # sent_text plus whatever was typed meanwhile, and `sent_text` becomes the base for any follow-up.
        p["sent"], p["sent_version"], p["sent_text"] = desired, od.version, p["text"]
        edit = {"documentChanges": [{"textDocument": {"uri": od.uri, "version": od.version},
                                     "edits": [minimal_edit(mine, desired)]}]}
        self._send_request("workspace/applyEdit", {"label": "Overleaf", "edit": edit},
                           lambda result, error, _od=od, _d=desired, _v=od.version: self._on_applied(_od, _d, _v, result, error))

    def _on_applied(self, od: OpenDoc, desired: str, version: int, result, error) -> None:
        with self._olock:
            p = od.pending
            if p is None or p["sent"] != desired:
                return
            p["sent"] = None
            applied = bool(result and result.get("applied")) and not error
            if applied:
                p["base"] = p["sent_text"]
                p["tries"] = 0
                if od.version > version:
                    p["await_version"] = None      # the editor already reported a newer version
                else:
                    p["await_version"] = version
                    threading.Timer(ACK_TIMEOUT, self._ack_timeout, (od, version)).start()
                if p["text"] == p["base"] and p["await_version"] is None:
                    od.pending = None
                    return
                self._pump(od)
            else:
                p["tries"] += 1
                threading.Timer(RETRY_DELAY * p["tries"], self._retry, (od,)).start()

    def _retry(self, od: OpenDoc) -> None:
        with self._olock:
            if self._open.get(od.rel) is od:
                self._pump(od)

    def _ack_timeout(self, od: OpenDoc, version: int) -> None:
        with self._olock:
            p = od.pending
            if p is not None and p["await_version"] == version and self._open.get(od.rel) is od:
                self._diverge(od, f"the editor did not report the edit to {od.rel}")

    def _diverge(self, od: OpenDoc, why: str) -> None:
        od.pending["diverged"] = True
        self._warn_once(f"diverged:{od.rel}:{od.version}",
                        f"{why}. {od.rel} is no longer syncing; nothing is sent for it. "
                        f"Run the command 'overleaf.reloadDocument' to take Overleaf's text (yours is backed up first).")

    # -- editor notifications ---------------------------------------------------------------------

    def did_open(self, uri: str, text: str, version: int) -> None:
        path = uri_to_path(uri)
        rel = self._rel(path) if path else None
        if rel is None:
            return
        with self._olock:
            self._open[rel] = OpenDoc(uri, rel, text, version)

    def did_change(self, uri: str, text: str, version: int) -> None:
        path = uri_to_path(uri)
        rel = self._rel(path) if path else None
        with self._olock:
            od = self._open.get(rel)
            if od is None:
                return
            od.text, od.version = text, version
            p = od.pending
            if p is not None and p["await_version"] is not None and version > p["await_version"]:
                p["await_version"] = None
                if p["text"] == p["base"]:
                    od.pending = None
                else:
                    self._pump(od)

    def did_close(self, uri: str) -> None:
        path = uri_to_path(uri)
        rel = self._rel(path) if path else None
        with self._olock:
            od = self._open.pop(rel, None)
            if od is None:
                return
            text = od.pending["text"] if od.pending else od.text
            try:
                _sync.atomic_write(self.output_dir / rel, text)     # the file and Overleaf agree again
            except OSError as e:
                self._warn_once(f"close-write:{rel}", f"could not write {rel} when it was closed: {e}")

    def reload_document(self, uri: str) -> bool:
        """Put the engine's current text of the document into the buffer, keeping the buffer's own text."""
        path = uri_to_path(uri)
        rel = self._rel(path) if path else None
        with self._olock:
            od = self._open.get(rel)
            if od is None:
                return False
            with self._lock:
                state = next((s for s in self._docs.values() if s["path"] == rel), None)
            target = od.pending["text"] if od.pending else (state["content"] if state else od.text)
            if target != od.text:
                try:
                    dest = self.output_dir / ".overleaf-sync-backup" / self._started / "buffers" / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(od.text, encoding="utf-8")
                except OSError as e:
                    self._warn_once(f"reload-backup:{rel}", f"cannot back up the buffer ({e}); not reloading")
                    return False
            od.pending = {"base": od.text, "text": target, "sent": None, "sent_version": None, "sent_text": None,
                          "await_version": None, "tries": 0, "diverged": False}
            self._pump(od)
            return True

    def status(self) -> dict:
        with self._olock:
            return {"open": {rel: {"pending": bool(od.pending), "diverged": bool(od.pending and od.pending["diverged"])}
                             for rel, od in self._open.items()},
                    "docs": len(self._docs), "connected": bool(self._ws_ready.is_set())}


# ── JSON-RPC over stdio ───────────────────────────────────────────────────────────────────────────

class Server:
    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile
        self._wlock = threading.Lock()
        self._next_id = 1
        self._callbacks: dict[int, callable] = {}
        self.sync: LspSync | None = None
        self.root: Path | None = None
        self.options: dict = {}
        self._shutdown = False
        self._thread: threading.Thread | None = None

    # transport
    def _write(self, msg: dict) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self._wlock:
            self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.wfile.flush()

    def _read(self) -> dict | None:
        length = None
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        if length is None:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def request(self, method: str, params: dict, callback) -> None:
        with self._wlock:
            rid, self._next_id = self._next_id, self._next_id + 1
            self._callbacks[rid] = callback
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

    def notify(self, kind: str, msg: str) -> None:
        level = {"error": 1, "warning": 2, "info": 3}.get(kind, 3)
        self._write({"jsonrpc": "2.0", "method": "window/showMessage", "params": {"type": level, "message": f"overleaf-sync: {msg}"}})

    # lifecycle
    def serve(self) -> int:
        while True:
            msg = self._read()
            if msg is None:
                break
            try:
                if self.handle(msg) == "exit":
                    break
            except Exception as e:                      # never let one bad message kill the session
                print(f"[lsp] error handling {msg.get('method')}: {e!r}", file=sys.stderr, flush=True)
                if "id" in msg and "method" in msg:
                    self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})
        self.stop()
        return 0 if self._shutdown else 1

    def stop(self) -> None:
        if self.sync is not None:
            self.sync._running = False
            self.sync._disconnect_ws()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def handle(self, msg: dict):
        method = msg.get("method")
        if method is None:                               # a response to one of our requests
            cb = self._callbacks.pop(msg.get("id"), None)
            if cb:
                cb(msg.get("result"), msg.get("error"))
            return None
        params = msg.get("params") or {}
        rid = msg.get("id")
        if method == "initialize":
            folders = params.get("workspaceFolders") or []
            root_uri = (folders[0]["uri"] if folders else None) or params.get("rootUri")
            self.root = uri_to_path(root_uri) if root_uri else None
            self.options = params.get("initializationOptions") or {}
            self._write({"jsonrpc": "2.0", "id": rid, "result": {
                "capabilities": {
                    "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
                    "executeCommandProvider": {"commands": ["overleaf.status", "overleaf.reloadDocument", "overleaf.confirm"]},
                },
                "serverInfo": {"name": "overleaf-sync-lsp"}}})
        elif method == "initialized":
            self.start_sync()
        elif method == "shutdown":
            self._shutdown = True
            self._write({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "exit":
            return "exit"
        elif method == "textDocument/didOpen" and self.sync:
            d = params["textDocument"]
            self.sync.did_open(d["uri"], d["text"], d["version"])
        elif method == "textDocument/didChange" and self.sync:
            changes = params.get("contentChanges") or []
            if changes and "range" not in changes[-1]:
                self.sync.did_change(params["textDocument"]["uri"], changes[-1]["text"], params["textDocument"]["version"])
        elif method == "textDocument/didClose" and self.sync:
            self.sync.did_close(params["textDocument"]["uri"])
        elif method == "workspace/executeCommand":
            result = self.execute(params.get("command"), params.get("arguments") or [])
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})
        elif rid is not None:
            self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
        return None

    def execute(self, command: str | None, args: list):
        if self.sync is None:
            return None
        if command == "overleaf.status":
            return self.sync.status()
        if command == "overleaf.reloadDocument" and args:
            uri = args[0] if isinstance(args[0], str) else args[0].get("uri")
            return self.sync.reload_document(uri)
        if command == "overleaf.confirm":
            (self.sync.output_dir / self.sync.WIPE_SENTINEL).touch()
            return True
        return None

    # the sync itself
    def start_sync(self) -> None:
        try:
            settings, cookie = self.resolve()
        except SystemExit as e:
            self.notify("error", str(e.code) if e.code not in (None, 0, 1) else "could not start: see the server log")
            return
        except Exception as e:
            self.notify("error", f"could not start: {e}")
            return
        self.sync = LspSync(settings["project_id"], cookie, settings["output_dir"],
                            dry_run=settings["dry_run"], allow_wipe=settings["allow_wipe"],
                            propagate_tree=settings["tree_sync"], prune_backups_days=settings["prune_backups_days"],
                            send_request=self.request, notify=self.notify)

        def run():
            try:
                self.sync.run()
            except SystemExit as e:
                self.notify("error", f"sync stopped (exit {e.code}); see the server log")
            except Exception as e:
                self.notify("error", f"sync stopped: {e}")
        self._thread = threading.Thread(target=run, name="overleaf-sync", daemon=True)
        self._thread.start()

    def resolve(self):
        """(settings, cookie) from the workspace's .overleaf-sync.toml plus initializationOptions."""
        import argparse
        from . import client as overleaf_cli
        root = self.root or Path.cwd()
        found = next((d / ".overleaf-sync.toml" for d in (root, *root.parents) if (d / ".overleaf-sync.toml").is_file()), None)
        cfg, cfg_path = _sync.load_config(str(found) if found else None) if found else ({}, None)
        cfg = {**cfg, **{k: v for k, v in self.options.items() if k in ("project_id", "url", "cookie_name", "cookie_file", "output_dir")}}
        args = argparse.Namespace(project_id=None, output_dir=None, url=None, cookie_name=None, cookie_file=None, dry_run=False, allow_wipe=False,
                                  allow_unknown_protocol=False, no_tree_sync=False, prune_backups=None)
        if not cfg.get("project_id"):
            raise SystemExit("no project: run `overleaf-sync init <project URL>` in the workspace folder first")
        settings = _sync.resolve_settings(args, cfg, cfg_path or (root / ".overleaf-sync.toml"))
        overleaf_cli.configure(settings["url"], settings["cookie_name"])
        cookie = _sync.read_cookie_file(settings["cookie_file"]) if settings["cookie_file"] else None
        cookie = _sync.load_overleaf_cookie(explicit_cookie=cookie)
        return settings, cookie


def main(argv: list[str] | None = None) -> int:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr                  # the engine prints progress: it must not touch the LSP channel
    return Server(sys.stdin.buffer, out).serve()


if __name__ == "__main__":
    sys.exit(main())
