#!/usr/bin/env python3
"""
overleaf_agent.py — Interactive REPL for live-editing Overleaf papers with an LLM
==================================================================================

HOW TO USE IN AN AGENT SESSION
------------------------------

1. Resolve your session cookie:
   By default the script reads `overleaf_session2` from Firefox's cookie jar.
   You can still pass `--cookie` explicitly to override this.

2. Start a REPL session:

   $ python3 overleaf_agent.py <project_id>

   If the project has multiple documents you will be shown a numbered list
   and asked to pick one:

     [1] main.tex          (doc: 69a81e69bdcdae63f5c4be41, v526)
     [2] references/refs.bib  ...
   Select document [1-2]: 1

   Then type instructions one after another:

     Editing: main.tex (v526, 18069 chars)
     >>> add an easter egg in the conclusion
       Assistant: Added a hidden comment in the conclusion
     Applying 1 OT operation...
     Done. (v527)
     >>> make the abstract shorter
     ...
     >>> exit

3. To skip the file picker, pass --doc-id:
   $ python3 overleaf_agent.py <project_id> \
       --doc-id 69a81e69bdcdae63f5c4be41

HOW IT WORKS
------------
  Session startup:
    - Connects to Overleaf via Socket.io (WebSocket, kept alive in a thread)
    - Joins the project and the selected document
    - Receives the current document content + version number

  On each instruction:
    - Sends current content + instruction to the model
    - The model returns the modified content via tool use
    - Diffs old vs new → minimal insert/delete OT operations
    - Pushes ops via the live WebSocket (applyOtUpdate)
    - Changes appear in Overleaf instantly; version increments

  Background WebSocket thread:
    - Heartbeats keep the connection alive between instructions
    - otUpdateApplied events from other collaborators are applied to the
      local content copy so the model always sees the latest version

USING FROM WITHIN AN AGENT SESSION
---------------------------------
Add to your local agent instructions:

  ## Tools
  - python3 overleaf_agent.py <project_id> [--cookie $OVERLEAF_COOKIE] [--doc-id <id>]
    Opens an interactive REPL to live-edit the Overleaf document.

Default auth source:
  Firefox cookies.sqlite → overleaf_session2

Override sources:
  --cookie
  OVERLEAF_COOKIE

For non-interactive use from an agent pass --doc-id so the file picker is
skipped.

Dependencies:
  pip install requests websocket-client anthropic

Environment (one of):
  ANTHROPIC_API_KEY=sk-ant-...
  ANTHROPIC_BASE_URL=... + ANTHROPIC_AUTH_TOKEN=...   (agent proxy)
  --api-key flag
"""

import argparse
import configparser
import datetime
import difflib
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# DNS fallback via getent (handles filtered /etc/resolv.conf)
# ---------------------------------------------------------------------------

