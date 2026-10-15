#!/usr/bin/env python3
"""
overleaf-sync — live two-way sync between an Overleaf project and a local folder
================================================================================

Works with overleaf.com and with self-hosted Overleaf (Community Edition,
Server Pro, institutional installs).  Any editor or coding agent that reads and
writes the files can take part: changes are detected by polling the files, and
Overleaf's real-time (ShareJS / operational transformation) protocol is spoken
directly, so a coauthor typing in the browser is merged, not overwritten.

    overleaf-sync <project_id|profile> [--url URL] [--output-dir DIR]
    overleaf-sync --check <project_id|profile>     # read-only diagnostics
    overleaf-sync <project_id> --dry-run           # pull only, log what would be sent

Settings come from, in increasing priority: defaults, environment
(OVERLEAF_URL, OVERLEAF_COOKIE_NAME, OVERLEAF_COOKIE), a TOML config file
(./.overleaf-sync.toml or ~/.config/overleaf-sync/config.toml), then flags.

Authentication (first match wins): --cookie, --cookie-file / cookie_file,
$OVERLEAF_COOKIE, --login-email with a password from --password-file or
$OVERLEAF_PASSWORD (self-hosted only), the Firefox cookie jar.
"""

import argparse
import configparser
import datetime
import difflib
import json
import shlex
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
import time as _time_mod
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


def read_cookie_file(path: str) -> str:
    """Read the cookie from a file (keeps it out of `ps` and shell history)."""
    p = Path(path).expanduser()
    if p.stat().st_mode & 0o077:
        print(f"Warning: {p} is readable by other users; run chmod 600 {p}", file=sys.stderr)
    value = p.read_text(encoding="utf-8").strip()
    if not value:
        print(f"Error: {p} is empty", file=sys.stderr)
        sys.exit(1)
    return value


COOKIE_EXPIRY: float | None = None     # epoch seconds, when the cookie came from Firefox


def describe_cookie_expiry(expiry: float | None, now: float | None = None) -> tuple[str, bool]:
    """(text, urgent) for the saved expiry of the session cookie."""
    if not expiry:
        return "unknown (session cookie, or not read from Firefox)", False
    left = expiry - (now if now is not None else _time_mod.time())
    if left <= 0:
        return "already expired as saved in Firefox (log in again if the server rejects it)", True
    if left < 86400:
        return f"expires in {left / 3600:.1f} h as saved in Firefox", True
    return f"expires in {left / 86400:.1f} days as saved in Firefox", False


