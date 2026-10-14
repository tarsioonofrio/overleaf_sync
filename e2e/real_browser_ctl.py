"""Helper for driving real-Chrome-vs-local-file scenarios against a throw-away overleaf.com project.

The browser half is driven separately (Claude in Chrome / javascript_tool); this script does the
local half and the verification. Usage:
    OVERLEAF_PROJECT=<id> real_browser_ctl.py up | down | local <scenario> | verify <scenario> [len hash] | restore | state
The project must be a throw-away one: `restore` rewrites main.tex and sample.bib from /tmp/olt/*.original.*.
"""
import os, re, struct, subprocess, sys, time, random, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("OVERLEAF_URL", "OVERLEAF_COOKIE_NAME", "OVERLEAF_COOKIE"): os.environ.pop(k, None)
import overleaf_cli, overleaf_sync as osy

PID = os.environ["OVERLEAF_PROJECT"]
OUT = Path("/tmp/olt/ol_br"); LOG = Path("/tmp/olt/br_sync.log"); PIDF = Path("/tmp/olt/br_sync.pid")
ORIG = Path("/tmp/olt/main.original.tex").read_text()
ORIG_BIB = Path("/tmp/olt/sample.original.bib").read_text()
M, B = OUT / "main.tex", OUT / "sample.bib"
TOK = re.compile(r"@@[BL]\d+@@\n|@@[BL][a-z]+-\d+@@")


