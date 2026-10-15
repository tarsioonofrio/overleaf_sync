"""
Tests for a coauthor typing on Overleaf while the user edits the local file.

FakeServer mimics Overleaf's ShareJS server: a client op submitted at an old
version is transformed (as "left") against the ops applied since, and every
applied op is broadcast in order: to us as a stripped {v, doc} echo for our
own ops, with the op for the coauthor's.
"""

from __future__ import annotations

import queue
import random
import threading

import pytest

from overleaf_sync import OverleafSync, apply_sharejs_ops, compute_ot_ops, transform_x


class FakeServer:
    def __init__(self, doc: str, version: int = 10):
        self.doc = doc
        self.version = version
        self.history: dict[int, list[dict]] = {}
        self.outbox: list[dict] = []  # broadcast to our client, in order

    def submit(self, op: list[dict], v: int, ours: bool) -> None:
        for hv in range(v, self.version):
            op, _ = transform_x(op, self.history[hv])
        self.doc = apply_sharejs_ops(self.doc, op)
        self.history[self.version] = op
        msg = {"v": self.version, "doc": "d1"}
        if not ours:
            msg.update(op=op, meta={"source": "P.other"})
        self.outbox.append(msg)
        self.version += 1

    def coauthor_edit(self, new_doc: str) -> None:
        self.submit(compute_ot_ops(self.doc, new_doc), self.version, ours=False)


class FakeWS:
    def __init__(self):
        self.sent: queue.Queue = queue.Queue()

    def send_event(self, name, args, callback=None):
        self.sent.put(args[1])

    def is_connected(self):
        return True


def _setup(tmp_path, doc: str):
    server = FakeServer(doc)
    sync = OverleafSync("pid", "cookie", str(tmp_path))
    sync._public_id = "P.ours"
    sync._docs["d1"] = {"content": doc, "version": server.version, "path": "main.tex"}
    sync._ws = FakeWS()
    sync._ws_ready.set()
    f = tmp_path / "main.tex"
    f.write_text(doc, encoding="utf-8")
    return server, sync, f


def _deliver_all(server, sync):
    while server.outbox:
        sync._on_ot_update([server.outbox.pop(0)])


def _start_push(sync) -> threading.Thread:
    def run():
        for doc_id, old, new in sync._collect_local_changes():
            sync._push_change(doc_id, old, new)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _wait_sent(sync, t):
    """Wait until the push thread has sent its update, or found nothing to push."""
    while t.is_alive():
        try:
            return sync._ws.sent.get(timeout=0.01)
        except queue.Empty:
            pass
    return None


def test_coauthor_op_does_not_overwrite_unpushed_local_edit(tmp_path):
    server, sync, f = _setup(tmp_path, "Hello World.\n")
    f.write_text("Hello big World.\n", encoding="utf-8")  # saved, not yet polled
    server.coauthor_edit("Hello World. Bye.\n")
    _deliver_all(server, sync)
    assert f.read_text(encoding="utf-8") == "Hello big World. Bye.\n"
    assert sync._docs["d1"]["content"] == server.doc == "Hello World. Bye.\n"


def test_coauthor_op_during_inflight_push(tmp_path):
    server, sync, f = _setup(tmp_path, "abc def\n")
    f.write_text("abc XX def\n", encoding="utf-8")
    t = _start_push(sync)
    update = _wait_sent(sync, t)
    assert update is not None and len(update["op"]) == 1
    # Server applied the coauthor's op before ours; it reaches us first.
    server.coauthor_edit("abc def ghi\n")
    _deliver_all(server, sync)
    assert f.read_text(encoding="utf-8") == "abc XX def ghi\n"
    server.submit(update["op"], update["v"], ours=True)
    _deliver_all(server, sync)
    t.join(timeout=5)
    assert not t.is_alive()
    assert server.doc == "abc XX def ghi\n"
    assert sync._docs["d1"]["content"] == server.doc
    assert f.read_text(encoding="utf-8") == server.doc
    assert not sync._docs["d1"].get("inflight")


def _random_edit(r: random.Random, s: str) -> str:
    p = r.randint(0, len(s))
    q = min(len(s), p + r.randint(0, 3))
    return s[:p] + "".join(r.choice("xyz \n") for _ in range(r.randint(0, 4))) + s[q:]


@pytest.mark.parametrize("seed", range(150))
def test_random_interleavings_converge(tmp_path, seed):
    r = random.Random(seed)
    server, sync, f = _setup(tmp_path, "The quick brown fox.\nJumps over.\n")
    t: threading.Thread | None = None
    pending: list[dict] = []  # our updates sent, not yet processed by server
    for _ in range(40):
        action = r.choice(["local", "remote", "push", "process", "deliver"])
        if action == "local":
            f.write_text(_random_edit(r, f.read_text(encoding="utf-8")), encoding="utf-8")
        elif action == "remote":
            server.coauthor_edit(_random_edit(r, server.doc))
        elif action == "push" and (t is None or not t.is_alive()):
            t = _start_push(sync)
            update = _wait_sent(sync, t)
            if update:
                pending.append(update)
        elif action == "process" and pending:
            u = pending.pop(0)
            server.submit(u["op"], u["v"], ours=True)
        elif action == "deliver" and server.outbox:
            sync._on_ot_update([server.outbox.pop(0)])
    # Quiesce: flush everything, then push remaining local edits.
    for _ in range(10):
        for u in pending:
            server.submit(u["op"], u["v"], ours=True)
        pending.clear()
        _deliver_all(server, sync)
        if t is not None:
            t.join(timeout=5)
            assert not t.is_alive()
        t = _start_push(sync)
        update = _wait_sent(sync, t)
        if update is None:
            break
        pending.append(update)
    local = f.read_text(encoding="utf-8")
    assert local == server.doc
    assert sync._docs["d1"]["content"] == server.doc
    assert sync._docs["d1"]["version"] == server.version
