"""Drive `overleaf-sync-lsp` with a scripted LSP client (a simulated editor) against a THROW-AWAY project,
while a second connection plays the web editor / a coauthor.
Usage: OVERLEAF_PROJECT=<id> python e2e/real_lsp_check.py   (cookie from Firefox or OVERLEAF_COOKIE)"""
import json, os, random, shutil, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
if not os.environ.get("TREE_CHECK_KEEP_ENV"):
    for k in ("OVERLEAF_URL", "OVERLEAF_COOKIE_NAME"): os.environ.pop(k, None)
from overleaf_sync import client as overleaf_cli, sync as osy
from overleaf_sync.lsp import path_to_uri

PID = os.environ["OVERLEAF_PROJECT"]
COOKIE = osy.load_overleaf_cookie()
DIR = Path("/tmp/olt/ol_lsp"); LOG = Path("/tmp/olt/lsp_server.log")
RUN = str(int(time.time())); ok = True


def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, extra, flush=True)


def wait(c, t=40):
    e = time.time() + t
    while time.time() < e:
        try:
            if c(): return True
        except Exception: pass
        time.sleep(0.25)
    try: return bool(c())
    except Exception: return False


class Remote:
    """A second realtime connection with the same session: the web editor / a coauthor."""
    def __init__(s):
        sess = overleaf_cli.OverleafSession(COOKIE); sess.get_project_bootstrap(PID)
        s.ws = osy.TrackingSocketIOClient(sess, PID); s.ws.on("otUpdateApplied", lambda a: None)
        s.ws.connect(); s.ws.run_forever()
        s.docs = {v: k for k, v in osy.collect_docs_from_project(s.ws.join_project()).items()}
        s.id = s.docs["main.tex"]
    def text(s): return "\n".join(s.ws.join_doc(s.id)[0])
    def apply(s, ops):
        v = s.ws.join_doc(s.id)[1]; ev = threading.Event()
        s.ws.send_event("applyOtUpdate", [s.id, {"doc": s.id, "op": ops, "v": v}], callback=lambda d: ev.set()); ev.wait(15)
    def insert_line(s, tok, r):
        cur = s.text(); starts = [0] + [i + 1 for i, c in enumerate(cur) if c == "\n"]; pos = r.choice(starts)
        s.apply([{"p": len(cur[:pos].encode("utf-16-le")) // 2, "i": tok + "\n"}])
    def set_text(s, new):
        cur = s.text(); ops = osy.ops_to_wire(cur, osy.compute_ot_ops(cur, new))
        if ops: s.apply(ops)


class EditorSim:
    """An editor speaking LSP to the server over pipes: a buffer with versions, correct applyEdit handling."""
    def __init__(s, cmd, env):
        s.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(LOG, "a"), env=env)
        s.lock = threading.RLock(); s.next_id = 1; s.waiting = {}; s.text = {}; s.version = {}; s.messages = []
        s.applied = 0; s.rejected = 0; s.alive = True
        threading.Thread(target=s.reader, daemon=True).start()

    def send(s, msg):
        b = json.dumps(msg).encode(); s.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b); s.p.stdin.flush()

    def request(s, method, params, timeout=30):
        with s.lock:
            rid = s.next_id; s.next_id += 1; ev = threading.Event(); s.waiting[rid] = [ev, None]
        s.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}); ev.wait(timeout); return s.waiting.pop(rid)[1]

    def notify(s, method, params): s.send({"jsonrpc": "2.0", "method": method, "params": params})

    def reader(s):
        f = s.p.stdout
        while True:
            length = None
            while True:
                line = f.readline()
                if not line: s.alive = False; return
                line = line.strip()
                if not line: break
                if line.lower().startswith(b"content-length:"): length = int(line.split(b":")[1])
            msg = json.loads(f.read(length))
            if "method" in msg and "id" in msg: s.on_request(msg)
            elif "method" in msg: s.messages.append(msg)
            elif msg.get("id") in s.waiting: s.waiting[msg["id"]][1] = msg; s.waiting[msg["id"]][0].set()

    def on_request(s, msg):
        if msg["method"] != "workspace/applyEdit":
            s.send({"jsonrpc": "2.0", "id": msg["id"], "result": None}); return
        with s.lock:
            dc = msg["params"]["edit"]["documentChanges"][0]; uri = dc["textDocument"]["uri"]
            if dc["textDocument"]["version"] != s.version[uri]:
                s.rejected += 1; res = {"applied": False}
            else:
                for e in dc["edits"]: s.text[uri] = apply_edit(s.text[uri], e)
                s.version[uri] += 1; s.applied += 1; res = {"applied": True}
                new, ver = s.text[uri], s.version[uri]
        s.send({"jsonrpc": "2.0", "id": msg["id"], "result": res})
        if res["applied"]:
            time.sleep(0.15)                      # editors report the change shortly after (some after the response)
            with s.lock:                          # report what the buffer is NOW, in order with the user's own changes
                s.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": s.version[uri]}, "contentChanges": [{"text": s.text[uri]}]})

    def open(s, path):
        uri = path_to_uri(path)
        with s.lock: s.text[uri] = path.read_text(); s.version[uri] = 1
        s.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "latex", "version": 1, "text": s.text[uri]}}); return uri

    def type_at(s, uri, pos, text):
        with s.lock:
            s.text[uri] = s.text[uri][:pos] + text + s.text[uri][pos:]; s.version[uri] += 1
            s.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": s.version[uri]}, "contentChanges": [{"text": s.text[uri]}]})