def fnv(s):
    h = 0x811C9DC5
    for u in struct.unpack("<%dH" % (len(s.encode("utf-16-le")) // 2), s.encode("utf-16-le")):
        h = ((h ^ u) * 0x01000193) & 0xFFFFFFFF
    return h


def server_texts():
    s = overleaf_cli.OverleafSession(osy.load_overleaf_cookie()); s.get_project_bootstrap(PID)
    ws = osy.TrackingSocketIOClient(s, PID); ws.on("otUpdateApplied", lambda a: None)
    ws.connect(); ws.run_forever()
    docs = {v: k for k, v in osy.collect_docs_from_project(ws.join_project()).items()}
    out = {p: "\n".join(ws.join_doc(i)[0]) for p, i in docs.items()}
    ws.disconnect()
    return out


def up():
    if PIDF.exists():
        try: os.kill(int(PIDF.read_text()), 0); print("already up"); return
        except OSError: pass
    OUT.mkdir(parents=True, exist_ok=True)
    f = open(LOG, "a")
    p = subprocess.Popen(["overleaf-sync", PID, "--output-dir", str(OUT)], stdout=f, stderr=f,
                         env=dict(os.environ, PYTHONUNBUFFERED="1"), start_new_session=True)
    PIDF.write_text(str(p.pid))
    for _ in range(120):
        if "Bidirectional sync active" in LOG.read_text()[-3000:]: break
        time.sleep(0.5)
    time.sleep(2); print("up", M.exists() and len(M.read_text()))


def down():
    if PIDF.exists():
        try: os.kill(int(PIDF.read_text()), 15)
        except OSError: pass
        PIDF.unlink()
    time.sleep(2); print("down")


def write(p, text):
    Path(p).write_text(text)


def state():
    st = server_texts()
    loc = M.read_text()
    print(f"server main {len(st['main.tex'])} fnv {fnv(st['main.tex'])} | local {len(loc)} fnv {fnv(loc)} | "
          f"server bib {len(st['sample.bib'])} local bib {len(B.read_text())}")


# ---- local halves ----------------------------------------------------------------------------
def local(name):
    r = random.Random(7)
    if name == "typing":              # prepend whole lines (read-modify-write like an editor save)
        time.sleep(1)
        for i in range(8):
            write(M, f"@@L{i}@@\n" + M.read_text()); time.sleep(1.1)
    elif name == "sameline":
        time.sleep(2)
        t = M.read_text(); i = t.index("\\title{Your Paper}") + len("\\title{Your Paper}")
        write(M, t[:i] + "@@Lx-1@@" + t[i:])
    elif name == "delete":
        time.sleep(2)
        for i in range(4):
            write(M, M.read_text() + f"@@L{i}@@\n"); time.sleep(1.3)
    elif name == "reformat":
        time.sleep(2)
        write(M, "% header added locally\n" + "\n".join(l.rstrip() for l in M.read_text().split("\n")))
    elif name == "unicode":
        time.sleep(1)
        for i, s in enumerate(["emoji 😀🚀 ", "CJK 漢字かな ", "combining éñ ", "RTL שלום ", "astral 𝔘𝔫𝔦 "]):
            write(M, M.read_text() + f"@@L{i}@@ {s}\n"); time.sleep(1.2)
    elif name == "bib":               # other document edited locally while the browser edits main.tex
        time.sleep(1)
        for i in range(6):
            write(B, B.read_text() + f"%% @@L{i}@@\n"); time.sleep(1.0)
    elif name == "burst":
        for i in range(150):
            with open(M, "a") as f: f.write(f"@@L{i}@@\n")
            time.sleep(0.03)
    elif name == "undo":
        time.sleep(3); write(M, M.read_text() + "@@L0@@\n")
    elif name == "offline":           # runs while the sync is down; the caller restarts it
        time.sleep(0.5)
        for i in range(5):
            write(M, M.read_text() + f"@@L{i}@@\n"); time.sleep(0.4)
    elif name == "bigdelete":
        time.sleep(1); write(M, "")
    elif name == "toobig":
        time.sleep(1); write(M, M.read_text() + ("x" * 99 + "\n") * 22000)   # > 2 MiB
    else:
        raise SystemExit("unknown scenario " + name)
    print("local done", name)


# ---- verification ----------------------------------------------------------------------------
def subseq(small, big):
    it = iter(big); return all(c in it for c in small)


def verify(name, blen=None, bhash=None):
    time.sleep(6)                     # let the last edit settle in both directions
    for _ in range(40):
        st = server_texts(); srv = st["main.tex"]
        if srv == M.read_text() and st["sample.bib"] == B.read_text(): break
        time.sleep(1)
    loc = M.read_text(); ok = True
    def chk(n, c, extra=""):
        nonlocal ok; ok &= bool(c); print(("  PASS " if c else "  FAIL ") + n, extra)
    chk("server == local (main)", srv == loc, f"{len(srv)}/{len(loc)}")
    chk("server == local (bib)", st["sample.bib"] == B.read_text())
    if blen is not None:
        chk("browser == server", int(blen) == len(srv) and int(bhash) == fnv(srv), f"browser {blen}/{bhash} server {len(srv)}/{fnv(srv)}")
    toks = re.findall(r"@@[BL][a-z]*-?\d*@@", srv)
    chk("no duplicated token", len(toks) == len(set(toks)), str(sorted(toks)))
    stripped = TOK.sub("", srv)
    if name in ("typing", "sameline", "delete_none", "burst", "undo", "offline"):
        chk("only tokens added (rest == original)", stripped == ORIG, f"{len(stripped)} vs {len(ORIG)}")
    n = {"typing": 16, "sameline": 2, "burst": 150, "undo": 1, "offline": None}.get(name)
    if name == "typing": chk("8 L + 8 B tokens", sum(t[2] == "L" for t in toks) == 8 and sum(t[2] == "B" for t in toks) == 8, str(len(toks)))
    if name == "offline":
        chk("5 local + 4 browser tokens", sum(t[2] == "L" for t in toks) == 5 and sum(t[2] == "B" for t in toks) == 4, str(sorted(toks)))
    if name == "burst": chk("150 local lines present", sum(t[2] == "L" for t in toks) == 150, str(sum(t[2] == "L" for t in toks)))
    if name == "delete":
        chk("author line deleted, rest intact", stripped == ORIG.replace("\\author{You}\n", "", 1), f"{len(stripped)}")
        chk("4 local tokens", sum(t[2] == "L" for t in toks) == 4)
    if name == "reformat":
        want = "% header added locally\n" + "\n".join(l.rstrip() for l in ORIG.split("\n"))
        chk("local reformat applied + browser tokens kept", stripped == want, f"{len(stripped)} vs {len(want)}")
        chk("browser tokens", sum(t[2] == "B" for t in toks) == 4)
    if name == "unicode":
        for s in ["emoji 😀🚀 ", "CJK 漢字かな ", "combining éñ ", "RTL שלום ", "astral 𝔘𝔫𝔦 "]:
            chk("unicode kept: " + s.strip(), osy.sanitize_non_bmp(s) in srv)
        for s in ["🙂🎉 ", "Ελληνικά ", "한국어 ", "a\u0301\u0323 ", "𝟘𝟙𝟚 "]:
            chk("browser unicode kept: " + s.strip(), osy.sanitize_non_bmp(s) in srv)
        chk("original kept", subseq(ORIG, srv))
    if name == "bib":
        chk("6 local bib lines", st["sample.bib"].count("@@L") == 6)
    print(("RESULT PASS " if ok else "RESULT FAIL ") + name, flush=True)
    return ok


def restore():
    M.write_text(ORIG); B.write_text(ORIG_BIB)
    for _ in range(60):
        st = server_texts()
        if st["main.tex"] == ORIG and st["sample.bib"] == ORIG_BIB: print("restored on server"); return
        time.sleep(1)
    print("RESTORE FAILED", {k: len(v) for k, v in st.items()})


if __name__ == "__main__":
    c, a = sys.argv[1], sys.argv[2:]
    {"up": up, "down": down, "state": state, "restore": restore}.get(c, lambda: None)() if c in ("up", "down", "state", "restore") else (local(*a) if c == "local" else verify(*a))
