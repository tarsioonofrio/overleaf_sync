"""Language server: open documents live in the editor, remote text reaches it with applyEdit."""

from __future__ import annotations

import io
import json
import threading

import pytest

import overleaf_sync.lsp as lsp
from overleaf_sync.lsp import LspSync, Server, minimal_edit, offset_to_position, uri_to_path

BASE = "line one\nline two\nline three\n"


# ── position helpers ──────────────────────────────────────────────────────────────────────────────

def test_positions_count_utf16_units():
    text = "a\U0001F600b\nsecond"
    assert offset_to_position(text, 2) == {"line": 0, "character": 3}      # after the emoji: 2 units
    assert offset_to_position(text, 4) == {"line": 1, "character": 0}
    assert offset_to_position(text, len(text)) == {"line": 1, "character": 6}


def apply_text_edit(text: str, edit: dict) -> str:
    def off(pos):
        lines = text.split("\n")
        o = sum(len(l) + 1 for l in lines[:pos["line"]])
        units = 0
        for i, ch in enumerate(lines[pos["line"]]):
            if units >= pos["character"]:
                return o + i
            units += 2 if ord(ch) > 0xFFFF else 1
        return o + len(lines[pos["line"]])
    return text[:off(edit["range"]["start"])] + edit["newText"] + text[off(edit["range"]["end"]):]


@pytest.mark.parametrize("old,new", [
    ("abc", "abc"), ("", "x"), ("x", ""), ("hello world", "hello brave new world"),
    ("a\U0001F600b\nc", "a\U0001F600XYb\nc"), ("one\ntwo\nthree", "one\nthree"), ("aaa", "aaaa"), ("aaaa", "aaa"),
])
def test_minimal_edit_round_trips(old, new):
    assert apply_text_edit(old, minimal_edit(old, new)) == new


def test_minimal_edit_touches_only_the_changed_part():
    e = minimal_edit("one\ntwo\nthree", "one\nTWO\nthree")
    assert e["range"] == {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}} and e["newText"] == "TWO"


# ── a spec-following editor ───────────────────────────────────────────────────────────────────────

class Editor:
    """Holds a buffer with a version; answers applyEdit like a correct client; didChange timing is configurable."""

    def __init__(self, sync, path, text):
        self.sync, self.uri, self.text, self.version = sync, "file://" + str(path), text, 1
        self.inbox, self.lazy_changes, self.applied_flag = [], False, True
        self.pending_change = False
        sync._send_request = lambda method, params, cb: self.inbox.append((params, cb))
        sync.did_open(self.uri, text, self.version)

    def type(self, at, s):
        self.text = self.text[:at] + s + self.text[at:]
        self.version += 1
        self.sync.did_change(self.uri, self.text, self.version)

    def flush_change(self):
        if self.pending_change:
            self.pending_change = False
            self.sync.did_change(self.uri, self.text, self.version)

    def deliver(self):
        """Process every applyEdit received so far; returns how many were applied."""
        n, box, self.inbox = 0, self.inbox, []
        for params, cb in box:
            dc = params["edit"]["documentChanges"][0]
            if dc["textDocument"]["version"] != self.version:
                cb({"applied": False}, None)
                continue
            for e in dc["edits"]:
                self.text = apply_text_edit(self.text, e)
            self.version += 1
            n += 1
            if self.lazy_changes:
                self.pending_change = True         # neovim-style: didChange comes after the response
                cb({"applied": True}, None)
            else:
                self.sync.did_change(self.uri, self.text, self.version)
                cb({"applied": True}, None)
        return n


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, "RETRY_DELAY", 0.01)
    (tmp_path / "main.tex").write_text(BASE)
    sync = LspSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    ed = Editor(sync, tmp_path / "main.tex", BASE)
    ed.v = [5]

    def remote(pos, s):
        sync._on_ot_update([{"doc": "d1", "v": ed.v[0], "op": [{"p": pos, "i": s}], "meta": {"source": "P.other"}}])
        ed.v[0] += 1
    ed.remote, ed.sync_ = remote, sync
    return ed


def pushes(ed):
    return ed.sync_._collect_local_changes()


def test_a_local_keystroke_is_seen_as_a_change_to_push(env):
    env.type(4, "X")
    assert pushes(env) == [("d1", BASE, BASE[:4] + "X" + BASE[4:])]


def test_remote_text_arrives_as_an_edit_and_is_not_pushed_back(env):
    env.remote(0, "REMOTE ")
    assert len(env.inbox) == 1
    assert pushes(env) == []                       # before the editor applied it: no reverse push
    assert env.deliver() == 1
    assert env.text == "REMOTE " + BASE
    assert pushes(env) == []
    assert env.sync_._open["main.tex"].pending is None