def offset(text, pos):
    lines = text.split("\n"); o = sum(len(l) + 1 for l in lines[:pos["line"]]); u = 0
    for i, ch in enumerate(lines[pos["line"]]):
        if u >= pos["character"]: return o + i
        u += 2 if ord(ch) > 0xFFFF else 1
    return o + len(lines[pos["line"]])


def apply_edit(text, e):
    return text[:offset(text, e["range"]["start"])] + e["newText"] + text[offset(text, e["range"]["end"]):]


shutil.rmtree(DIR, ignore_errors=True); DIR.mkdir(parents=True); LOG.write_text("")
R = Remote(); ORIG = R.text()
subprocess.run(["overleaf-sync", "init", PID, "--dir", str(DIR), "--yes", "--no-start"], check=True, capture_output=True,
               env=dict(os.environ))
ed = EditorSim(["overleaf-sync-lsp"], dict(os.environ, PYTHONUNBUFFERED="1"))
try:
    r = ed.request("initialize", {"processId": None, "rootUri": path_to_uri(DIR), "capabilities": {}})
    check("0 initialize answers with full-text sync", r["result"]["capabilities"]["textDocumentSync"]["change"] == 1)
    ed.notify("initialized", {})
    main = DIR / "main.tex"
    check("1 the server downloads the project", wait(lambda: main.exists() and main.read_text() == ORIG, 90))
    time.sleep(4)
    uri = ed.open(main)

    tok = f"%% lsp typed {RUN}\n"
    ed.type_at(uri, 0, tok)
    check("2 a keystroke in the editor reaches overleaf.com (no save, no file write)", wait(lambda: tok in R.text(), 30))
    check("2b the file on disk was not touched", main.read_text() == ORIG)

    rtok = f"%% lsp remote {RUN}"
    R.insert_line(rtok, random.Random(1))
    check("3 a remote edit reaches the editor buffer as applyEdit", wait(lambda: rtok in ed.text[uri] and ed.applied >= 1, 30))
    check("3b buffer == overleaf.com", wait(lambda: ed.text[uri] == R.text(), 30))

    # simultaneous typing, human-ish rhythm, while the coauthor edits
    rr, rl = random.Random(5), random.Random(6); end = time.time() + 20; typed, remote = [], []
    def local_side():
        n = 0
        while time.time() < end:
            n += 1; t = f"<<L{RUN}-{n}>>"
            with ed.lock: cur = ed.text[uri]
            starts = [0] + [i + 1 for i, c in enumerate(cur) if c == "\n"]
            ed.type_at(uri, rl.choice(starts), t + "\n"); typed.append(t); time.sleep(rl.uniform(0.2, 0.7))
    def remote_side():
        n = 0
        while time.time() < end:
            n += 1; t = f"<<C{RUN}-{n}>>"; R.insert_line(t, rr); remote.append(t); time.sleep(rr.uniform(0.6, 1.4))
    ts = [threading.Thread(target=local_side), threading.Thread(target=remote_side)]; [t.start() for t in ts]; [t.join() for t in ts]
    check("4 simultaneous typing converges: buffer == server", wait(lambda: ed.text[uri] == R.text(), 60))
    fin = R.text(); bad = [t for t in typed + remote if fin.count(t) != 1]
    for b in bad[:3]:
        key = b.split("-")[-1]
        print("   diag", b, "count", fin.count(b), [l[:120] for l in fin.split("\n") if key in l][:4])
    check("4b every edit exactly once", not bad, f"{len(typed)} typed + {len(remote)} remote, rejected applyEdits={ed.rejected}, bad={bad[:3]}")
    check("4c the file on disk still untouched while the document is open", main.read_text() == ORIG)

    ed.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
    check("5 closing the document writes the final text to the file", wait(lambda: main.read_text() == R.text(), 30))
    st = ed.request("workspace/executeCommand", {"command": "overleaf.status", "arguments": []})
    check("6 status command answers", st and st["result"] and st["result"]["docs"] >= 1)
finally:
    try:
        ed.request("shutdown", {}, timeout=10); ed.notify("exit", {})
    except Exception: pass
    time.sleep(1); ed.p.terminate()
    R.set_text(ORIG)                                           # put the throw-away project back
    check("7 project restored", wait(lambda: R.text() == ORIG, 30))
print("ALL OK" if ok else "SOME FAILED")
print(LOG.read_text()[-1500:])