@contextmanager
def dns_override_if_needed(url: str):
    """
    If Python's resolver can't resolve the host in url (e.g. because
    /etc/resolv.conf points to filtered public DNS), fall back to
    `getent hosts` which uses the full nsswitch chain (systemd-resolved,
    NSCD, /etc/hosts).  Monkey-patches socket.getaddrinfo for the duration.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        yield
        return

    try:
        socket.getaddrinfo(hostname, 443)
        yield
        return
    except socket.gaierror:
        pass

    try:
        result = subprocess.run(
            ["getent", "hosts", hostname],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            yield
            return
        ip = result.stdout.split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        yield
        return

    print(f"  [dns] {hostname} → {ip} (via getent fallback)")
    original_getaddrinfo = socket.getaddrinfo

    def patched(host, port, *args, **kwargs):
        if host == hostname:
            return original_getaddrinfo(ip, port, *args, **kwargs)
        return original_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


# ---------------------------------------------------------------------------
# Firefox cookie lookup
# ---------------------------------------------------------------------------

def _default_firefox_profile_dir() -> Path | None:
    root = Path.home() / ".mozilla" / "firefox"
    profiles_ini = root / "profiles.ini"
    if not profiles_ini.exists():
        return None

    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini)

    for section in cfg.sections():
        if section.startswith("Install") and cfg.has_option(section, "Default"):
            candidate = root / cfg.get(section, "Default")
            if candidate.exists():
                return candidate

    for section in cfg.sections():
        if not section.startswith("Profile"):
            continue
        if cfg.get(section, "Default", fallback="0") != "1":
            continue
        rel = cfg.get(section, "IsRelative", fallback="1") == "1"
        path = Path(cfg.get(section, "Path", fallback=""))
        candidate = root / path if rel else path
        if candidate.exists():
            return candidate

    dbs = sorted(root.glob("*/cookies.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    return dbs[0].parent if dbs else None


def load_overleaf_cookie(
    explicit_cookie: str | None = None,
    firefox_profile: str | None = None,
    cookie_db: str | None = None,
) -> str:
    """
    Resolve overleaf_session2, preferring:
      1. explicit_cookie argument
      2. OVERLEAF_COOKIE env var
      3. Firefox cookies.sqlite
    """
    if explicit_cookie:
        return explicit_cookie

    env_cookie = os.environ.get("OVERLEAF_COOKIE")
    if env_cookie:
        return env_cookie

    candidates: list[Path] = []
    if cookie_db:
        candidates.append(Path(cookie_db).expanduser())
    else:
        profile_dir = Path(firefox_profile).expanduser() if firefox_profile else _default_firefox_profile_dir()
        if profile_dir is not None:
            candidates.append(profile_dir / "cookies.sqlite")
        root = Path.home() / ".mozilla" / "firefox"
        candidates.extend(
            p for p in sorted(root.glob("*/cookies.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
            if p not in candidates
        )

    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        print(
            "Error: could not resolve overleaf_session2.\n"
            "Tried:\n"
            "  --cookie\n"
            "  $OVERLEAF_COOKIE\n"
            "  Firefox cookies.sqlite\n"
            "Pass --cookie explicitly or use --firefox-profile / --cookie-db."
        )
        sys.exit(1)

    best_row = None
    best_db = None
    for db_path in candidates:
        tmp_dir = Path(tempfile.mkdtemp(prefix="overleaf-cookie-"))
        tmp_db = tmp_dir / "cookies.sqlite"
        try:
            shutil.copy2(db_path, tmp_db)
            with sqlite3.connect(tmp_db) as conn:
                row = conn.execute(
                    """
                    SELECT value, lastAccessed
                    FROM moz_cookies
                    WHERE name = 'overleaf_session2'
                      AND (host = '.overleaf.com' OR host = 'www.overleaf.com')
                    ORDER BY lastAccessed DESC
                    LIMIT 1
                    """
                ).fetchone()
            if row and (best_row is None or row[1] > best_row[1]):
                best_row = row
                best_db = db_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if best_row and best_row[0]:
        return best_row[0]

    print(
        "Error: overleaf_session2 not found in Firefox cookie jars.\n"
        f"Checked: {', '.join(str(p) for p in candidates)}\n"
        "Pass --cookie explicitly if your active browser session is elsewhere."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# OT helpers
# ---------------------------------------------------------------------------

def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str, limit: int) -> int:
    n = min(len(a), len(b), limit)
    i = 0
    while i < n and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def _lis(seq: list[int]) -> list[int]:
    """Indices of a longest strictly increasing subsequence of seq."""
    import bisect
    tails: list[int] = []      # tails[k]: index in seq ending the best run of length k+1
    prev = [-1] * len(seq)
    for i, x in enumerate(seq):
        k = bisect.bisect_left(tails, x, key=lambda t: seq[t])
        if k > 0:
            prev[i] = tails[k - 1]
        if k == len(tails):
            tails.append(i)
        else:
            tails[k] = i
    out = []
    i = tails[-1] if tails else -1
    while i >= 0:
        out.append(i)
        i = prev[i]
    return out[::-1]


def _line_hunks(A: list[str], B: list[str], depth: int = 0) -> list[tuple[int, int, int, int]]:
    """
    Patience diff on lines: anchor on lines that occur exactly once on both
    sides, keep the longest in-order chain of anchors, and recurse into the
    gaps.  Returns (a1, a2, b1, b2) hunks, ascending.  Unlike one coarse block
    from the first to the last change, edits far apart stay separate hunks, so
    a concurrent edit between them is not dragged to a block edge.
    """
    hunks: list[tuple[int, int, int, int]] = []

    def go(a1: int, a2: int, b1: int, b2: int, depth: int) -> None:
        while a1 < a2 and b1 < b2 and A[a1] == B[b1]:
            a1 += 1; b1 += 1
        while a1 < a2 and b1 < b2 and A[a2 - 1] == B[b2 - 1]:
            a2 -= 1; b2 -= 1
        if a1 == a2 and b1 == b2:
            return
        if a1 == a2 or b1 == b2 or depth > 40:
            hunks.append((a1, a2, b1, b2))
            return
        count_a: dict[str, int] = {}
        for i in range(a1, a2):
            count_a[A[i]] = count_a.get(A[i], 0) + 1
        count_b: dict[str, int] = {}
        pos_b: dict[str, int] = {}
        for j in range(b1, b2):
            count_b[B[j]] = count_b.get(B[j], 0) + 1
            pos_b[B[j]] = j
        pairs = [(i, pos_b[A[i]]) for i in range(a1, a2)
                 if count_a[A[i]] == 1 and count_b.get(A[i]) == 1]
        if not pairs:
            hunks.append((a1, a2, b1, b2))
            return
        chain = [pairs[k] for k in _lis([p[1] for p in pairs])]
        ci, cj = a1, b1
        for i, j in chain:
            go(ci, i, cj, j, depth + 1)
            ci, cj = i + 1, j + 1
        go(ci, a2, cj, b2, depth + 1)

    go(0, len(A), 0, len(B), depth)
    return hunks


def compute_ot_ops(old: str, new: str) -> list[dict]:
    """
    Diff old → new and return a list of ShareJS ops in reverse position order
    so they can be applied sequentially without shifting offsets.

    Separate changes become separate ops (line-level patience diff, then
    char-level trimming inside each hunk): the ops of one hunk never cover the
    unchanged text between it and the next, so a concurrent remote edit there
    keeps its place when the ops are transformed.
    """
    if old == new:
        return []

    prefix = _common_prefix(old, new)
    suffix = _common_suffix(old[prefix:], new[prefix:], min(len(old), len(new)) - prefix)
    old_end, new_end = len(old) - suffix, len(new) - suffix
    old_mid, new_mid = old[prefix:old_end], new[prefix:new_end]

    # Hunks as (old_start, old_text, new_text), ascending, in old coordinates.
    spans: list[tuple[int, str, str]] = []
    if "\n" in old_mid or "\n" in new_mid:
        A = old_mid.splitlines(keepends=True)
        B = new_mid.splitlines(keepends=True)
        offs = [0]
        for line in A:
            offs.append(offs[-1] + len(line))
        boffs = [0]
        for line in B:
            boffs.append(boffs[-1] + len(line))
        for a1, a2, b1, b2 in _line_hunks(A, B):
            o, n = old_mid[offs[a1]:offs[a2]], new_mid[boffs[b1]:boffs[b2]]
            p = _common_prefix(o, n)
            q = _common_suffix(o[p:], n[p:], min(len(o), len(n)) - p)
            o_t, n_t = o[p:len(o) - q], n[p:len(n) - q]
            if o_t or n_t:
                spans.append((prefix + offs[a1] + p, o_t, n_t))
    else:
        spans.append((prefix, old_mid, new_mid))

    # Within one hunk the delete must run before the insert: if the insert ran
    # first, the cursor would land on the new text and the delete would remove
    # it instead of the old text.  Built as [insert, delete] per hunk, then
    # reversed so positions descend and delete precedes insert.
    ops: list[dict] = []
    for start, o_t, n_t in spans:
        if n_t:
            ops.append({"p": start, "i": n_t})
        if o_t:
            ops.append({"p": start, "d": o_t})
    return list(reversed(ops))


def decode_doc_line(line: str) -> str:
    """
    Undo the line encoding applied by Overleaf's joinDoc response.

    The real-time server sends each doc line as `unescape(encodeURIComponent(line))`,
    i.e. its UTF-8 bytes packed one per char (the browser reverses it with
    `decodeURIComponent(escape(line))`).  Without this, "’" arrives as "â\\x80\\x99".
    otUpdateApplied ops are NOT encoded this way, only joinDoc lines.
    """
    try:
        return line.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Already a proper unicode string (char > U+00FF, or not valid UTF-8 bytes).
        return line


def apply_sharejs_ops(content: str, ops: list[dict]) -> str:
    """
    Apply a list of ShareJS ops received from the server to a local string.
    Op format: {"p": position, "i": inserted_text} or {"p": position, "d": deleted_text}
    """
    for op in ops:
        p = op["p"]
        if "i" in op:
            content = content[:p] + op["i"] + content[p:]
        elif "d" in op:
            content = content[:p] + content[p + len(op["d"]):]
    return content


# Overleaf (ShareJS in JS) counts positions in UTF-16 code units; Python str
# indexes code points.  They differ after any char outside the BMP (emoji,
# some math symbols).  compute_ot_ops / apply_sharejs_ops work in code points;
# the two helpers below convert at the wire boundary.

def _cp_to_utf16(content: str, p: int) -> int:
    if content.isascii():
        return p
    return len(content[:p].encode("utf-16-le", "surrogatepass")) // 2


def _utf16_to_cp(content: str, p16: int) -> int:
    if content.isascii():
        return p16
    units = content.encode("utf-16-le", "surrogatepass")[: 2 * p16]
    return len(units.decode("utf-16-le", "surrogatepass"))


def ops_to_wire(old_content: str, ops: list[dict]) -> list[dict]:
    """
    Convert ops from compute_ot_ops(old_content, ...) to UTF-16 positions.
    Valid because ops are in descending position order: when each op runs on
    the server, the text before its position is still old_content[:p].
    """
    return [{**op, "p": _cp_to_utf16(old_content, op["p"])} for op in ops]


_NON_BMP = re.compile("[\U00010000-\U0010FFFF]")


def sanitize_non_bmp(content: str) -> str:
    """
    Replace each non-BMP char with two U+FFFD, which is what Overleaf stores
    for it (verified live: emoji sent as JSON escapes or raw UTF-8 both come
    back as U+FFFD U+FFFD).  Same UTF-16 length, so positions are unaffected.
    """
    return _NON_BMP.sub("��", content)


def apply_wire_ops(content: str, ops: list[dict]) -> str:
    """Apply server ops (UTF-16 positions) sequentially to a local string."""
    return apply_sharejs_ops(content, wire_to_cp(content, ops))


def wire_to_cp(content: str, ops: list[dict]) -> list[dict]:
    """Convert server ops (UTF-16 positions, applied sequentially) to code points."""
    out = []
    for op in ops:
        cp_op = {**op, "p": _utf16_to_cp(content, op["p"])}
        out.append(cp_op)
        content = apply_sharejs_ops(content, [cp_op])
    return out


# OT transform for ShareJS text ops (port of share/lib/types/text.js).  An op
# is a list of {p,i}/{p,d} components applied sequentially.  Our ops are the
# "left" side, as on the Overleaf server: concurrent inserts at the same
# position put ours first.

def _transform_pos(pos: int, c: dict, insert_after: bool = False) -> int:
    if "i" in c:
        if c["p"] < pos or (c["p"] == pos and insert_after):
            return pos + len(c["i"])
        return pos
    if pos <= c["p"]:
        return pos
    if pos <= c["p"] + len(c["d"]):
        return c["p"]
    return pos - len(c["d"])


def _transform_component(dest: list[dict], c: dict, other: dict, side: str) -> None:
    if "i" in c:
        if c["i"]:
            dest.append({"p": _transform_pos(c["p"], other, side == "right"), "i": c["i"]})
        return
    if "i" in other:
        s = c["d"]
        if c["p"] < other["p"]:
            dest.append({"p": c["p"], "d": s[: other["p"] - c["p"]]})
            s = s[other["p"] - c["p"]:]
        if s:
            dest.append({"p": c["p"] + len(other["i"]), "d": s})
        return
    # delete vs delete
    if c["p"] >= other["p"] + len(other["d"]):
        dest.append({"p": c["p"] - len(other["d"]), "d": c["d"]})
    elif c["p"] + len(c["d"]) <= other["p"]:
        dest.append(c)
    else:
        d = ""
        if c["p"] < other["p"]:
            d = c["d"][: other["p"] - c["p"]]
        if c["p"] + len(c["d"]) > other["p"] + len(other["d"]):
            d += c["d"][other["p"] + len(other["d"]) - c["p"]:]
        if d:
            dest.append({"p": _transform_pos(c["p"], other), "d": d})


def transform_x(left: list[dict], right: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Given ops `left` and `right` both applicable to the same document, return
    (left', right') such that apply(apply(doc, left), right') ==
    apply(apply(doc, right), left').
    """
    new_right: list[dict] = []
    for rc in right:
        new_left: list[dict] = []
        k = 0
        cur: dict | None = rc
        while k < len(left):
            next_c: list[dict] = []
            _transform_component(new_left, left[k], cur, "left")
            _transform_component(next_c, cur, left[k], "right")
            k += 1
            if len(next_c) == 1:
                cur = next_c[0]
            elif not next_c:
                new_left.extend(left[k:])
                cur = None
                break
            else:
                l2, r2 = transform_x(left[k:], next_c)
                new_left.extend(l2)
                new_right.extend(r2)
                cur = None
                break
        if cur is not None:
            new_right.append(cur)
        left = new_left
    return left, new_right


