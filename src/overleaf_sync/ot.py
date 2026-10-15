"""
Pure text / operational-transformation helpers for Overleaf's ShareJS text type.

No network, no files: diffing a text into ops, applying ops (strictly or not), converting between
code-point and UTF-16 positions, transforming concurrent ops against each other, and 3-way merging.
Shared by the file-sync daemon (`overleaf_sync.sync`) and any other front-end.
"""

from __future__ import annotations

import difflib
import re

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


REFINE_MAX_CELLS = 40_000     # lines(old) x lines(new) above this: leave the hunk as one replace
REFINE_THOROUGH_CELLS = 1_000  # up to this many line pairs, similarity also counts text inserted mid-line
REFINE_THOROUGH_SIMILARITY = 0.8   # ...but then it must be close: lines made of the same few characters match by accident
REFINE_MIN_SIMILARITY = 0.5   # two lines are "the same line, edited" above this


def _line_similarity(x: str, y: str, thorough: bool) -> float:
    shorter = max(min(len(x), len(y)), 1)
    size = min(1.0, 2 * shorter / max(len(x), len(y), 1))       # a very short text is not "the same line" as a long one
    p = _common_prefix(x, y)
    q = _common_suffix(x[p:], y[p:], min(len(x), len(y)) - p)
    fast = size * (p + q) / shorter                          # a line with text added to it still counts as the same line
    if fast >= REFINE_MIN_SIMILARITY or not thorough or max(len(x), len(y)) > 300:
        return fast
    sm = difflib.SequenceMatcher(None, x, y, autojunk=False)       # text added in the middle too
    mid = size * sum(b.size for b in sm.get_matching_blocks()) / shorter
    return mid if mid >= REFINE_THOROUGH_SIMILARITY else fast


def _embed(short: str, long: str) -> list[int] | None:
    """Indexes in `long` where `short` appears as a subsequence (leftmost embedding), or None."""
    idx, j = [], 0
    for ch in short:
        j = long.find(ch, j)
        if j < 0:
            return None
        idx.append(j)
        j += 1
    return idx


def _pure_changes(o: str, n: str) -> list[tuple[int, str, str]] | None:
    """If `n` is `o` with text only inserted (or only removed), those separate changes; else None."""
    if not o or not n or len(o) == len(n):
        return None
    grows = len(n) > len(o)
    short, long = (o, n) if grows else (n, o)
    idx = _embed(short, long)
    if idx is None:
        return None
    out, prev = [], 0                       # `prev`: index in `long` after the last matched char
    for k, j in enumerate(idx + [len(long)]):
        if j > prev:
            gap = long[prev:j]
            out.append((k, "", gap) if grows else (prev, gap, ""))
        prev = j + 1
    if grows:                               # `k` counted matched chars of o: that is the offset in o
        return out
    return out


def _refine_hunk(o: str, n: str) -> list[tuple[int, str, str]]:
    """
    Split a replaced hunk into separate changes (offset in `o`, old text, new text).  A hunk can be a
    whole stretch of lines when every line changed a little (a token typed at the start of each line):
    as one delete+insert, a concurrent edit inside it would be dragged out of place when transformed.
    Lines are paired by similarity (an order-preserving alignment); a paired line becomes one small
    replace, unpaired lines whole deletions / insertions.
    """
    if o and n and o != n:
        if len(n) > len(o) and o in n:                                  # text added on both sides of o
            k = n.index(o)
            return [c for c in ((0, "", n[:k]), (len(o), "", n[k + len(o):])) if c[2]]
        if len(o) > len(n) and n in o:                                  # text removed on both sides of n
            k = o.index(n)
            return [c for c in ((0, o[:k], ""), (k + len(n), o[k + len(n):], "")) if c[1]]
    A, B = o.splitlines(keepends=True), n.splitlines(keepends=True)
    if not o or not n or len(A) * len(B) > REFINE_MAX_CELLS:
        return [(0, o, n)]
    thorough = len(A) * len(B) <= REFINE_THOROUGH_CELLS
    sim = [[_line_similarity(x, y, thorough) for y in B] for x in A]
    # best[i][j]: best total similarity pairing A[i:] with B[j:]
    best = [[0.0] * (len(B) + 1) for _ in range(len(A) + 1)]
    for i in range(len(A) - 1, -1, -1):
        for j in range(len(B) - 1, -1, -1):
            top = max(best[i + 1][j], best[i][j + 1])
            if sim[i][j] >= REFINE_MIN_SIMILARITY:
                top = max(top, best[i + 1][j + 1] + sim[i][j])
            best[i][j] = top
    pairs, i, j = [], 0, 0
    while i < len(A) and j < len(B):
        if sim[i][j] >= REFINE_MIN_SIMILARITY and best[i][j] == best[i + 1][j + 1] + sim[i][j]:
            pairs.append((i, j)); i += 1; j += 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    offs = [0]
    for line in A:
        offs.append(offs[-1] + len(line))
    out: list[tuple[int, str, str]] = []
    ai = bi = 0
    for pi, pj in pairs + [(len(A), len(B))]:
        if pi > ai or pj > bi:                                    # unpaired lines between two pairs
            out.append((offs[ai], "".join(A[ai:pi]), "".join(B[bi:pj])))
        if pi < len(A):
            x, y = A[pi], B[pj]
            p = _common_prefix(x, y)
            q = _common_suffix(x[p:], y[p:], min(len(x), len(y)) - p)
            xm, ym = x[p:len(x) - q], y[p:len(y) - q]
            if xm and ym and len(ym) > len(xm) and xm in ym:           # text added on both sides of xm
                k = ym.index(xm)
                out.append((offs[pi] + p, "", ym[:k]))
                out.append((offs[pi] + p + len(xm), "", ym[k + len(xm):]))
            elif xm and ym and len(xm) > len(ym) and ym in xm:         # text removed on both sides of ym
                k = xm.index(ym)
                out.append((offs[pi] + p, xm[:k], ""))
                out.append((offs[pi] + p + k + len(ym), xm[k + len(ym):], ""))
            elif xm or ym:
                pure = _pure_changes(xm, ym)
                if pure:
                    out.extend((offs[pi] + p + off, ot, nt) for off, ot, nt in pure)
                else:
                    out.append((offs[pi] + p, xm, ym))
        ai, bi = pi + 1, pj + 1
    return out or [(0, o, n)]


