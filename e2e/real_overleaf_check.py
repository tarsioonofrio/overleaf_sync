import os, sys, time, subprocess, threading, random, shutil
from pathlib import Path
sys.path.insert(0, "/home/tarsio/repos/overleaf_sync")
for k in ("OVERLEAF_URL", "OVERLEAF_COOKIE_NAME", "OVERLEAF_COOKIE"): os.environ.pop(k, None)
import overleaf_cli, overleaf_sync as osy
PID = os.environ["OVERLEAF_PROJECT"]   # a throw-away project you own
COOKIE = osy.load_overleaf_cookie()                    # from Firefox, never printed
ORIG = Path("/tmp/olt/main.original.tex").read_text()
OUT = Path("/tmp/olt/ol_real"); LOG = Path("/tmp/olt/real_sync.log")
RUN = str(int(time.time())); ok = True
def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, extra, flush=True)
def wait(c, t=25):
    e = time.time() + t
    while time.time() < e:
        try:
            if c(): return True
        except Exception: pass
        time.sleep(0.25)
    try: return bool(c())
    except Exception: return False

class Remote:
    """A second real-time connection with the same session: stands in for the web editor / a coauthor."""
    def __init__(s):
        sess = overleaf_cli.OverleafSession(COOKIE); sess.get_project_bootstrap(PID)
        s.ws = osy.TrackingSocketIOClient(sess, PID); s.ws.on("otUpdateApplied", lambda a: None)
        s.ws.connect(); s.ws.run_forever()
        s.docs = {v: k for k, v in osy.collect_docs_from_project(s.ws.join_project()).items()}
    def text(s, p="main.tex"): return "\n".join(s.ws.join_doc(s.docs[p])[0])
    def insert_line(s, tok, r, p="main.tex"):
        cur = s.text(p); v = s.ws.join_doc(s.docs[p])[1]
        starts = [0] + [i + 1 for i, c in enumerate(cur) if c == "\n"]; pos = r.choice(starts)
        ev = threading.Event()
        s.ws.send_event("applyOtUpdate", [s.docs[p], {"doc": s.docs[p], "op": [{"p": len(cur[:pos].encode("utf-16-le")) // 2, "i": tok + "\n"}], "v": v}], callback=lambda d: ev.set())
        ev.wait(15)
    def close(s): s.ws.disconnect()

def start_sync(extra=()):
    f = open(LOG, "a")
    return subprocess.Popen(["overleaf-sync", PID, "--output-dir", str(OUT), *extra], stdout=f, stderr=f, env=dict(os.environ, PYTHONUNBUFFERED="1"))
def logtxt(): return LOG.read_text() if LOG.exists() else ""

shutil.rmtree(OUT, ignore_errors=True); LOG.write_text("")
R = Remote()
check("server starts with the original content", R.text() == ORIG, f"{len(R.text())} chars")
p = start_sync()
assert wait(lambda: "Bidirectional sync active" in logtxt(), 60), logtxt()[-600:]
time.sleep(4)
M = OUT / "main.tex"
check("initial local == server", M.read_text() == R.text())

# 1 local -> server
M.write_text(M.read_text() + f"%% real-test local edit {RUN}\n")
check("1 local edit reaches overleaf.com", wait(lambda: f"real-test local edit {RUN}" in R.text()))
# 2 server -> local
R.insert_line(f"%% real-test remote edit {RUN}", random.Random(1))
check("2 remote edit reaches the local file", wait(lambda: f"real-test remote edit {RUN}" in M.read_text()))
check("2 local == server", wait(lambda: M.read_text() == R.text()))

# 3 concurrent edits, moderate rate
r_l, r_r = random.Random(2), random.Random(3); end = time.time() + 25; lt, rt = [], []
def local_side():
    n = 0
    while time.time() < end:
        n += 1; tok = f"<<RL-{RUN}-{n}>>"; cur = M.read_text()
        starts = [0] + [i + 1 for i, c in enumerate(cur) if c == "\n"]; pos = r_l.choice(starts)
        tmp = OUT / ".swp"; tmp.write_text(cur[:pos] + tok + "\n" + cur[pos:]); os.replace(tmp, M); lt.append(tok); time.sleep(r_l.uniform(0.8, 1.6))
def remote_side():
    n = 0
    while time.time() < end:
        n += 1; tok = f"<<RR-{RUN}-{n}>>"; R.insert_line(tok, r_r); rt.append(tok); time.sleep(r_r.uniform(0.6, 1.4))
ts = [threading.Thread(target=local_side), threading.Thread(target=remote_side)]; [t.start() for t in ts]; [t.join() for t in ts]
check("3 concurrent: local == server", wait(lambda: M.read_text() == R.text(), 40))
fin = R.text(); lost = [t for t in lt + rt if fin.count(t) != 1]
check("3 concurrent: every edit exactly once", not lost, f"{len(lt)} local + {len(rt)} remote, bad={lost[:4]}")

# 4 truncate + rewrite (a non-atomic save): the server must never be emptied
mins = [len(R.text())]; stop = [False]
def watch():
    while not stop[0]: mins.append(len(R.text())); time.sleep(0.4)
th = threading.Thread(target=watch); th.start(); good = M.read_text()
for i in range(3):
    with open(M, "w"): pass
    time.sleep(0.5); good += f"%% t{i}\n"; M.write_text(good); time.sleep(0.8)
time.sleep(5); stop[0] = True; th.join()
check("4 truncate/rewrite never emptied the server", min(mins) > len(good) * 0.5, f"min={min(mins)} final={len(good)}")
check("4 server == local after", wait(lambda: R.text() == M.read_text(), 30))

# 5 a wipe is held (not sent) and cancelled before the hold ends
keep = M.read_text(); M.write_text(""); time.sleep(3.0)
check("5 wipe held: server intact after 3 s", len(R.text()) > 1000)
M.write_text(keep); time.sleep(4)
check("5 wipe cancelled: nothing was deleted on the server", R.text() == keep)

# 6 offline edit -> backup on restart
p.terminate(); p.wait(); time.sleep(1)
M.write_text("OFFLINE EDIT, not on the server\n"); server_before = R.text()
n0 = len(logtxt()); p = start_sync(); wait(lambda: "Bidirectional sync active" in logtxt()[n0:], 60); time.sleep(2)
bk = list(OUT.glob(".overleaf-sync-backup/*/main.tex"))
check("6 offline edit backed up on restart", bk and bk[-1].read_text() == "OFFLINE EDIT, not on the server\n")
check("6 local restored from the server, server untouched", M.read_text() == server_before == R.text())

# 7 restore the original content on the server through the sync
M.write_text(ORIG)
check("7 original content restored on overleaf.com", wait(lambda: R.text() == ORIG, 90), f"{len(R.text())} vs {len(ORIG)}")
p.terminate(); p.wait(); R.close()
print("ALL OK" if ok else "SOME FAILED")
print(logtxt()[-1800:])