def load_overleaf_cookie(
    explicit_cookie: str | None = None,
    firefox_profile: str | None = None,
    cookie_db: str | None = None,
    url: str | None = None,
    cookie_name: str | None = None,
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

    from urllib.parse import urlparse
    import overleaf_cli
    url = url or overleaf_cli.BASE_URL
    cookie_name = cookie_name or overleaf_cli.COOKIE_NAME
    host = urlparse(url).hostname or "www.overleaf.com"
    parent = host.split(".", 1)[1] if host.count(".") >= 2 else host

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
            # Firefox keeps recent cookies in the write-ahead log: copying only
            # cookies.sqlite would read a stale (often expired) session.
            for suffix in ("", "-wal", "-shm"):
                src = db_path.with_name(db_path.name + suffix)
                if src.exists():
                    shutil.copy2(src, tmp_dir / ("cookies.sqlite" + suffix))
            with sqlite3.connect(tmp_db) as conn:
                row = conn.execute(
                    """
                    SELECT value, lastAccessed, expiry
                    FROM moz_cookies
                    WHERE name = ?
                      AND host IN (?, ?, ?)
                    ORDER BY lastAccessed DESC
                    LIMIT 1
                    """,
                    (cookie_name, host, "." + host, "." + parent),
                ).fetchone()
            if row and (best_row is None or row[1] > best_row[1]):
                best_row = row
                best_db = db_path
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if best_row and best_row[0]:
        global COOKIE_EXPIRY
        exp = best_row[2] or 0
        COOKIE_EXPIRY = float(exp / 1000 if exp > 1e11 else exp) or None     # some Firefox versions store ms
        return best_row[0]

    print(
        f"Error: {cookie_name} for {host} not found in Firefox cookie jars.\n"
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


def apply_ops_strict(content: str, ops) -> str:
    """
    apply_sharejs_ops for ops that come from the server: anything that does not
    fit the document we hold (bad position or type, a delete whose text is not
    there) means our copy and the server's have diverged, so raise and let the
    caller resync instead of guessing.  Ops without i/d (comment ops) are skipped.
    """
    if not isinstance(ops, list):
        raise ValueError(f"ops is not a list: {type(ops).__name__}")
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("op is not an object")
        p = op.get("p")
        if type(p) is not int or not 0 <= p <= len(content):
            raise ValueError(f"bad op position {p!r} in a {len(content)}-char doc")
        if "i" in op:
            if not isinstance(op["i"], str):
                raise ValueError("insert text is not a string")
            content = content[:p] + op["i"] + content[p:]
        elif "d" in op:
            d = op["d"]
            if not isinstance(d, str) or content[p:p + len(d)] != d:
                raise ValueError("delete text does not match the document")
            content = content[:p] + content[p + len(d):]
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
    if not isinstance(ops, list):
        raise ValueError(f"ops is not a list: {type(ops).__name__}")
    out = []
    u16 = lambda t: len(t.encode("utf-16-le", "surrogatepass")) // 2
    n16 = len(content) if content.isascii() else u16(content)       # document length in UTF-16 units
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("op is not an object")
        p16 = op.get("p")
        if type(p16) is not int or not 0 <= p16 <= n16:
            raise ValueError(f"bad op position {p16!r}")
        cp_op = {**op, "p": _utf16_to_cp(content, p16)}
        out.append(cp_op)
        content = apply_ops_strict(content, [cp_op])
        if isinstance(cp_op.get("i"), str):
            n16 += u16(cp_op["i"])
        elif isinstance(cp_op.get("d"), str):
            n16 -= u16(cp_op["d"])
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
    # our own files: backups, merge bases, wipe confirmation, lock, temp files
    if any(p.startswith(".overleaf-sync") or p.endswith(".sync-tmp") for p in parts):
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


def collect_tree(project_data: dict) -> tuple[dict[str, str], set[str]]:
    """({folder path: folder id} with "" for the root, every entity path: docs, files, folders)."""
    folders: dict[str, str] = {}
    paths: set[str] = set()

    def _recurse(folder: dict, prefix: str) -> None:
        for kind in ("docs", "fileRefs"):
            for e in folder.get(kind, []):
                paths.add(prefix + e["name"])
        for sub in folder.get("folders", []):
            folders[prefix + sub["name"]] = sub["_id"]
            paths.add(prefix + sub["name"])
            _recurse(sub, prefix + sub["name"] + "/")

    for root in project_data.get("rootFolder", []):
        folders[""] = root["_id"]
        _recurse(root, "")
    return folders, paths


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
                 dry_run: bool = False, allow_wipe: bool = False, propagate_tree: bool = True,
                 prune_backups_days: float | None = None):
        self.project_id = project_id
        self.session_cookie = session_cookie
        self.output_dir = Path(output_dir)
        # dry_run: never send anything to Overleaf; only log what would be sent.
        self.dry_run = dry_run
        # allow_wipe: push a file that was emptied locally without asking first.
        self.allow_wipe = allow_wipe
        self._wipe_ok: set[str] = set()          # docs whose wipe the user confirmed
        self._wipe_warned_at: dict[str, float] = {}   # doc_id → wall time of the "NOT sending" warning
        self._recorded: set[tuple] = set()       # (doc, old, new) whose deletions are already in deleted.jsonl
        self._base_project_written = False
        self._poll_error_at: dict[str, float] = {}
        self._base_saved: dict[str, str] = {}    # doc_id → last base written to disk
        self._warned: set[str] = set()
        self._last_untracked_scan = 0.0
        # propagate_tree: create / rename / delete docs in Overleaf when the local files are
        # created / renamed / deleted (see _tree_scan).  Off → only text edits are synced.
        self.propagate_tree = propagate_tree
        # prune_backups_days: at start, delete backup runs older than this many days (default: keep all).
        self.prune_backups_days = prune_backups_days
        self._http = None                         # OverleafSession of the current connection
        self._folders: dict[str, str] = {}        # folder path ("" = root) → folder id
        self._server_paths: set[str] = set()      # every entity path in the project (docs, files, folders)
        self._last_tree_scan = 0.0
        self._tree_seen: dict[tuple, float] = {}  # pending tree action → first time seen
        self._tree_warned_at: dict[tuple, float] = {}
        self._tree_retry_at: dict[tuple, float] = {}
        # One `touch` confirms everything that was already waiting for it (emptied files, deletions,
        # big batches): items register when they warn, the sentinel is consumed once.
        self._confirm_waiting: dict[tuple, float] = {}    # item → wall time of its warning
        self._confirmed: set[tuple] = set()

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
        self._candidates: dict[str, tuple[str, float, float]] = {}
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
            # If the local write below fails (disk full, permissions) the state
            # must go back to what the file on disk still corresponds to:
            # otherwise the reconnect would diff the old file against the new
            # server text and push the coauthor's edit as a deletion.
            before = (state["content"], state["version"],
                      (list(inflight["ops"]), inflight["base"]) if inflight else None)
            wipe_cancelled: list[bool] = []
            try:
                base = inflight["base"] if inflight else state["content"]
                remote = wire_to_cp(base, ops)
                if inflight:
                    inflight["base"] = apply_ops_strict(base, remote)
                    inflight["ops"], remote = transform_x(inflight["ops"], remote)
                old_content = state["content"]
                state["content"] = apply_ops_strict(old_content, remote)
                if v is not None:
                    state["version"] = v + 1

                out_path = safe_join(self.output_dir, state["path"])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                def merge_remote(local: str) -> str:
                    if local == old_content:
                        return state["content"]
                    if not self.allow_wipe and self._is_wipe(old_content, local):
                        wipe_cancelled.append(True)       # do not leave a fragment of the update in an emptied file
                        return state["content"]
                    local_ops = compute_ot_ops(old_content, local)
                    _, remote_t = transform_x(local_ops, remote)
                    return apply_sharejs_ops(local, remote_t)

                _, seen = update_file(out_path, merge_remote, default=old_content)
            except BaseException:
                state["content"], state["version"] = before[0], before[1]
                if inflight and before[2]:
                    inflight["ops"], inflight["base"] = before[2]
                raise
            note = "" if seen == old_content else ", merged with local edits"
            if wipe_cancelled:
                note = ", pending empty-file wipe cancelled by this edit"
                self._warned.discard(f"wipe:{doc_id}")
                self._wipe_warned_at.pop(doc_id, None)
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
                if self.propagate_tree:
                    self._tree_scan(self._ws)
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
                key = f"poll:{type(e).__name__}:{e}"
                if _time.monotonic() - self._poll_error_at.get(key, -1e9) > 300:   # repeat every 5 min
                    self._warned.discard(key)
                self._poll_error_at[key] = _time.monotonic() if key not in self._warned else self._poll_error_at.get(key, 0)
                self._warn_once(key, f"poll error: {e}")
            _time.sleep(self.POLL_INTERVAL)

    # A save is not atomic for many tools (truncate, then write): reading in
    # between yields a half-written or empty file, which would be pushed to
    # Overleaf as "delete everything".  So a change is only pushed once the
    # file has had the same content on two consecutive polls, and a change that
    # wipes out most of the document must hold for BIG_DELETE_SECS.
    BIG_DELETE_SECS = 5.0
    MAX_DEFER_SECS = 3.0
    BIG_DELETE_MIN_CHARS = 200

    def _take_confirmations(self) -> None:
        """Consume the sentinel, if any, on behalf of every item whose warning it follows."""
        sentinel = self.output_dir / self.WIPE_SENTINEL
        try:
            made = sentinel.stat().st_mtime
        except OSError:
            return
        ok = {k for k, warned in self._confirm_waiting.items() if made >= warned - 1.0}
        if not ok:
            return                      # a stray or old sentinel must not approve what nobody has seen
        self._confirmed |= ok
        for k in ok:
            del self._confirm_waiting[k]
        try:
            sentinel.unlink()
        except OSError:
            pass

    @classmethod
    def _is_big_delete(cls, old: str, new: str) -> bool:
        if not old.strip():
            return False
        if not new.strip():
            return True
        kept = len(new) / len(old)
        return len(old) >= cls.BIG_DELETE_MIN_CHARS and kept < 0.5

    def _settled(self, changes: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        """
        Keep only the changes whose file content is stable enough to push:
        unchanged since the previous poll (so a half-written save is skipped),
        held BIG_DELETE_SECS for a large deletion, but never deferred more
        than MAX_DEFER_SECS while a writer keeps changing the file.
        """
        now = _time.monotonic()
        ready = []
        current = set()
        sentinel = self.output_dir / self.WIPE_SENTINEL
        wipes = [d for d, old, new in changes if self._is_wipe(old, new)]
        if wipes and not self.allow_wipe:
            for d in wipes:
                if d in self._wipe_warned_at:
                    self._confirm_waiting.setdefault(("wipe", d), self._wipe_warned_at[d])
            self._take_confirmations()
            for d in wipes:
                if ("wipe", d) in self._confirmed:
                    self._confirmed.discard(("wipe", d))
                    self._wipe_ok.add(d)
        for doc_id, old, new in changes:
            current.add(doc_id)
            if self._is_wipe(old, new) and not self.allow_wipe and doc_id not in self._wipe_ok:
                path = self._docs[doc_id]["path"]
                self._wipe_warned_at.setdefault(doc_id, _time.time())
                self._confirm_waiting.setdefault(("wipe", doc_id), self._wipe_warned_at[doc_id])
                self._warn_once(
                    f"wipe:{doc_id}",
                    f"{path} was emptied locally; NOT sending it ({len(old)} chars would be deleted "
                    f"in Overleaf). Restore the file to cancel, or confirm with: "
                    f"touch {shlex.quote(str(sentinel))}")
                continue
            if not self._is_wipe(old, new):
                self._wipe_ok.discard(doc_id)
                self._warned.discard(f"wipe:{doc_id}")
                self._wipe_warned_at.pop(doc_id, None)
                self._confirm_waiting.pop(("wipe", doc_id), None)
                self._confirmed.discard(("wipe", doc_id))
            seen = self._candidates.get(doc_id)       # (content, stable since, dirty since)
            if seen is None:
                self._candidates[doc_id] = (new, now, now)
                continue
            content, stable_since, dirty_since = seen
            big = self._is_big_delete(old, new)
            if content != new:
                self._candidates[doc_id] = (new, now, dirty_since)
                if big or now - dirty_since < self.MAX_DEFER_SECS:
                    continue
                ready.append((doc_id, old, new))      # still changing, but waited long enough
                continue
            if big:
                if now - stable_since < self.BIG_DELETE_SECS:
                    continue
                print(f"  {_ts()} [sync] large deletion in {self._docs[doc_id]['path']} "
                      f"held {self.BIG_DELETE_SECS:.0f}s, pushing", flush=True)
            ready.append((doc_id, old, new))
        for doc_id in list(self._candidates):
            if doc_id not in current:
                del self._candidates[doc_id]
        self._wipe_ok &= current
        for gone in [d for d in self._wipe_warned_at if d not in current]:
            del self._wipe_warned_at[gone]
            self._warned.discard(f"wipe:{gone}")
            self._confirm_waiting.pop(("wipe", gone), None)
            self._confirmed.discard(("wipe", gone))
        return ready

    # The last version both sides agreed on, kept on disk per doc, so edits made
    # while the sync was stopped are merged on the next start instead of being
    # set aside (see _sync_all_docs).
    BASE_DIR = ".overleaf-sync-base"
    WIPE_SENTINEL = ".overleaf-sync-confirm-wipe"

    def _base_path(self, doc_id: str) -> Path:
        return self.output_dir / self.BASE_DIR / doc_id

    def _load_base(self, doc_id: str) -> str | None:
        try:
            if (self.output_dir / self.BASE_DIR / ".project").read_text().strip() != self.project_id:
                return None
            return self._base_path(doc_id).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _save_base(self, doc_id: str, content: str) -> None:
        if self._base_saved.get(doc_id) == content:
            return
        try:
            d = self.output_dir / self.BASE_DIR
            d.mkdir(exist_ok=True)
            if not self._base_project_written:
                (d / ".project").write_text(self.project_id)
                self._base_project_written = True
            atomic_write(self._base_path(doc_id), content)
            self._base_saved[doc_id] = content
        except OSError as e:
            # A base that is older than what both sides now agree on would make the
            # next start re-apply old edits; better none (server wins, local backed up).
            self._base_saved.pop(doc_id, None)
            try:
                self._base_path(doc_id).unlink(missing_ok=True)
            except OSError:
                pass
            self._warn_once("base-write", f"could not save the merge base ({e}); edits made while the sync is stopped will be backed up, not merged")

    @staticmethod
    def _is_wipe(old: str, new: str) -> bool:
        return bool(old.strip()) and not new.strip()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"  {_ts()} [sync] {msg}", flush=True)

    # -- files created / renamed / deleted on disk -------------------------------------------------
    #
    # Creating and renaming are reversible and add nothing a coauthor loses, so they go through after
    # TREE_HOLD_SECS of being stable (an editor's save-by-delete-and-recreate must not look like a
    # delete).  Deleting is not: it needs the same `touch` confirmation as an emptied file, the whole
    # document is kept in the backup folder first, and Overleaf keeps a deleted doc's history too.
    # A big batch (a `git checkout`, an unzip) also needs the confirmation.
    TREE_HOLD_SECS = 3.0
    TREE_BULK = 20
    TREE_RETRY_SECS = 60.0
    TEXT_EXTS = frozenset(
        "tex latex ltx sty cls bst bib bibtex txt md rmd rnw rtex tikz mtx asy lbx bbx cbx dtx ins ist def clo "
        "ldf lco cfg lua gv mf yml yaml lhs mk xmpdata inc m".split())

    RENAME_SIMILARITY = 0.85
    SIMILARITY_MAX_CHARS = 300_000

    @classmethod
    def _similarity(cls, a: str, b: str) -> float:
        """Line-based likeness (0..1) of two texts; 0 for huge ones (not worth the time)."""
        if not a.strip() or not b.strip() or max(len(a), len(b)) > cls.SIMILARITY_MAX_CHARS:
            return 0.0
        sm = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False)
        if sm.real_quick_ratio() < 0.5 or sm.quick_ratio() < 0.5:
            return 0.0
        return sm.ratio()

    def _eligible_new_file(self, rel: str, p: Path) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if "." not in name or name.rsplit(".", 1)[1].lower() not in self.TEXT_EXTS:
            return False
        if not is_safe_relpath(rel) or len(name) > 150 or any(len(part) > 150 for part in rel.split("/")):
            return False
        if any(c in rel for c in '\\\0*<>:"|?') or p.is_symlink():
            return False
        try:
            return p.stat().st_size <= 2 * 1024 * 1024
        except OSError:
            return False

    def _list_local_files(self) -> list[tuple[str, Path]]:
        out = []
        for root, dirs, files in os.walk(self.output_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith(".") or f.endswith(".sync-tmp"):
                    continue
                p = Path(root) / f
                out.append((p.relative_to(self.output_dir).as_posix(), p))
        return out

    def _warn_untracked(self) -> None:
        """Local files that will never be uploaded as documents; say so."""
        now = _time.monotonic()
        if now - self._last_untracked_scan < 5.0:
            return
        self._last_untracked_scan = now
        with self._lock:
            tracked = {s["path"] for s in self._docs.values()}
            server = set(self._server_paths)
        for rel, p in self._list_local_files():
            if rel in tracked:
                continue
            if self.propagate_tree and rel not in server and self._eligible_new_file(rel, p):
                continue                      # _tree_scan creates it
            if rel in server:
                why = "is not a text document in Overleaf"
            elif self.propagate_tree:
                why = "is not a text file type Overleaf edits (or is too big / has an unsafe name)"
            else:
                why = "is not an Overleaf document (create it in Overleaf, or drop --no-tree-sync)"
            self._warn_once(f"untracked:{rel}", f"{rel} {why}; it will NOT be uploaded")

    def _tree_scan(self, ws) -> None:
        """Turn created / renamed / deleted local files into the same change in Overleaf."""
        now = _time.monotonic()
        if now - self._last_tree_scan < 1.0 or self._http is None or not self._ws_ready.is_set():
            return
        self._last_tree_scan = now
        with self._lock:
            if any(s.get("inflight") for s in self._docs.values()):
                return
            docs = {d: (s["path"], s["content"]) for d, s in self._docs.items()}
            server = set(self._server_paths)
        tracked = {path: d for d, (path, _) in docs.items()}
        local = dict(self._list_local_files())
        missing = {d: path for path, d in tracked.items() if path not in local}
        new = [rel for rel, p in local.items()
               if rel not in tracked and rel not in server and self._eligible_new_file(rel, p)]

        # A missing doc and a new file with the very same text are a rename / move.
        renames: dict[str, str] = {}
        if missing and new:
            texts: dict[str, str | None] = {}
            for rel in new:
                try:
                    texts[rel] = sanitize_non_bmp(local[rel].read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    texts[rel] = None
            for d, path in missing.items():
                same = [r for r in new if texts[r] is not None and texts[r] == docs[d][1]]
                rivals = [m for m in missing if docs[m][1] == docs[d][1]]
                if len(same) == 1 and len(rivals) == 1:
                    renames[d] = same[0]
            # Moved AND edited: no identical text, but one clearly-closest candidate each way.
            left_d = [d for d in missing if d not in renames]
            left_r = [r for r in new if r not in renames.values() and texts[r] is not None]
            for d in left_d:
                scored = sorted(((self._similarity(docs[d][1], texts[r]), r) for r in left_r), reverse=True)
                if not scored or scored[0][0] < self.RENAME_SIMILARITY:
                    continue
                if len(scored) > 1 and scored[1][0] >= self.RENAME_SIMILARITY:
                    continue                                          # ambiguous
                r = scored[0][1]
                back = sorted((self._similarity(docs[m][1], texts[r]) for m in left_d), reverse=True)
                if len(back) > 1 and back[1] >= self.RENAME_SIMILARITY:
                    continue
                renames[d] = r
        renamed_to = set(renames.values())
        actions = [("rename", d, r) for d, r in sorted(renames.items())]
        actions += [("create", r) for r in sorted(new) if r not in renamed_to]
        actions += [("delete", d) for d in sorted(missing) if d not in renames]

        current = set(actions)
        for key in list(self._tree_seen):
            if key not in current:
                del self._tree_seen[key]
                self._tree_warned_at.pop(key, None)
                self._warned.discard(("tree-warn", key))
                self._confirm_waiting.pop(key, None)
                self._confirmed.discard(key)
        for key in actions:
            self._tree_seen.setdefault(key, now)
        ready = [k for k in actions if now - self._tree_seen[k] >= self.TREE_HOLD_SECS
                 and now >= self._tree_retry_at.get(k, 0.0)]
        if not ready:
            return

        bulk = sum(k[0] != "delete" for k in ready) > self.TREE_BULK
        needs = [k for k in ready if k[0] == "delete" or (bulk and k[0] != "delete")]
        if needs and not self.allow_wipe:
            sentinel = self.output_dir / self.WIPE_SENTINEL
            for k in needs:
                self._tree_warned_at.setdefault(k, _time.time())
            fresh = [k for k in needs if ("tree-warn", k) not in self._warned]
            if fresh:
                self._warned.update(("tree-warn", k) for k in fresh)
                names = ", ".join((docs[k[1]][0] if k[0] == "delete" else k[-1]) for k in fresh[:5])
                more = f" and {len(fresh) - 5} more" if len(fresh) > 5 else ""
                what = "deleted locally" if not bulk else "created or moved at once"
                print(f"  {_ts()} [sync] {len(fresh)} file(s) {what}: {names}{more}. NOT changing them in "
                      f"Overleaf. Restore the file (or restart the sync) to cancel, or confirm with: touch {shlex.quote(str(sentinel))}", flush=True)
            for k in needs:
                if k not in self._confirmed:
                    self._confirm_waiting.setdefault(k, self._tree_warned_at[k])
            self._take_confirmations()
            ready = [k for k in ready if k not in needs or k in self._confirmed]
        if not ready:
            return

        done = False
        for key in ready:
            if self.dry_run:
                self._warn_once(f"dry-tree:{key}", f"[dry-run] would {key[0]} {key[-1] if key[0] != 'delete' else docs[key[1]][0]} in Overleaf; nothing sent")
                continue
            try:
                self._apply_tree_action(key, docs, local)
                self._confirmed.discard(key)
                done = True
            except Exception as e:
                if type(e).__name__ == "AuthError":
                    raise
                wait = max(self.TREE_RETRY_SECS, getattr(e, "retry_after", None) or 0)
                self._tree_retry_at[key] = _time.monotonic() + wait
                self._warn_once(f"tree-err:{key}:{e}", f"could not {key[0]} {key[-1] if key[0] != 'delete' else docs[key[1]][0]}: {e} "
                                f"(trying again in {wait:.0f}s)")
        if done:
            self._tree_seen.clear()
            self._force_reconnect(ws)          # re-join: the tree and the docs are rebuilt (and merged)

    def _ensure_folder(self, folder: str) -> str:
        """Folder id for a path, creating the missing parents."""
        parts = folder.split("/") if folder else []
        for i in range(len(parts) + 1):
            sub = "/".join(parts[:i])
            if sub in self._folders:
                continue
            parent = "/".join(parts[:i - 1])
            made = self._http.create_folder(self.project_id, parts[i - 1], self._folders[parent])
            self._folders[sub] = made["_id"]
            self._server_paths.add(sub)
            print(f"  {_ts()} [sync] created folder {sub}/ in Overleaf", flush=True)
        return self._folders[folder]

    def _apply_tree_action(self, key: tuple, docs: dict, local: dict) -> None:
        kind = key[0]
        if kind == "create":
            rel = key[1]
            if rel in self._server_paths:
                return
            try:
                local[rel].read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise RuntimeError(f"not readable as UTF-8 text ({e})")
            folder, _, name = rel.rpartition("/")
            made = self._http.create_doc(self.project_id, name, self._ensure_folder(folder))
            self._server_paths.add(rel)
            print(f"  {_ts()} [sync] new local file {rel} → created in Overleaf"
                  f"{'' if made.get('_id') else ' (no id returned)'}", flush=True)
        elif kind == "rename":
            _, doc_id, new_rel = key
            old_rel = docs[doc_id][0]
            old_folder, _, old_name = old_rel.rpartition("/")
            new_folder, _, new_name = new_rel.rpartition("/")
            if new_rel in self._server_paths:
                raise RuntimeError(f"{new_rel} already exists in Overleaf")
            try:
                edited = local[new_rel].read_text(encoding="utf-8") != docs[doc_id][1]
            except (OSError, UnicodeDecodeError):
                edited = False
            if edited and not self._record_doc_deletion(old_rel, docs[doc_id][1]):
                raise RuntimeError("could not keep a copy of the document first")
            if old_folder == new_folder:
                self._http.rename_doc(self.project_id, doc_id, new_name)
            else:
                target = self._ensure_folder(new_folder)
                if old_name == new_name:
                    self._http.move_doc(self.project_id, doc_id, target)
                elif (new_folder + "/" + old_name).lstrip("/") not in self._server_paths:
                    self._http.move_doc(self.project_id, doc_id, target)       # move first, then rename
                    self._http.rename_doc(self.project_id, doc_id, new_name)
                elif (old_folder + "/" + new_name).lstrip("/") not in self._server_paths:
                    self._http.rename_doc(self.project_id, doc_id, new_name)   # rename first, then move
                    self._http.move_doc(self.project_id, doc_id, target)
                else:
                    raise RuntimeError("both intermediate names are taken in Overleaf")
            print(f"  {_ts()} [sync] {old_rel} was renamed locally → {new_rel} in Overleaf"
                  f"{' (its text was edited too; merged on the re-join)' if edited else ''}", flush=True)
        elif kind == "delete":
            doc_id = key[1]
            path, content = docs[doc_id]
            if not self._record_doc_deletion(path, content):
                raise RuntimeError("could not keep a copy of the document first")
            self._http.delete_doc(self.project_id, doc_id)
            self._base_saved.pop(doc_id, None)
            try:
                self._base_path(doc_id).unlink(missing_ok=True)
            except OSError:
                pass
            print(f"  {_ts()} [sync] {path} deleted locally (confirmed) → deleted in Overleaf; "
                  f"a copy is in {self.output_dir}/.overleaf-sync-backup/{self._started}/", flush=True)

    def _record_doc_deletion(self, path: str, content: str) -> bool:
        try:
            folder = self.output_dir / ".overleaf-sync-backup" / self._started
            dest = folder / "deleted-docs" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            with open(folder / "deleted.jsonl", "a", encoding="utf-8") as log:
                log.write(json.dumps({"time": datetime.datetime.now().isoformat(timespec="milliseconds"),
                                      "path": path, "deleted_document": True, "chars": len(content)},
                                     ensure_ascii=False) + "\n")
            return True
        except OSError as e:
            self._warn_once(f"record-doc:{e.errno}", f"cannot keep a copy of {path} ({e}); not deleting it in Overleaf")
            return False

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
        to_save: list[tuple[str, str]] = []
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
                    to_save.append((doc_id, fc))
                    self._warned.discard(f"wipe:{doc_id}")      # restored: a later wipe warns again
                    self._wipe_warned_at.pop(doc_id, None)
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
                    try:
                        atomic_write(out_path, sanitized)
                    except OSError as e:
                        self._warn_once(f"sanitize:{doc_id}:{e.errno}",
                                        f"{state['path']}: cannot rewrite the file ({e}); not sending it")
                        continue
                    print(
                        f"  {_ts()} [sync] {state['path']}: Overleaf cannot store chars outside "
                        f"the BMP (e.g. emoji); replaced them with U+FFFD U+FFFD locally",
                        flush=True,
                    )
                    fc = sanitized
                if fc != state["content"]:
                    changed.append((doc_id, state["content"], fc))
        for doc_id, content in to_save:        # disk I/O outside the lock the socket handler needs
            self._save_base(doc_id, content)
        return changed

    def _push_change(self, doc_id: str, old_content: str, new_content: str) -> bool:
        """
        Send old → new as ONE applyOtUpdate and wait for its echo.  Remote ops
        arriving meanwhile are transformed against it by _on_ot_update.
        """
        cp_ops = compute_ot_ops(old_content, new_content)
        if not cp_ops:
            return True
        with self._lock:                        # cheap pre-check: do not record a push that will bail
            doc = self._docs.get(doc_id)
            if doc is None or doc["content"] != old_content or doc.get("inflight"):
                return False
            ws0 = self._ws
            if not self._ws_ready.is_set() or ws0 is None or not ws0.is_connected():
                self._ws_ready.clear()
                return False
            path0 = doc["path"]
        # A change that deletes text is only sent once that text is safely on
        # disk (see _snapshot_before_delete); with a full disk it waits as a
        # whole.  Retries of the same change are recorded once.
        key = (doc_id, hash(old_content), hash(new_content))
        if key not in self._recorded:
            if not self._snapshot_before_delete(path0, old_content, cp_ops):
                return False
            if len(self._recorded) > 500:
                self._recorded.clear()
            self._recorded.add(key)
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

    BIG_SNAPSHOT_CHARS = 2000      # a push deleting this much also keeps a full copy
    SNAPSHOT_MAX_FILES = 20

    def _snapshot_before_delete(self, pathname: str, old_content: str, cp_ops: list[dict]) -> bool:
        """
        Whatever goes wrong with a deletion (a stale save that overwrote a
        coauthor's text, a tool bug, a mistake), the text must stay
        recoverable.  Every deleted string is appended to deleted.jsonl with its
        position; a push that deletes a lot also keeps the whole document as
        Overleaf has it.  (Overleaf's own History has it too.)
        """
        deleted = [(o["p"], o["d"]) for o in cp_ops if "d" in o]
        if not deleted:
            return True
        try:
            folder = self.output_dir / ".overleaf-sync-backup" / self._started
            folder.mkdir(parents=True, exist_ok=True)
            now = datetime.datetime.now().isoformat(timespec="milliseconds")
            with open(folder / "deleted.jsonl", "a", encoding="utf-8") as log:
                for p, text in deleted:
                    # The diff deletes only the characters that differ, so the
                    # record also keeps the whole line(s) they were part of.
                    start = old_content.rfind("\n", 0, p) + 1
                    if text.endswith("\n"):
                        end = p + len(text)
                    else:
                        end = old_content.find("\n", p + len(text))
                        end = len(old_content) if end < 0 else end + 1
                    log.write(json.dumps({"time": now, "path": pathname, "pos": p, "deleted": text,
                                          "lines": old_content[start:end]}, ensure_ascii=False) + "\n")
            if sum(len(t) for _, t in deleted) >= self.BIG_SNAPSHOT_CHARS:
                dest = folder / f"{pathname}.{datetime.datetime.now().strftime('%H%M%S.%f')}.before-push"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(old_content, encoding="utf-8")
                snaps = sorted(folder.rglob("*.before-push"), key=lambda p: p.stat().st_mtime)
                for stale in snaps[: max(0, len(snaps) - self.SNAPSHOT_MAX_FILES)]:
                    stale.unlink(missing_ok=True)
        except OSError as e:
            self._warn_once(f"record:{e.errno}",
                            f"could not write the deletion record ({e}); NOT deleting text in Overleaf "
                            f"until it can be recorded (free disk space / fix permissions on "
                            f"{self.output_dir}/.overleaf-sync-backup). Your file is untouched.")
            return False
        return True

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
        self._http = session

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

    def _prune_backups(self) -> None:
        """Opt-in: remove whole backup runs (`<timestamp>/`) older than prune_backups_days; never this run's."""
        if not self.prune_backups_days:
            return
        root = self.output_dir / ".overleaf-sync-backup"
        cutoff = datetime.datetime.now() - datetime.timedelta(days=self.prune_backups_days)
        try:
            runs = sorted(root.iterdir())
        except OSError:
            return
        for run in runs:
            try:
                when = datetime.datetime.strptime(run.name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue                                    # not one of ours: leave it alone
            if run.name == self._started or when >= cutoff or not run.is_dir() or run.is_symlink():
                continue
            try:
                shutil.rmtree(run)
                print(f"  {_ts()} [sync] pruned backup {run.name} (older than {self.prune_backups_days:g} days)", flush=True)
            except OSError as e:
                self._warn_once(f"prune:{run.name}", f"could not prune {run}: {e}")

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
        with self._lock:
            self._folders, self._server_paths = collect_tree(project_data)

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
                if initial and old_content is None:
                    old_content = self._load_base(doc_id)   # last agreed version of a past run
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

            if out_path.exists() and (not initial or old_content is not None):
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
                if initial and seen is not None and seen != server_content and branch == ["merge"]:
                    self._backup_local(pathname, seen)      # keep the pre-merge file too
                if seen != server_content:
                    if initial:
                        print(f"  {pathname:<45} v{version}  ({len(server_content)} chars)")
                    if branch == ["stale"]:
                        print(f"  {_ts()} [sync] stale local {pathname} → updated to v{version}", flush=True)
                    else:
                        print(f"  {_ts()} [sync] local edit pending on {pathname} — merged", flush=True)
                    continue
            if not initial and self.propagate_tree and old_content is not None and not out_path.exists():
                # Deleted or moved on disk: _tree_scan decides (rewriting it here would bring
                # back the old name of a rename, and leave a duplicate doc behind).
                continue
            if initial and out_path.exists():
                try:
                    previous = out_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    previous = None
                if previous is not None and previous != server_content:
                    self._backup_local(pathname, previous)
            atomic_write(out_path, server_content)
            self._save_base(doc_id, server_content)
            if initial:
                print(f"  {pathname:<45} v{version}  ({len(server_content)} chars)")

    # -- startup --------------------------------------------------------------

    def _acquire_dir_lock(self) -> None:
        """Two syncs on one directory would fight over the files; refuse the second."""
        import fcntl
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.output_dir / ".overleaf-sync.lock", "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"Error: another overleaf-sync is already syncing {self.output_dir}", file=sys.stderr)
            sys.exit(1)

    def run(self):
        self._acquire_dir_lock()
        try:
            (self.output_dir / self.WIPE_SENTINEL).unlink()    # only a confirmation given after a warning counts
        except OSError:
            pass
        self._running = True
        self._prune_backups()
        try:
            self._connect_and_sync_state(initial=True)
        except Exception as e:
            print(f"Error: initial sync failed: {e}")
            sys.exit(1)

        print(f"\nBidirectional sync active... (Ctrl-C to stop)\n", flush=True)
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
                auth_failures = 0
                while self._running:
                    try:
                        self._connect_and_sync_state(initial=False)
                        print(f"  {_ts()} [sync] reconnected", flush=True)
                        break
                    except Exception as e:
                        if type(e).__name__ == "UnsupportedProtocol":
                            self._fatal = f"{e}\nLocal files are untouched."
                            self._running = False
                            break
                        if type(e).__name__ == "AuthError":
                            auth_failures += 1
                            if auth_failures >= 3:
                                self._fatal = (f"{e}\nLocal files are untouched; nothing more "
                                               f"will be sent to Overleaf.")
                                self._running = False
                                break
                        else:
                            auth_failures = 0
                        # Back off: hammering a rate-limiting server (HTTP 429)
                        # or a down one only makes the outage longer.
                        wait = max(retry, getattr(e, "retry_after", None) or 0)
                        print(f"  {_ts()} [sync] reconnect failed: {e} — retrying in {wait:.0f}s", flush=True)
                        end = _time.monotonic() + wait
                        while self._running and _time.monotonic() < end:
                            _time.sleep(min(1.0, max(0.0, end - _time.monotonic())))
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

def load_config(path: str | None) -> tuple[dict, Path | None]:
    """Read the TOML config (explicit path, ./.overleaf-sync.toml, or the user config)."""
    import tomllib
    candidates = [Path(path).expanduser()] if path else [
        Path.cwd() / ".overleaf-sync.toml",
        Path.home() / ".config" / "overleaf-sync" / "config.toml",
    ]
    for p in candidates:
        if p.is_file():
            try:
                return tomllib.loads(p.read_text(encoding="utf-8")), p
            except tomllib.TOMLDecodeError as e:
                print(f"Error: {p}: {e}", file=sys.stderr)
                sys.exit(1)
    if path:
        print(f"Error: config file {path} not found", file=sys.stderr)
        sys.exit(1)
    return {}, None


def resolve_settings(args, cfg: dict) -> dict:
    """Merge flags > project profile > top-level config; pick the project."""
    projects = cfg.get("projects", {})
    target = args.project_id
    profile: dict = {}
    name = None
    if target in projects:
        name, profile = target, projects[target]
    elif target is None and projects:
        if len(projects) == 1:
            name, profile = next(iter(projects.items()))
        else:
            sys.exit("Error: several projects in the config; name one: " + ", ".join(sorted(projects)))
    project_id = profile.get("project_id") or (target if target not in projects else None)
    if project_id is None:
        project_id = project_from_git_remote()
    if not re.fullmatch(r"[0-9a-f]{24}", project_id or ""):
        sys.exit(f"Error: {project_id!r} is not an Overleaf project id (24 hex characters, from the project URL)")
    pick = lambda key: profile.get(key, cfg.get(key))
    output_dir = args.output_dir or pick("output_dir") or os.path.join(os.getcwd(), name or project_id)
    return {
        "project_id": project_id,
        "name": name,
        "url": args.url or pick("url"),
        "cookie_name": args.cookie_name or pick("cookie_name"),
        "cookie_file": args.cookie_file or pick("cookie_file"),
        "output_dir": str(Path(output_dir).expanduser()),
        "dry_run": args.dry_run or bool(pick("dry_run")),
        "allow_wipe": args.allow_wipe or bool(pick("allow_wipe")),
        "prune_backups_days": args.prune_backups if args.prune_backups is not None else pick("prune_backups_days"),
        "tree_sync": not args.no_tree_sync and pick("tree_sync") is not False,
        "allow_unknown_protocol": args.allow_unknown_protocol or bool(pick("allow_unknown_protocol")),
    }


def project_from_git_remote() -> str:
    import subprocess as _sp
    try:
        remotes_out = _sp.check_output(["git", "remote", "-v"], stderr=_sp.PIPE, text=True)
    except Exception as exc:
        sys.exit(f"Error: no project given and 'git remote -v' failed: {exc}")
    m = re.search(r"overleaf\.com/([a-f0-9]{24})", remotes_out)
    if not m:
        sys.exit("Error: no project given, no config, and no Overleaf git remote found.\n"
                 "Pass the project id (the 24 hex characters in the project URL).")
    print(f"[auto] project_id={m.group(1)} (from git remote)", flush=True)
    return m.group(1)


def run_check(settings: dict, cookie: str) -> int:
    """Read-only diagnostics: authenticate, join the project and every doc, compare with the local folder."""
    sys.path.insert(0, str(Path(__file__).parent))
    import overleaf_cli
    out_dir = Path(settings["output_dir"])
    print(f"Instance : {overleaf_cli.BASE_URL}  (session cookie: {overleaf_cli.COOKIE_NAME})")
    print(f"Project  : {settings['project_id']}")
    print(f"Folder   : {out_dir}  ({'exists' if out_dir.is_dir() else 'will be created'})")
    session = overleaf_cli.OverleafSession(cookie)
    boot = session.get_project_bootstrap(settings["project_id"])
    text, _ = describe_cookie_expiry(COOKIE_EXPIRY)
    print(f"Cookie   : {text}")
    print(f"Logged in: yes — project \"{boot.get('ol-projectName', '?')}\"")
    print(f"Protocol : OT migration stage {boot.get('ol-otMigrationStage', 'not announced')} (supported: 0)")
    ws = TrackingSocketIOClient(session, settings["project_id"])
    ws.connect()
    ws.run_forever()
    try:
        raw = collect_docs_from_project(ws.join_project())
        docs = filter_safe_docs(raw)
        if not docs:
            print("Error: the project has no text documents")
            return 1
        print(f"Real-time: connected, {len(docs)} text document(s)"
              + (f" ({len(raw) - len(docs)} ignored: unsafe path)" if len(raw) != len(docs) else ""))
        differing = 0
        for doc_id, rel in sorted(docs.items(), key=lambda x: x[1]):
            lines, version = ws.join_doc(doc_id)
            content = "\n".join(lines)
            local = out_dir / rel
            if not local.exists():
                state = "not on disk yet"
            else:
                try:
                    same = local.read_text(encoding="utf-8") == content
                except UnicodeDecodeError:
                    same = False
                state = "identical" if same else "DIFFERS (a backup is kept before it is replaced)"
                differing += not same
            print(f"  {rel:<45} v{version:<6} {len(content):>8} chars   {state}")
            ws.leave_doc(doc_id)
        print("\nOK: nothing was modified. Start with --dry-run first on an important project.")
        return 0
    finally:
        ws.disconnect()


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="overleaf-sync",
        description="Live two-way sync between an Overleaf project and a local folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              overleaf-sync 69a0589216d96e98bc76b8c6 --dry-run     # first look, sends nothing
              overleaf-sync 69a0589216d96e98bc76b8c6 --check       # diagnostics only
              overleaf-sync paper                                  # profile from the config file
              overleaf-sync 69a0... --url http://localhost:8080 --login-email me@x.org --password-file pw

            config (~/.config/overleaf-sync/config.toml):
              url = "https://www.overleaf.com"
              cookie_file = "~/.config/overleaf-sync/cookie"
              [projects.paper]
              project_id = "69a0589216d96e98bc76b8c6"
              output_dir = "~/papers/paper"
        """),
    )
    parser.add_argument("project_id", nargs="?", default=None,
                        help="project id (24 hex chars from the URL) or a profile name from the config; "
                             "taken from the config or a git remote if omitted")
    parser.add_argument("--url", default=None, help="Overleaf instance (default https://www.overleaf.com)")
    parser.add_argument("--cookie-name", default=None,
                        help="session cookie name (default: overleaf_session2 on overleaf.com, overleaf.sid elsewhere)")
    parser.add_argument("--cookie", default=None,
                        help="session cookie value (visible in ps; prefer --cookie-file)")
    parser.add_argument("--cookie-file", default=None, help="file holding the session cookie value")
    parser.add_argument("--login-email", default=None,
                        help="log in with e-mail/password (self-hosted only; overleaf.com needs the browser cookie)")
    parser.add_argument("--password-file", default=None, help="file holding the password for --login-email")
    parser.add_argument("--firefox-profile", default=None, help="Firefox profile directory to read cookies.sqlite from")
    parser.add_argument("--cookie-db", default=None, help="path to a specific Firefox cookies.sqlite")
    parser.add_argument("--config", default=None, help="config file (default ./.overleaf-sync.toml, then ~/.config/overleaf-sync/config.toml)")
    parser.add_argument("--output-dir", default=None, help="local folder (default ./<project id or profile>)")
    parser.add_argument("--check", action="store_true", help="read-only diagnostics, then exit")
    parser.add_argument("--listen", action="store_true", help="one-way mirror Overleaf → local")
    parser.add_argument("--allow-wipe", action="store_true",
                        help="push a file emptied locally without asking (default: hold it until confirmed)")
    parser.add_argument("--prune-backups", type=float, metavar="DAYS", default=None,
                        help="at start, delete .overleaf-sync-backup runs older than DAYS days (default: keep everything)")
    parser.add_argument("--no-tree-sync", action="store_true",
                        help="do not create / rename / delete Overleaf documents when local files are created / renamed / deleted")
    parser.add_argument("--allow-unknown-protocol", action="store_true",
                        help="go on even if the server announces an OT protocol stage other than 0 (unsupported, may corrupt documents)")
    parser.add_argument("--dry-run", action="store_true",
                        help="pull from Overleaf but never send local edits; log what would be sent")

    args = parser.parse_args(argv)
    cfg, cfg_path = load_config(args.config)
    settings = resolve_settings(args, cfg)

    sys.path.insert(0, str(Path(__file__).parent))
    import overleaf_cli
    overleaf_cli.configure(settings["url"], settings["cookie_name"])
    overleaf_cli.ALLOW_UNKNOWN_PROTOCOL = settings["allow_unknown_protocol"]

    cookie_file = settings["cookie_file"]
    cookie = args.cookie
    if not cookie and cookie_file:
        cookie = read_cookie_file(cookie_file)
    if not cookie and args.login_email and not os.environ.get("OVERLEAF_COOKIE"):
        password = (Path(args.password_file).expanduser().read_text().strip() if args.password_file
                    else os.environ.get("OVERLEAF_PASSWORD"))
        if not password:
            sys.exit("Error: --login-email needs --password-file or $OVERLEAF_PASSWORD")
        try:
            cookie = overleaf_cli.login_with_password(args.login_email, password)
        except overleaf_cli.AuthError as e:
            sys.exit(f"Error: {e}")
    session_cookie = load_overleaf_cookie(
        explicit_cookie=cookie,
        firefox_profile=args.firefox_profile,
        cookie_db=args.cookie_db,
    )

    if args.check:
        try:
            sys.exit(run_check(settings, session_cookie))
        except (overleaf_cli.AuthError, overleaf_cli.UnsupportedProtocol) as e:
            sys.exit(f"Error: {e}")
    text, urgent = describe_cookie_expiry(COOKIE_EXPIRY)
    if urgent:
        print(f"Warning: the session cookie {text}.", flush=True)
    if args.listen:
        OverleafListener(
            project_id=settings["project_id"],
            session_cookie=session_cookie,
            output_dir=settings["output_dir"],
        ).run()
    else:
        OverleafSync(
            project_id=settings["project_id"],
            session_cookie=session_cookie,
            output_dir=settings["output_dir"],
            dry_run=settings["dry_run"],
            allow_wipe=settings["allow_wipe"],
            propagate_tree=settings["tree_sync"],
            prune_backups_days=settings["prune_backups_days"],
        ).run()


if __name__ == "__main__":
    main()