MIN_EVIDENCE_CHARS = 8
MAX_DOC_LENGTH = 2 * 1024 * 1024    # Overleaf's per-document limit (UTF-16 units)


def inflight_already_applied(inflight: dict, server_content: str, server_version: int) -> bool:
    """
    After a reconnect, did the server already apply our un-echoed op?

    The server can apply it and the connection drop before the echo reaches
    us.  Re-pushing then duplicates the text on the server, so the merge must
    start from the post-op content instead.  Evidence required: the server
    moved past the version we sent at, and every component of the op shows
    up in the server's text (an inserted string occurs more often than in the
    pre-op text, a deleted one less often).  Tiny components are ambiguous
    (they occur everywhere), so they never count as evidence.
    """
    v_sent = inflight.get("v")
    if v_sent is not None and server_version <= v_sent:
        return False
    base = inflight["base"]
    evidence = False
    for op in inflight["ops"]:
        if "i" in op:
            text, more = op["i"], server_content.count(op["i"]) > base.count(op["i"])
        else:
            text, more = op["d"], server_content.count(op["d"]) < base.count(op["d"])
        if len(text) < MIN_EVIDENCE_CHARS:
            continue
        if not more:
            return False
        evidence = True
    return evidence


def merge3(base: str, mine: str, theirs: str) -> str:
    """3-way merge: apply both base→mine and base→theirs edits."""
    mine_ops = compute_ot_ops(base, mine)
    theirs_ops = compute_ot_ops(base, theirs)
    _, theirs_t = transform_x(mine_ops, theirs_ops)
    return apply_sharejs_ops(mine, theirs_t)


# ---------------------------------------------------------------------------
# Socket.io client with event handler support
# ---------------------------------------------------------------------------

class TrackingSocketIOClient:
    """
    Thin wrapper around SocketIOClient that adds named event handler
    registration and suppresses the default noisy logging.
    """

    def __init__(self, session, project_id: str):
        sys.path.insert(0, str(Path(__file__).parent))
        from overleaf_cli import SocketIOClient as _Base
        self._base = _Base(session, project_id)
        self._handlers: dict[str, callable] = {}
        # Called when a handler raises: our in-memory state may now differ
        # from the server's, so the owner must resync before pushing again.
        self.on_handler_error: callable | None = None
        self._orig_on_close = self._base._on_close
        # Override the base _handle_event to route through our handlers
        self._base._handle_event = self._handle_event
        self._base._on_close = self._on_close

    def on(self, event_name: str, handler: callable):
        self._handlers[event_name] = handler

    def _handle_event(self, name: str, args: list):
        if name in self._handlers:
            try:
                self._handlers[name](args)
            except Exception as e:
                print(f"  [ws] handler error for {name}: {e}")
                if self.on_handler_error:
                    self.on_handler_error(name, e)

    def _on_close(self, ws, code, msg):
        self._base._connected.clear()
        self._orig_on_close(ws, code, msg)

    # Delegate everything else to the base client
    def connect(self): return self._base.connect()
    def run_forever(self): return self._base.run_forever()

    def join_project(self) -> dict:
        """
        Send joinProject and collect the response.
        Overleaf may respond via an ack (type 6) OR a joinProjectResponse
        event (type 5); handle whichever arrives first.
        """
        result = {}
        done = threading.Event()

        def on_event(args):
            # event path: args[0] = {"publicId": ..., "project": {rootFolder, ...}}
            if args and isinstance(args[0], dict):
                if "publicId" in args[0]:
                    result["_public_id"] = args[0]["publicId"]
                result.update(args[0].get("project", args[0]))
            done.set()

        def on_ack(data):
            # ack path: data = [null, {rootFolder, ...}]
            if data and len(data) > 1 and data[1]:
                result.update(data[1])
            done.set()

        self._handlers["joinProjectResponse"] = on_event
        self._base._connected.wait(timeout=10)
        self._base.send_event(
            "joinProject", [{"project_id": self._base.project_id}], callback=on_ack
        )
        done.wait(timeout=10)
        self._handlers.pop("joinProjectResponse", None)
        return result

    def join_doc(self, doc_id):
        lines, version = self._base.join_doc(doc_id)
        return [decode_doc_line(line) for line in lines], version

    def leave_doc(self, doc_id): return self._base.leave_doc(doc_id)
    def send_event(self, *a, **kw): return self._base.send_event(*a, **kw)
    def disconnect(self): return self._base.disconnect()
    def is_connected(self): return self._base._running and self._base._connected.is_set()