def test_the_disk_file_is_not_touched_while_the_document_is_open(env, tmp_path):
    env.remote(0, "REMOTE ")
    env.deliver()
    assert (tmp_path / "main.tex").read_text() == BASE


def test_typing_between_request_and_apply_is_kept_and_nothing_is_reverted(env):
    env.remote(0, "R ")
    env.type(len(BASE), "TYPED")                 # the user types before the editor processes our edit
    assert env.deliver() == 0                    # stale version: rejected
    assert pushes(env) == []                     # the engine still reads the promised text
    for _ in range(50):                          # the retry timer re-sends from the new buffer
        if env.inbox:
            break
        threading.Event().wait(0.02)
    assert env.deliver() == 1
    assert env.text == "R " + BASE + "TYPED"
    assert pushes(env) == [("d1", "R " + BASE, "R " + BASE + "TYPED")]    # only the typing is pushed


def test_response_before_didchange_does_not_cause_a_revert(env):
    env.lazy_changes = True
    env.remote(0, "R ")
    env.deliver()                                # applied, but the editor has not reported the change yet
    assert env.sync_._open["main.tex"].text == BASE            # the engine still has the old buffer
    assert pushes(env) == []                                   # and must not push "BASE" over the server's text
    env.flush_change()
    assert pushes(env) == []
    assert env.sync_._open["main.tex"].pending is None


def test_a_second_remote_edit_while_the_first_is_in_flight(env):
    env.remote(0, "A ")
    env.remote(0, "B ")
    assert len(env.inbox) == 1                   # one edit at a time
    assert env.deliver() == 1
    assert env.deliver() == 1                    # the follow-up goes out after the first was reported
    assert env.text == "B A " + BASE
    assert pushes(env) == []


def test_remote_edit_and_local_edit_in_the_same_line_merge(env):
    env.type(5, "LOCAL ")                        # "line LOCAL one"
    env.remote(len(BASE), "tail\n")
    env.deliver()
    assert env.text == BASE[:5] + "LOCAL " + BASE[5:] + "tail\n"
    assert pushes(env) == [("d1", BASE + "tail\n", env.text)]


def test_an_editor_that_never_applies_is_marked_diverged_and_nothing_is_pushed(env):
    env.remote(0, "R ")
    for _ in range(lsp.MAX_TRIES + 2):
        env.type(len(env.text), ".")             # every attempt is stale: the user keeps typing
        env.deliver()
        threading.Event().wait(0.05 * 2)
    od = env.sync_._open["main.tex"]
    assert od.pending and od.pending["diverged"]
    assert pushes(env) == []                     # frozen: no revert, and no half-synced typing either


def test_reload_document_backs_up_the_buffer_and_takes_the_server_text(env, tmp_path):
    env.remote(0, "R ")
    od = env.sync_._open["main.tex"]
    od.pending["diverged"] = True
    env.inbox.clear()
    env.type(0, "MINE ")
    assert env.sync_.reload_document(env.uri)
    assert env.deliver() == 1
    assert env.text == "R " + BASE
    assert next((tmp_path / ".overleaf-sync-backup").rglob("buffers/main.tex")).read_text() == "MINE " + BASE


def test_closing_writes_the_last_text_to_the_file_and_hands_over_to_the_poller(env, tmp_path):
    env.remote(0, "R ")                          # promised but not applied yet
    env.sync_.did_close(env.uri)
    assert (tmp_path / "main.tex").read_text() == "R " + BASE
    assert "main.tex" not in env.sync_._open
    assert pushes(env) == []


def test_a_stale_or_repeated_didchange_is_ignored(env):
    env.type(0, "NEW ")
    env.sync_.did_change(env.uri, BASE, env.version - 1)          # an older report arriving late
    env.sync_.did_change(env.uri, "something else", env.version)   # the same version again
    assert env.sync_._open["main.tex"].text == "NEW " + BASE


def test_documents_that_are_not_open_still_use_the_file(env, tmp_path):
    env.sync_.did_close(env.uri)
    (tmp_path / "main.tex").write_text(BASE + "from disk\n")
    assert pushes(env) == [("d1", BASE, BASE + "from disk\n")]


def test_uri_outside_the_folder_is_ignored(env, tmp_path):
    env.sync_.did_open("file:///elsewhere/x.tex", "x", 1)
    assert list(env.sync_._open) == ["main.tex"]
    assert uri_to_path("untitled:Untitled-1") is None


# ── protocol ──────────────────────────────────────────────────────────────────────────────────────

def frame(msg):
    b = json.dumps(msg).encode()
    return b"Content-Length: %d\r\n\r\n" % len(b) + b