def _unmatched(t: str) -> int:
    """Delimiters of `t` left without a partner inside it ("<T5>" has none, "5><T" has two)."""
    bad = t.count("$") % 2
    for a, b in ("<>", "{}", "()", "[]"):
        depth = 0
        for ch in t:
            if ch == a:
                depth += 1
            elif ch == b:
                if depth:
                    depth -= 1
                else:
                    bad += 1
        bad += depth
    return bad


def _slide(old: str, start: int, text: str, deleting: bool, lo: int, hi: int, line_start_wins: bool) -> tuple[int, str]:
    """
    A pure insertion (or deletion) of `text` at `start` can often be written at several neighbouring
    positions (inserting "<T3>" before "<T3>" is also inserting "4><T" after "<T3").  The diff picks
    one by accident; a concurrent edit at the same spot would then land inside a word.  Move it only
    to a place that cuts strictly fewer words (or, if line_start_wins, to the start of a line),
    never outside [lo, hi], the untouched stretch of `old` around it.
    """
    def can_left(q: int, t: str) -> bool:
        return q > lo and old[q - 1] == t[-1]

    def can_right(q: int, t: str) -> bool:
        i = q + len(t) if deleting else q                # the character the text would slide over
        return i < hi and old[i] == t[0]

    def cuts(q: int, t: str) -> tuple[int, int]:
        """(unbalanced delimiters, words cut): the text a person typed is usually a whole unit."""
        after = old[q + len(t)] if deleting and q + len(t) < len(old) else old[q] if not deleting and q < len(old) else ""
        return _unmatched(t), int(q > 0 and old[q - 1].isalnum() and t[0].isalnum()) + int(t[-1].isalnum() and after.isalnum())

    q, t = start, text
    while can_left(q, t):
        q, t = q - 1, t[-1] + t[:-1]
    cands = []
    while True:
        if line_start_wins and (q == 0 or old[q - 1] == "\n"):
            return q, t
        cands.append((*cuts(q, t), abs(q - start), q, t))
        if not can_right(q, t):
            break
        q, t = q + 1, t[1:] + t[0]
    here = cuts(start, text)
    best = min(cands)
    return (best[3], best[4]) if best[:2] < here else (start, text)


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
    refined: set[int] = set()            # indexes of spans produced by _refine_hunk
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
                parts = _refine_hunk(o_t, n_t)
                for off, ot, nt in parts:
                    if len(parts) > 1:
                        refined.add(len(spans))
                    spans.append((prefix + offs[a1] + p + off, ot, nt))
    else:
        parts = _refine_hunk(old_mid, new_mid)
        for off, ot, nt in parts:
            if len(parts) > 1:
                refined.add(len(spans))
            spans.append((prefix + off, ot, nt))

    # A pure insertion or deletion may be writable at several neighbouring places: pick the sensible one,
    # without crossing the neighbouring changes.
    for k, (start, o_t, n_t) in enumerate(spans):
        if bool(o_t) != bool(n_t):
            lo = spans[k - 1][0] + len(spans[k - 1][1]) if k else 0
            hi = spans[k + 1][0] if k + 1 < len(spans) else len(old)
            at, moved = _slide(old, start, o_t or n_t, deleting=bool(o_t), lo=lo, hi=hi, line_start_wins=k in refined)
            spans[k] = (at, moved, "") if o_t else (at, "", moved)

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

