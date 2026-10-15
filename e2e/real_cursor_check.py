"""Cursor sharing against a THROW-AWAY project: our cursor must show up for other connections, theirs as inlay hints for us.
A browser tab with the project open (same account) plays the other user: click somewhere in its editor before running.
Usage: OVERLEAF_PROJECT=<id> python e2e/real_cursor_check.py   (cookie from Firefox or OVERLEAF_COOKIE)"""
import json, os, shutil, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
for k in ("OVERLEAF_URL", "OVERLEAF_COOKIE_NAME"): os.environ.pop(k, None)
from overleaf_sync import client as overleaf_cli, sync as osy
from overleaf_sync.lsp import path_to_uri

PID = os.environ["OVERLEAF_PROJECT"]
DIR = Path("/tmp/olt/ol_cur"); LOG = Path("/tmp/olt/cur_server.log"); ok = True
ROW, COL = 12, 4


def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, extra, flush=True)


def wait(c, t=60):
    e = time.time() + t
    while time.time() < e:
        try:
            if c(): return True
        except Exception: pass
        time.sleep(0.25)
    return False


class Client:
    def __init__(s):
        s.p = subprocess.Popen(["overleaf-sync-lsp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(LOG, "a"), env=dict(os.environ, PYTHONUNBUFFERED="1"))
        s.next_id, s.waiting, s.messages = 1, {}, []
        threading.Thread(target=s.reader, daemon=True).start()
    def send(s, msg):
        b = json.dumps(msg).encode(); s.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b); s.p.stdin.flush()
    def request(s, method, params, timeout=60):
        rid = s.next_id; s.next_id += 1; ev = threading.Event(); s.waiting[rid] = [ev, None]
        s.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}); ev.wait(timeout); return s.waiting.pop(rid)[1]
    def reader(s):
        f = s.p.stdout
        while True:
            length = None
            while True:
                line = f.readline()
                if not line: return
                line = line.strip()
                if not line: break
                if line.lower().startswith(b"content-length:"): length = int(line.split(b":")[1])
            msg = json.loads(f.read(length))
            if "method" in msg and "id" in msg: s.messages.append(msg); s.send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            elif "method" in msg: s.messages.append(msg)
            elif msg.get("id") in s.waiting: s.waiting[msg["id"]][1] = msg; s.waiting[msg["id"]][0].set()


def connected_users():
    sess = overleaf_cli.OverleafSession(osy.load_overleaf_cookie()); sess.get_project_bootstrap(PID)
    ws = osy.TrackingSocketIOClient(sess, PID); ws.connect(); ws.run_forever(); ws.join_project()
    box, ev = [], threading.Event()
    ws.send_event("clientTracking.getConnectedUsers", [], callback=lambda d: (box.append(d), ev.set())); ev.wait(10); ws.disconnect()
    return box[0][1] if box else []


shutil.rmtree(DIR, ignore_errors=True); DIR.mkdir(parents=True); LOG.write_text("")
subprocess.run(["overleaf-sync", "init", PID, "--dir", str(DIR), "--yes", "--no-start"], check=True, capture_output=True, env=dict(os.environ))
c = Client()
try:
    c.request("initialize", {"processId": None, "rootUri": path_to_uri(DIR), "capabilities": {"workspace": {"inlayHint": {"refreshSupport": True}}}})
    c.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    main = DIR / "main.tex"
    check("1 the project is downloaded", wait(lambda: main.exists(), 90)); time.sleep(4)
    uri = path_to_uri(main); text = main.read_text()
    c.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": "latex", "version": 1, "text": text}}})
    r = c.request("textDocument/documentHighlight", {"textDocument": {"uri": uri}, "position": {"line": ROW, "character": COL}})
    check("2 documentHighlight is answered", r["result"] == [])
    check("3 other connections see our cursor", wait(lambda: any(u.get("cursorData", {}).get("row") == ROW and u["cursorData"].get("column") == COL for u in connected_users()), 20))
    hints = []
    def got_hints():
        global hints
        hints = c.request("textDocument/inlayHint", {"textDocument": {"uri": uri}, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 9999, "character": 0}}})["result"]
        return bool(hints)
    check("4 we get inlay hints for the other (browser) cursor", wait(got_hints, 30), json.dumps(hints)[:300])
    check("5 our own cursor is not among them", all(not (h["position"] == {"line": ROW, "character": COL}) for h in hints))
    check("6 the editor was asked to refresh hints", any(m.get("method") == "workspace/inlayHint/refresh" for m in c.messages))
finally:
    c.p.terminate()
print("ALL OK" if ok else "SOME FAILED")