def run_session(messages, real_start=False):
    out = io.BytesIO()
    srv = Server(io.BytesIO(b"".join(frame(m) for m in messages)), out)
    if not real_start:
        srv.start_sync = lambda: None
    code = srv.serve()
    replies, data = [], out.getvalue()
    while data:
        head, _, rest = data.partition(b"\r\n\r\n")
        n = int(head.split(b":")[1])
        replies.append(json.loads(rest[:n]))
        data = rest[n:]
    return code, replies


def test_initialize_shutdown_exit():
    code, replies = run_session([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"rootUri": "file:///tmp/x"}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    caps = replies[0]["result"]["capabilities"]
    assert caps["textDocumentSync"]["change"] == 1 and "overleaf.reloadDocument" in caps["executeCommandProvider"]["commands"]
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": None}
    assert code == 0


def test_exit_without_shutdown_is_an_error_code():
    code, _ = run_session([{"jsonrpc": "2.0", "method": "exit"}])
    assert code == 1


def test_unknown_request_gets_an_error_not_silence():
    _, replies = run_session([{"jsonrpc": "2.0", "id": 7, "method": "textDocument/hover", "params": {}}, {"jsonrpc": "2.0", "method": "exit"}])
    assert replies[0]["error"]["code"] == -32601


def test_a_broken_message_does_not_end_the_session():
    _, replies = run_session([
        {"jsonrpc": "2.0", "id": 1, "method": "workspace/executeCommand", "params": {"command": "overleaf.status"}},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "exit"},
    ])
    assert [r.get("id") for r in replies] == [1, 2]


def test_the_engine_prints_to_stderr_not_to_the_protocol_channel(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(frame({"jsonrpc": "2.0", "method": "exit"}))})())
    real_out = sys.stdout
    try:
        lsp.main()
        assert sys.stdout is sys.stderr
    finally:
        sys.stdout = real_out


# ── randomised interleavings ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", range(150))
@pytest.mark.parametrize("lazy", [False, True])
def test_random_interleavings_lose_nothing_and_revert_nothing(tmp_path, monkeypatch, seed, lazy):
    import random
    rnd = random.Random(seed)
    monkeypatch.setattr(lsp, "RETRY_DELAY", 0.001)
    monkeypatch.setattr(lsp, "MAX_TRIES", 10**6)       # this schedule is far busier than any editor; divergence has its own test
    (tmp_path / "main.tex").write_text(BASE)
    sync = LspSync("pid", "cookie", str(tmp_path))
    sync._docs["d1"] = {"content": BASE, "version": 5, "path": "main.tex"}
    ed = Editor(sync, tmp_path / "main.tex", BASE)
    ed.lazy_changes = lazy
    v, remote_toks, typed_toks = 5, [], []

    def line_start(text):
        starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n" and i + 1 <= len(text)]
        return rnd.choice(starts)

    def settle_push():
        for doc_id, old, new in sync._collect_local_changes():     # what the poller would push, accepted at once
            sync._docs[doc_id]["content"] = new
            sync._docs[doc_id]["version"] += 1
            nonlocal_v[0] += 1
    nonlocal_v = [5]

    for step in range(60):
        a = rnd.random()
        if a < 0.3:
            tok = f"<R{step}>"
            cur = sync._docs["d1"]["content"]
            pos = line_start(cur)
            sync._on_ot_update([{"doc": "d1", "v": sync._docs["d1"]["version"], "op": [{"p": pos, "i": tok}], "meta": {"source": "P.o"}}])
            remote_toks.append(tok)
        elif a < 0.55:
            tok = f"<T{step}>"
            ed.type(line_start(ed.text), tok)
            typed_toks.append(tok)
        elif a < 0.85:
            ed.deliver()
        elif a < 0.92:
            ed.flush_change()
        else:
            settle_push()
        if rnd.random() < 0.3:
            settle_push()
    for _ in range(80):                                            # quiesce
        ed.deliver(); ed.flush_change(); settle_push()
        if not ed.inbox and sync._open["main.tex"].pending is None:
            break
        threading.Event().wait(0.003)
    final_server = sync._docs["d1"]["content"]
    assert not (sync._open["main.tex"].pending or {}).get("diverged")
    assert ed.text == final_server, (seed, lazy)
    for tok in remote_toks + typed_toks:
        assert ed.text.count(tok) == 1 and final_server.count(tok) == 1, (seed, lazy, tok)


def test_a_folder_without_config_gets_a_message_telling_how_to_init(tmp_path):
    code, replies = run_session([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"rootUri": tmp_path.as_uri()}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
        {"jsonrpc": "2.0", "method": "exit"},
    ], real_start=True)
    shown = [r for r in replies if r.get("method") == "window/showMessage"]
    assert shown and shown[0]["params"]["type"] == 1 and "overleaf-sync init" in shown[0]["params"]["message"]
    assert code == 0