# ---------------------------------------------------------------------------
# Project doc-tree helpers
# ---------------------------------------------------------------------------

def is_safe_relpath(rel: str) -> bool:
    """
    A doc path comes from the server (any collaborator can name a doc), so it
    must never be able to point outside the sync directory.
    """
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if any(p.startswith(".overleaf-sync-backup") or p.endswith(".sync-tmp") for p in parts):
        return False
    return len(rel.encode("utf-8", "surrogatepass")) < 4096 and all(
        len(p.encode("utf-8", "surrogatepass")) <= 255 for p in parts
    )


def safe_join(root: Path, rel: str) -> Path:
    """root / rel, refusing anything that resolves (symlinks included) outside root."""
    if not is_safe_relpath(rel):
        raise ValueError(f"unsafe document path {rel!r}")
    real_root = os.path.realpath(root)
    target = root / rel
    if os.path.commonpath([real_root, os.path.realpath(target)]) != real_root:
        raise ValueError(f"{rel!r} resolves outside {root}")
    return target


def filter_safe_docs(docs: dict[str, str]) -> dict[str, str]:
    """Drop docs with unsafe paths or paths that collide on case-insensitive disks."""
    out: dict[str, str] = {}
    seen: dict[str, str] = {}
    for doc_id, rel in sorted(docs.items(), key=lambda x: x[1]):
        if not is_safe_relpath(rel):
            print(f"  [sync] ignoring document with unsafe path {rel!r}", flush=True)
            continue
        key = rel.casefold()
        if key in seen:
            print(f"  [sync] ignoring {rel!r}: path collides with {seen[key]!r} "
                  f"on case-insensitive filesystems", flush=True)
            continue
        seen[key] = rel
        out[doc_id] = rel
    return out


def collect_docs_from_project(project_data: dict) -> dict[str, str]:
    """
    Extract {doc_id: pathname} by walking the rootFolder tree returned by
    the joinProject WebSocket event. Works for any project regardless of
    edit history depth.
    """
    def _recurse(folder: dict, prefix: str) -> dict[str, str]:
        out = {}
        for doc in folder.get("docs", []):
            out[doc["_id"]] = prefix + doc["name"]
        for sub in folder.get("folders", []):
            out.update(_recurse(sub, prefix + sub["name"] + "/"))
        return out

    docs: dict[str, str] = {}
    for root in project_data.get("rootFolder", []):
        docs.update(_recurse(root, ""))
    return docs


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

import time as _time


