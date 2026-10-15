"""Run the language server's compile command against a THROW-AWAY project and look at the diagnostics.
It changes nothing in the project: Overleaf compiles whatever the project holds right now.
Usage: OVERLEAF_PROJECT=<id> python e2e/real_compile_check.py   (cookie from Firefox or OVERLEAF_COOKIE)"""
import json, os, shutil, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
for k in ("OVERLEAF_URL", "OVERLEAF_COOKIE_NAME"): os.environ.pop(k, None)
from overleaf_sync.lsp import path_to_uri

PID = os.environ["OVERLEAF_PROJECT"]
DIR = Path("/tmp/olt/ol_cmp"); LOG = Path("/tmp/olt/cmp_server.log")
ok = True


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
        s.p = subprocess.Popen(["overleaf-sync-lsp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(LOG, "a"),
                               env=dict(os.environ, PYTHONUNBUFFERED="1"))
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
            if "method" in msg and "id" in msg: s.send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            elif "method" in msg: s.messages.append(msg)
            elif msg.get("id") in s.waiting: s.waiting[msg["id"]][1] = msg; s.waiting[msg["id"]][0].set()

    def of(s, method): return [m["params"] for m in s.messages if m["method"] == method]


shutil.rmtree(DIR, ignore_errors=True); DIR.mkdir(parents=True); LOG.write_text("")
subprocess.run(["overleaf-sync", "init", PID, "--dir", str(DIR), "--yes", "--no-start"], check=True, capture_output=True, env=dict(os.environ))
c = Client()
try:
    r = c.request("initialize", {"processId": None, "rootUri": path_to_uri(DIR), "capabilities": {}})
    check("0 the server offers the compile command and a code action",
          "overleaf.compile" in r["result"]["capabilities"]["executeCommandProvider"]["commands"] and r["result"]["capabilities"]["codeActionProvider"])
    c.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    main = DIR / "main.tex"
    check("1 the project is downloaded", wait(lambda: main.exists(), 90))
    acts = c.request("textDocument/codeAction", {"textDocument": {"uri": path_to_uri(main)}, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "context": {"diagnostics": []}})
    check("2 code action 'compile' offered", acts["result"] and acts["result"][0]["command"]["command"] == "overleaf.compile")
    res = c.request("workspace/executeCommand", {"command": "overleaf.compile", "arguments": []})
    check("3 the command answers at once", res["result"] == {"compile": "started"}, str(res["result"]))
    check("4 a result message arrives", wait(lambda: any("compile" in m["message"] for m in c.of("window/showMessage")), 120),
          str([m["message"] for m in c.of("window/showMessage")]))
    pubs = c.of("textDocument/publishDiagnostics")
    print("   diagnostics:", [(Path(p["uri"]).name, [(d["severity"], d["range"]["start"]["line"] + 1, d["message"][:50]) for d in p["diagnostics"]]) for p in pubs][:6])
    msg = c.of("window/showMessage")[-1]["message"]
    print("   message:", msg)
    check("5 diagnostics are published for project files", any(Path(p["uri"]).name == "main.tex" and p["diagnostics"] for p in pubs) or "0 error(s), 0 warning(s)" in msg)
    pdf = DIR / ".overleaf-sync-build" / "output.pdf"
    check("6 the PDF was saved outside the synced files", pdf.exists() and pdf.read_bytes()[:4] == b"%PDF", str(pdf.stat().st_size if pdf.exists() else None))
    check("7 the log was saved", (DIR / ".overleaf-sync-build" / "output.log").exists())
    res2 = c.request("workspace/executeCommand", {"command": "overleaf.compile", "arguments": []})
    check("8 a second compile works", res2["result"] and "compile" in res2["result"] and wait(lambda: len(c.of("window/showMessage")) >= 2, 120))
finally:
    c.p.terminate()
print("ALL OK" if ok else "SOME FAILED")