def atomic_write(path: Path, text: str) -> None:
    """Write via temp file + rename so readers never see a truncated file."""
    tmp = path.with_name(f".{path.name}.sync-tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        shutil.copymode(path, tmp)
    except FileNotFoundError:
        pass
    os.replace(tmp, path)


def _exchange(a: Path, b: Path) -> bool:
    """Atomically swap two paths (Linux renameat2 RENAME_EXCHANGE). False if unsupported."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        AT_FDCWD, RENAME_EXCHANGE = -100, 2
        return libc.renameat2(AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b), RENAME_EXCHANGE) == 0
    except (OSError, AttributeError):
        return False


def _sig(path: Path):
    st = os.stat(path)
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def update_file(path: Path, fn, default: str | None = None) -> tuple[str, str]:
    """
    Replace `path` with fn(current text) without ever discarding a concurrent
    write by an editor.  Reading the file, computing, then renaming leaves a
    window in which an editor save would be silently overwritten.  Instead the
    new file is swapped in atomically (RENAME_EXCHANGE), which hands back
    exactly what was in `path`.  If that is not what fn saw, the editor wrote
    in between: the swap is undone at once (so the editor's text is back in
    place within microseconds, not after a whole merge) and the merge is
    redone on top of what it wrote.

    fn must be pure.  `default` stands in for a missing file.  Returns
    (text now in the file, text of the file fn last merged).  Limitation: an
    editor that keeps the old file descriptor open across saves writes into
    the replaced inode; no rename-based writer can see that.
    """
    tmp = path.with_name(f".{path.name}.sync-tmp")

    def snapshot():
        for _ in range(20):
            try:
                before = _sig(path)
                text = path.read_text(encoding="utf-8")
                if _sig(path) == before:
                    return text, before
            except FileNotFoundError:
                if default is None:
                    raise
                return default, None
        raise RuntimeError(f"{path.name} kept changing while merging")

    basis, sig0 = snapshot()
    for _ in range(50):
        new = fn(basis)
        if sig0 is not None and new == basis:
            return new, basis
        tmp.write_text(new, encoding="utf-8")
        if sig0 is None:                       # file missing: just create it
            os.replace(tmp, path)
            return new, basis
        shutil.copymode(path, tmp)
        if not _exchange(tmp, path):
            # No atomic exchange (non-Linux): best effort, re-check then replace.
            if path.read_text(encoding="utf-8") == basis:
                os.replace(tmp, path)
                return new, basis
            tmp.unlink(missing_ok=True)
            basis, sig0 = snapshot()
            continue
        # tmp now holds what was in `path`
        changed = _sig(tmp) != sig0
        if not changed:
            changed = tmp.read_text(encoding="utf-8") != basis
        if not changed:
            tmp.unlink()
            return new, basis
        _exchange(tmp, path)                   # put the editor's file back at once
        tmp.unlink(missing_ok=True)
        basis, sig0 = snapshot()
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"{path.name} kept changing while merging")


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _ops_summary(ops: list[dict]) -> str:
    inserted = sum(len(op["i"]) for op in ops if "i" in op)
    deleted  = sum(len(op["d"]) for op in ops if "d" in op)
    parts = []
    if inserted:
        parts.append(f"+{inserted}ch")
    if deleted:
        parts.append(f"-{deleted}ch")
    return ", ".join(parts) if parts else "no-op"


# ---------------------------------------------------------------------------
# Listen mode
# ---------------------------------------------------------------------------

class OverleafListener:
    """
    Passive sync: joins every live document in a project, writes initial
    content to disk, then applies incoming OT updates in real time.

    No model, no editing — purely Overleaf → local filesystem.
    """

    def __init__(self, project_id: str, session_cookie: str, output_dir: str):
        self.project_id = project_id
        self.session_cookie = session_cookie
        self.output_dir = Path(output_dir)

        self._lock = threading.Lock()
        self._docs: dict[str, dict] = {}  # doc_id → {content, version, path}
        self._ws: TrackingSocketIOClient | None = None

    def _on_ot_update(self, args: list):
        payload = args[0] if args else {}
        doc_id = payload.get("doc")
        v = payload.get("v")
        ops = payload.get("op", [])

        with self._lock:
            if doc_id not in self._docs:
                return
            state = self._docs[doc_id]
            if ops:
                state["content"] = apply_wire_ops(state["content"], ops)
            if v is not None:
                state["version"] = v + 1
            out_path = safe_join(self.output_dir, state["path"])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(state["content"], encoding="utf-8")
            label = (state["path"], state["version"], _ops_summary(ops))

        print(f"  {_ts()} overleaf→local  {label[0]}  v{label[1]}  ({label[2]})")

    def run(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from overleaf_cli import OverleafSession

        s = OverleafSession(self.session_cookie)
        s.get_project_bootstrap(self.project_id)

        # Connect WebSocket — joinProject returns the full doc tree
        self._ws = TrackingSocketIOClient(s, self.project_id)
        self._ws.on("otUpdateApplied", self._on_ot_update)
        self._ws.connect()
        self._ws.run_forever()
        project_data = self._ws.join_project()
        docs = filter_safe_docs(collect_docs_from_project(project_data))

        if not docs:
            print("Error: no documents found in project.")
            sys.exit(1)

        # Join each doc and write initial content to disk
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nSyncing {len(docs)} document(s) → {self.output_dir}/\n")
        for doc_id, pathname in sorted(docs.items(), key=lambda x: x[1]):
            lines, version = self._ws.join_doc(doc_id)
            content = "\n".join(lines)
            with self._lock:
                self._docs[doc_id] = {"content": content, "version": version, "path": pathname}
            out_path = safe_join(self.output_dir, pathname)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            print(f"  {pathname:<45} v{version}  ({len(content)} chars)")

        print(f"\nListening for changes... (Ctrl-C to stop)\n")

        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            for doc_id in list(self._docs.keys()):
                self._ws.leave_doc(doc_id)
            self._ws.disconnect()
            print("Done.")


# ---------------------------------------------------------------------------
# Bidirectional sync mode
# ---------------------------------------------------------------------------


class OverleafSync:
    """
    Bidirectional sync: Overleaf ↔ local folder.

    - Incoming OT updates from Overleaf → applied to memory + written to disk.
    - Local file changes (detected by polling every 0.5 s) → diffed against
      the last-known Overleaf state → pushed as OT ops via applyOtUpdate.

    Concurrent edits: incoming ops are transformed past our in-flight op and
    unpushed local edits, then merged into the local file.  If an
    applyOtUpdate is rejected, we re-join the doc and 3-way merge the local
    file onto the server content.
    """

    POLL_INTERVAL = 0.5  # seconds between local file checks

    def __init__(self, project_id: str, session_cookie: str, output_dir: str,
                 dry_run: bool = False):
        self.project_id = project_id
        self.session_cookie = session_cookie
        self.output_dir = Path(output_dir)
        # dry_run: never send anything to Overleaf; only log what would be sent.
        self.dry_run = dry_run
        self._warned: set[str] = set()
        self._last_untracked_scan = 0.0

        self._lock = threading.Lock()
        # doc_id → {content, version, path}
        self._docs: dict[str, dict] = {}
        self._ws: TrackingSocketIOClient | None = None
        self._running = False
        self._ws_ready = threading.Event()
        self._ws_ready_at: float = 0.0   # monotonic time when last _ws_ready.set()
        self._watch_thread = None
        # Our socket.io publicId — used to distinguish our own op echoes from
        # genuine external ops that happen to share the same version number.
        self._public_id: str = ""
        # Set by the watch thread on an unrecoverable condition; run() exits 1.
        self._fatal: str | None = None
        # doc_id → (file content, first time seen with it); see _settled.
        self._candidates: dict[str, tuple[str, float]] = {}
        # doc_id → pre-op doc, for docs whose last update the server rejected
        self._pending_rejection: dict[str, str] = {}
        # doc_id → content the server refused; not pushed again unchanged
        self._rejected_content: dict[str, str] = {}
        self._started = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # -- incoming from Overleaf -----------------------------------------------

    def _on_ot_update(self, args: list):
        payload = args[0] if args else {}
        doc_id = payload.get("doc")
        v = payload.get("v")
        ops = payload.get("op", [])
        source = payload.get("meta", {}).get("source", "")

        with self._lock:
            if doc_id not in self._docs:
                return
            state = self._docs[doc_id]

            # Echo suppression: the server broadcasts our own ops back to us
            # (because we send dupIfSource:[]).  Detect them by the source field
            # in meta, which the server always sets to our publicId.
            #
            # The old version-number-based approach (pending_versions / v <
            # state["version"]) incorrectly suppressed *concurrent external ops*
            # that arrived with the same version number as our in-flight op.
            # That caused local state to diverge → wrong positions in the next
            # push → otUpdateError in the browser → out-of-sync modal.
            # In practice (verified live) the server strips our own echo down
            # to {"v", "doc"}: no op, no meta.  Other clients' updates always
            # carry an op, so a missing op also identifies our echo.
            #
            # This echo is also the real ack of our in-flight op (as in
            # Overleaf's own client): the applyOtUpdate callback only means
            # "queued", and other clients' ops the server applied before ours
            # can still arrive after it.
            if "op" not in payload or (self._public_id and source == self._public_id):
                inflight = state.get("inflight")
                if inflight and (v is None or v >= state["version"]):
                    del state["inflight"]
                    # A rejected op is announced by this same {v, doc} shape and
                    # only then by otUpdateError, so remember the pre-op doc.
                    state["pushed_from"] = inflight["base"]
                    inflight["done"].set()
                if v is not None and v + 1 > state["version"]:
                    state["version"] = v + 1
                return

            # state["content"] is the server doc at state["version"] with our
            # in-flight op (if any) applied; the file on disk is that plus the
            # user's not-yet-pushed edits.  Remote ops are relative to the
            # server doc, so transform them past both before applying, rather
            # than overwriting the file with the server content.
            if v is not None and v > state["version"]:
                # We missed updates: our copy no longer matches the server, so
                # anything pushed from it could land at the wrong position.
                raise RuntimeError(
                    f"{state['path']}: update gap (got v{v}, expected v{state['version']})"
                )
            inflight = state.get("inflight")
            base = inflight["base"] if inflight else state["content"]
            remote = wire_to_cp(base, ops)
            if inflight:
                inflight["base"] = apply_sharejs_ops(base, remote)
                inflight["ops"], remote = transform_x(inflight["ops"], remote)
            old_content = state["content"]
            state["content"] = apply_sharejs_ops(old_content, remote)
            if v is not None:
                state["version"] = v + 1

            out_path = safe_join(self.output_dir, state["path"])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            def merge_remote(local: str) -> str:
                if local == old_content:
                    return state["content"]
                local_ops = compute_ot_ops(old_content, local)
                _, remote_t = transform_x(local_ops, remote)
                return apply_sharejs_ops(local, remote_t)

            _, seen = update_file(out_path, merge_remote, default=old_content)
            note = "" if seen == old_content else ", merged with local edits"
            label = (state["path"], state["version"], _ops_summary(ops) + note)

        print(f"  {_ts()} overleaf→local  {label[0]}  v{label[1]}  ({label[2]})")

    def _on_ot_error(self, args: list):
        """
        The server rejected an update (e.g. "Update takes doc over max doc
        size").  It sends the same {v, doc} message as for an accepted op
        first, so by now we may have counted the op as applied.  Our idea of
        the server doc is wrong: keep the local file (backup + merge from the
        pre-op doc on reconnect), never push that content again as is, and
        reconnect to resync.
        """
        message = args[0] if args else "unknown error"
        info = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
        doc_id = info.get("doc_id")
        print(f"  {_ts()} [sync] server rejected an update: {message}", flush=True)
        with self._lock:
            state = self._docs.get(doc_id)
            if state is not None:
                inflight = state.get("inflight")
                self._pending_rejection[doc_id] = inflight["base"] if inflight else state.get("pushed_from", state["content"])
                self._rejected_content[doc_id] = state["content"]
                try:
                    local = safe_join(self.output_dir, state["path"]).read_text(encoding="utf-8")
                    self._backup_local(state["path"], local)
                except (OSError, ValueError, UnicodeDecodeError):
                    pass
        ws = self._ws
        if ws is not None:
            self._force_reconnect(ws)

    # -- outgoing to Overleaf -------------------------------------------------

    def _watch_files(self):
        # We deliberately use path-based polling rather than inotify here.
        # inotify watches inodes: tools like Claude Code (and many editors)
        # delete the file and create a new one on every save, so an inode-level
        # watch silently breaks after the first write.  Polling reads each
        # tracked file by its path on every tick, which is always correct
        # regardless of how the file was written.
        #
        # Path-based polling has one blind spot: if output_dir itself is
        # renamed (e.g. `mv dir dir.bak` then a fresh checkout at `dir`),
        # editors whose cwd was inside it keep writing to the renamed copy,
        # which we never read again.  Stop the sync when that happens: going
        # on would silently drop those edits, or push the new dir's content.
        dir_ino = self._dir_inode()
        while self._running:
            if self._dir_inode() != dir_ino:
                self._fatal = (
                    f"{self.output_dir} was moved, deleted or replaced while syncing.\n"
                    f"Edits made in the old directory (e.g. by an editor whose cwd was "
                    f"inside it) were NOT pushed to Overleaf.\n"
                    f"Restart the sync, and restart editors/shells that were inside it."
                )
                self._running = False
                return
            # While the WebSocket is down, skip pushing entirely.  The
            # reconnect path in _connect_and_sync_state already detects any
            # local edits accumulated during the outage and replays them once
            # the connection is restored — no need to spam the log or block
            # here waiting for the socket to come back.
            if not self._ws_ready.is_set():
                _time.sleep(self.POLL_INTERVAL)
                continue
            # Give the connection a few seconds to settle after (re)connect
            # before sending ops.  Pushing immediately can cause the server to
            # drop the socket if it hasn't finished its own setup, which
            # triggers another reconnect and creates an infinite loop.
            SETTLE_SECS = 3.0
            age = _time.monotonic() - self._ws_ready_at
            if age < SETTLE_SECS:
                _time.sleep(SETTLE_SECS - age)
                continue
            try:
                self._warn_untracked()
                for doc_id, old, new in self._settled(self._collect_local_changes()):
                    if self.dry_run:
                        self._warn_once(
                            f"dry:{doc_id}:{hash(new)}",
                            f"[dry-run] would push {self._docs[doc_id]['path']} "
                            f"({_ops_summary(compute_ot_ops(old, new))}); nothing sent",
                        )
                        continue
                    self._push_change(doc_id, old, new)
            except Exception as e:
                print(f"  {_ts()} [sync] poll error: {e}", flush=True)
            _time.sleep(self.POLL_INTERVAL)

    # A save is not atomic for many tools (truncate, then write): reading in
    # between yields a half-written or empty file, which would be pushed to
    # Overleaf as "delete everything".  So a change is only pushed once the
    # file has had the same content on two consecutive polls, and a change that
    # wipes out most of the document must hold for BIG_DELETE_SECS.
    BIG_DELETE_SECS = 5.0
    BIG_DELETE_MIN_CHARS = 200

    @classmethod
    def _is_big_delete(cls, old: str, new: str) -> bool:
        if not old.strip():
            return False
        if not new.strip():
            return True
        kept = len(new) / len(old)
        return len(old) >= cls.BIG_DELETE_MIN_CHARS and kept < 0.5

    def _settled(self, changes: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        """Keep only the changes whose file content has been stable long enough."""
        now = _time.monotonic()
        ready = []
        current = set()
        for doc_id, old, new in changes:
            current.add(doc_id)
            prev = self._candidates.get(doc_id)
            if prev is None or prev[0] != new:
                self._candidates[doc_id] = (new, now)
                continue
            if self._is_big_delete(old, new):
                if now - prev[1] < self.BIG_DELETE_SECS:
                    continue
                print(f"  {_ts()} [sync] large deletion in {self._docs[doc_id]['path']} "
                      f"held {self.BIG_DELETE_SECS:.0f}s, pushing", flush=True)
            ready.append((doc_id, old, new))
        for doc_id in list(self._candidates):
            if doc_id not in current:
                del self._candidates[doc_id]
        return ready

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"  {_ts()} [sync] {msg}", flush=True)

    def _warn_untracked(self) -> None:
        """Local files that are not Overleaf docs are never uploaded; say so."""
        now = _time.monotonic()
        if now - self._last_untracked_scan < 5.0:
            return
        self._last_untracked_scan = now
        with self._lock:
            tracked = {s["path"] for s in self._docs.values()}
        for p in self.output_dir.rglob("*"):
            if not p.is_file() or p.name.endswith(".sync-tmp"):
                continue
            rel = p.relative_to(self.output_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue  # backups, .git, editor temp files
            if rel.as_posix() not in tracked:
                self._warn_once(
                    f"untracked:{rel}",
                    f"{rel} is not an Overleaf document and will NOT be uploaded "
                    f"(create it in Overleaf, then restart or wait for the tree refresh)",
                )

    def _dir_inode(self) -> int | None:
        try:
            return self.output_dir.stat().st_ino
        except FileNotFoundError:
            return None

    def _collect_local_changes(self) -> list[tuple[str, str, str]]:
        """
        Return (doc_id, last_synced, local) for every file that differs from
        the last-known Overleaf state.  Non-BMP chars are rewritten on disk
        to what Overleaf will store (see sanitize_non_bmp) so the local file
        keeps matching Overleaf.
        """
        changed: list[tuple[str, str, str]] = []
        with self._lock:
            for doc_id, state in list(self._docs.items()):
                if state.get("inflight"):
                    continue  # push already running for this doc
                try:
                    out_path = safe_join(self.output_dir, state["path"])
                    fc = out_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except ValueError as e:
                    self._warn_once(f"unsafe:{state['path']}", f"not syncing {state['path']}: {e}")
                    continue
                if fc == state["content"]:
                    continue
                if fc == self._rejected_content.get(doc_id):
                    self._warn_once(
                        f"rejected:{doc_id}:{hash(fc)}",
                        f"{state['path']}: Overleaf rejected this content; not sending it again "
                        f"(change the file to retry). Your file is untouched.")
                    continue
                if len(fc.encode("utf-16-le", "surrogatepass")) // 2 > MAX_DOC_LENGTH:
                    self._warn_once(
                        f"toolong:{doc_id}:{hash(fc)}",
                        f"{state['path']} is longer than Overleaf's {MAX_DOC_LENGTH}-character "
                        f"document limit; not sending it. Your file is untouched.")
                    continue
                sanitized = sanitize_non_bmp(fc)
                if sanitized != fc:
                    atomic_write(out_path, sanitized)
                    print(
                        f"  {_ts()} [sync] {state['path']}: Overleaf cannot store chars outside "
                        f"the BMP (e.g. emoji); replaced them with U+FFFD U+FFFD locally",
                        flush=True,
                    )
                    fc = sanitized
                if fc != state["content"]:
                    changed.append((doc_id, state["content"], fc))
        return changed

    def _push_change(self, doc_id: str, old_content: str, new_content: str) -> bool:
        """
        Send old → new as ONE applyOtUpdate and wait for its echo.  Remote ops
        arriving meanwhile are transformed against it by _on_ot_update.
        """
        cp_ops = compute_ot_ops(old_content, new_content)
        if not cp_ops:
            return True
        done = threading.Event()
        err: list = [None]
        with self._lock:
            state = self._docs[doc_id]
            # Bail if a remote update already changed the baseline.
            if state["content"] != old_content or state.get("inflight"):
                return False
            ws = self._ws
            if not self._ws_ready.is_set() or ws is None or not ws.is_connected():
                self._ws_ready.clear()
                return False
            pathname = state["path"]
            v = state["version"]
            state["inflight"] = {"ops": cp_ops, "base": old_content, "done": done, "v": v}
            state["content"] = new_content

        ops = ops_to_wire(old_content, cp_ops)
        print(f"  {_ts()} local→overleaf  {pathname}  ({_ops_summary(ops)})", flush=True)

        def on_ack(data):
            if data and data[0] is not None:
                err[0] = data[0]
                done.set()

        update = {"doc": doc_id, "op": ops, "v": v, "dupIfSource": []}
        try:
            ws.send_event("applyOtUpdate", [doc_id, update], callback=on_ack)
        except Exception as e:
            print(f"  [sync] send failed for {pathname}: {e}", flush=True)
            self._ws_ready.clear()
            return False

        if done.wait(timeout=10) and err[0] is None:
            return True
        if not ws.is_connected():
            # Reconnect re-joins every doc and merges the local file.
            print(f"  {_ts()} [sync] connection lost mid-push for {pathname}, will retry on reconnect", flush=True)
            self._ws_ready.clear()
            return False
        print(f"  [sync] push of {pathname} failed ({err[0] or 'no echo'}), re-syncing from Overleaf...", flush=True)
        try:
            self._resync_doc(doc_id, ws)
        except Exception as e:
            # Can't trust our state: drop the socket so the reconnect path
            # re-joins every doc and merges (it keeps the in-flight base).
            print(f"  [sync] re-sync of {pathname} failed ({e}); reconnecting", flush=True)
            self._force_reconnect(ws)
        return False

    def _force_reconnect(self, ws) -> None:
        self._ws_ready.clear()
        try:
            ws.disconnect()
        except Exception:
            pass

    def _resync_doc(self, doc_id: str, ws) -> None:
        """Re-join doc_id and merge the local file onto the server content."""
        lines, new_v = ws.join_doc(doc_id)
        server_content = "\n".join(lines)
        with self._lock:
            state = self._docs[doc_id]
            inflight = state.pop("inflight", None)
            # Merge from before the failed op, so it is re-pushed if the
            # server lacks it.
            base = inflight["base"] if inflight else state["content"]
            if inflight and inflight_already_applied(inflight, server_content, new_v):
                base = state["content"]    # the server already has our op
            out_path = safe_join(self.output_dir, state["path"])
            previous_content = state["content"]
            state["content"] = server_content
            state["version"] = new_v
            update_file(out_path, lambda local: merge3(base, local, server_content),
                        default=previous_content)
        print(f"  [sync] re-synced to server v{new_v}, local edits kept", flush=True)

    def _disconnect_ws(self):
        self._ws_ready.clear()
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            for doc_id in list(self._docs.keys()):
                ws.leave_doc(doc_id)
        except Exception:
            pass
        try:
            ws.disconnect()
        except Exception:
            pass

    def _connect_and_sync_state(self, initial: bool):
        sys.path.insert(0, str(Path(__file__).parent))
        from overleaf_cli import OverleafSession

        session = OverleafSession(self.session_cookie)
        session.get_project_bootstrap(self.project_id)

        ws = TrackingSocketIOClient(session, self.project_id)
        ws.on("otUpdateApplied", self._on_ot_update)
        ws.on("otUpdateError", self._on_ot_error)
        ws.on_handler_error = lambda name, exc: self._force_reconnect(ws)

        def on_tree_change(args, _name=None):
            # Added / renamed / moved / removed entity: re-join the project so
            # the doc tree is rebuilt (the reconnect path merges safely).
            print(f"  {_ts()} [sync] project tree changed; refreshing", flush=True)
            self._force_reconnect(ws)

        for ev in ("reciveNewDoc", "reciveNewFile", "reciveNewFolder",
                   "reciveEntityRename", "reciveEntityMove", "removeEntity"):
            ws.on(ev, on_tree_change)
        ws.connect()
        ws.run_forever()
        try:
            self._sync_all_docs(ws, initial)
        except BaseException:
            try:
                ws.disconnect()
            except Exception:
                pass
            raise
        self._ws = ws
        self._ws_ready_at = _time.monotonic()
        self._ws_ready.set()

    def _backup_local(self, pathname: str, content: str) -> None:
        """Keep a copy of a local file we are about to overwrite with the server's."""
        dest = self.output_dir / ".overleaf-sync-backup" / self._started / pathname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        print(f"  {_ts()} [sync] local {pathname} differed from Overleaf; "
              f"backup in {dest}", flush=True)

    def _sync_all_docs(self, ws, initial: bool) -> None:
        project_data = ws.join_project()
        if not project_data:
            raise RuntimeError("joinProject returned nothing")
        self._public_id = project_data.pop("_public_id", None) or ""
        docs = filter_safe_docs(collect_docs_from_project(project_data))
        if not docs:
            raise RuntimeError("no documents found in project")

        self.output_dir.mkdir(parents=True, exist_ok=True)

        for doc_id, rel in list(docs.items()):
            try:
                safe_join(self.output_dir, rel)
            except ValueError as e:
                print(f"  {_ts()} [sync] ignoring {rel}: {e}", flush=True)
                del docs[doc_id]

        with self._lock:
            for gone in [d for d in self._docs if d not in docs]:
                path = self._docs.pop(gone)["path"]
                print(f"  {_ts()} [sync] {path} no longer exists in Overleaf; "
                      f"no longer syncing it (local copy left untouched)", flush=True)

        for doc_id, pathname in sorted(docs.items(), key=lambda x: x[1]):
            lines, version = ws.join_doc(doc_id)
            server_content = "\n".join(lines)
            out_path = safe_join(self.output_dir, pathname)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                # Save the pre-disconnect content BEFORE overwriting it with
                # server_content — we need it to detect stale local files.
                old_state = self._docs.get(doc_id, {})
                old_content = old_state.get("content", None)
                # An unacked op may not have reached the server: merge from
                # before it so it is re-pushed rather than silently dropped.
                inflight = old_state.get("inflight")
                if doc_id in self._pending_rejection:
                    # The server refused our last update: merge from the doc as
                    # it was before it, and never treat the local file as stale.
                    inflight = {"base": self._pending_rejection.pop(doc_id), "ops": []}
                merge_base = inflight["base"] if inflight else old_content
                if inflight and inflight_already_applied(inflight, server_content, version):
                    merge_base = old_content    # the server already has our op

                self._docs[doc_id] = {
                    "content": server_content,
                    "version": version,
                    "path": pathname,
                }
            old_path = old_state.get("path")
            if not initial and old_path and old_path != pathname:
                # Renamed / moved in Overleaf: carry the local file (and any
                # edits pending in it) along, so they merge into the new doc
                # instead of being stranded at a path that is no longer synced.
                try:
                    old_file = safe_join(self.output_dir, old_path)
                except ValueError:
                    old_file = None
                if old_file is not None and old_file.is_file() and not out_path.exists():
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old_file, out_path)
                    print(f"  {_ts()} [sync] {old_path} was renamed in Overleaf → {pathname}", flush=True)

            if not initial and out_path.exists():
                branch: list[str] = []

                def reconcile(local_content: str) -> str:
                    branch.clear()
                    if local_content == server_content:
                        return server_content
                    if old_content is not None and local_content == old_content and not inflight:
                        # Local file is stale — identical to the last synced state
                        # before the disconnect.  Overwrite with the latest server
                        # content; do NOT push old content to Overleaf.
                        branch.append("stale")
                        return server_content
                    # Local has genuine edits relative to the pre-disconnect
                    # state (or old_content is unknown).  Merge them on top of
                    # the server content so no edits are lost.  In-memory state
                    # stays server_content, so the poll loop diffs
                    # (server → merged) and pushes to Overleaf.
                    branch.append("merge")
                    return merge3(merge_base or "", local_content, server_content)

                _, seen = update_file(out_path, reconcile)
                if seen != server_content:
                    if branch == ["stale"]:
                        print(f"  {_ts()} [sync] stale local {pathname} → updated to v{version}", flush=True)
                    else:
                        print(f"  {_ts()} [sync] local edit pending on {pathname} — merged", flush=True)
                    continue
            if initial and out_path.exists():
                try:
                    previous = out_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    previous = None
                if previous is not None and previous != server_content:
                    self._backup_local(pathname, previous)
            atomic_write(out_path, server_content)
            if initial:
                print(f"  {pathname:<45} v{version}  ({len(server_content)} chars)")

    # -- startup --------------------------------------------------------------

    def run(self):
        self._running = True
        try:
            self._connect_and_sync_state(initial=True)
        except Exception as e:
            print(f"Error: initial sync failed: {e}")
            sys.exit(1)

        print(f"\nBidirectional sync active... (Ctrl-C to stop)\n")
        self._watch_thread = threading.Thread(target=self._watch_files, daemon=True)
        self._watch_thread.start()

        try:
            backoff = 2.0
            while self._running:
                _time.sleep(1)
                ws = self._ws
                if ws is not None and ws.is_connected():
                    backoff = 2.0  # reset on stable connection
                    continue
                print(f"  {_ts()} [sync] disconnected, reconnecting in {backoff:.0f}s...", flush=True)
                _time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                self._disconnect_ws()
                retry = 2.0
                while self._running:
                    try:
                        self._connect_and_sync_state(initial=False)
                        print(f"  {_ts()} [sync] reconnected", flush=True)
                        break
                    except Exception as e:
                        # Back off: hammering a rate-limiting server (HTTP 429)
                        # or a down one only makes the outage longer.
                        print(f"  {_ts()} [sync] reconnect failed: {e} — retrying in {retry:.0f}s", flush=True)
                        _time.sleep(retry)
                        retry = min(retry * 2, 30.0)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self._running = False
            self._disconnect_ws()
            print("Done.")
        if self._fatal:
            print(f"\nError: {self._fatal}", file=sys.stderr, flush=True)
            sys.exit(1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Live Overleaf ↔ local sync and model editing REPL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              # zero-arg sync from a git repo with an Overleaf remote
              python3 overleaf_agent.py --sync

              # interactive file picker → REPL
              python3 overleaf_agent.py 69a0589216d96e98bc76b8c6 \\
                  --cookie "s:abc123..."

              # skip file picker
              python3 overleaf_agent.py 69a0589216d96e98bc76b8c6 \\
                  --cookie "s:abc123..." --doc-id 69a81e69bdcdae63f5c4be41

              # passive mirror: Overleaf → local
              python3 overleaf_agent.py 69a0589216d96e98bc76b8c6 \\
                  --cookie "s:abc123..." --listen

              # bidirectional sync: Overleaf ↔ local
              python3 overleaf_agent.py 69a0589216d96e98bc76b8c6 \\
                  --cookie "s:abc123..." --sync
        """),
    )
    parser.add_argument("project_id", nargs="?", default=None,
                        help="Overleaf project ID (from the URL); auto-detected from git remote if omitted)")
    parser.add_argument("--cookie", default=None,
                        help="overleaf_session2 cookie value (overrides Firefox/default lookup)")
    parser.add_argument("--firefox-profile", default=None,
                        help="Firefox profile directory to read cookies.sqlite from")
    parser.add_argument("--cookie-db", default=None,
                        help="Path to a specific Firefox cookies.sqlite database")
    parser.add_argument("--listen", action="store_true",
                        help="Passive mode: Overleaf → local folder, read-only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Sync Overleaf → local but never send local edits; log what would be sent")
    parser.add_argument("--output-dir", default=None,
                        help="Local folder (default: current working directory)")

    args = parser.parse_args()

    if args.project_id is None:
        import re as _re
        import subprocess as _sp
        try:
            remotes_out = _sp.check_output(
                ["git", "remote", "-v"], stderr=_sp.PIPE, text=True
            )
        except Exception as exc:
            parser.error(f"No project_id given and 'git remote -v' failed: {exc}")
        m = _re.search(r"overleaf\.com/([a-f0-9]{24})", remotes_out)
        if not m:
            parser.error(
                f"No project_id given and no Overleaf remote found.\n"
                f"git remotes:\n{remotes_out.strip() or '  (none)'}\n"
                f"Add one with: git remote add origin https://git.overleaf.com/<project_id>"
            )
        args.project_id = m.group(1)
        remote_url = _re.search(r"\S+overleaf\S+", remotes_out).group(0)  # type: ignore[union-attr]
        print(f"[auto] project_id={args.project_id} (from git remote {remote_url})", flush=True)

    if args.output_dir is None:
        args.output_dir = os.path.join(os.getcwd(), args.project_id)

    session_cookie = load_overleaf_cookie(
        explicit_cookie=args.cookie,
        firefox_profile=args.firefox_profile,
        cookie_db=args.cookie_db,
    )

    if args.listen:
        OverleafListener(
            project_id=args.project_id,
            session_cookie=session_cookie,
            output_dir=args.output_dir,
        ).run()
    else:
        OverleafSync(
            project_id=args.project_id,
            session_cookie=session_cookie,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        ).run()


if __name__ == "__main__":
    main()
