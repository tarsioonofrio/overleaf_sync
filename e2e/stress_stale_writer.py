#!/usr/bin/env python3
"""
In-process stress (no network): remote ops are merged into the local file while a
'stale writer' reads the file, waits, and writes it back whole.  Counts tokens that
were lost or duplicated.  Usage: stress_stale_writer.py [seconds] [seed]
"""
import os, random, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import overleaf_sync as o

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 15
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
out = Path(tempfile.mkdtemp()); base = "\n".join(f"line {i} of the document" for i in range(120))
(out / "main.tex").write_text(base)
s = o.OverleafSync("p", "c", str(out)); s._docs["d1"] = {"content": base, "version": 0, "path": "main.tex"}
server_lock = threading.Lock(); stop = False
ltoks, rtoks, v = [], [], [0]
def line_starts(t): return [0] + [i + 1 for i, c in enumerate(t) if c == "\n"]

def writer():
    r = random.Random(seed); n = 0
    while not stop:
        n += 1; tok = f"<<L{n}>>"
        cur = (out / "main.tex").read_text(); time.sleep(r.choice([0.002, 0.01, 0.04]))
        pos = r.choice(line_starts(cur)); new = cur[:pos] + tok + "\n" + cur[pos:]
        if n % 2:
            with open(out / "main.tex", "w") as f: f.write(new)
        else:
            tmp = out / ".swp"; tmp.write_text(new); os.replace(tmp, out / "main.tex")
        ltoks.append(tok); time.sleep(r.uniform(0.02, 0.15))

def feeder():
    r = random.Random(seed + 1); n = 0
    while not stop:
        n += 1; tok = f"<<R{n}>>"
        with server_lock:
            content = s._docs["d1"]["content"]; pos = r.choice(line_starts(content))
            s._on_ot_update([{"doc": "d1", "v": v[0], "op": [{"p": pos, "i": tok + "\n"}], "meta": {"source": "other"}}])
            v[0] += 1; rtoks.append(tok)
        time.sleep(r.uniform(0.02, 0.12))

def poller():
    while not stop:
        with server_lock:
            for doc_id, old, new in s._settled(s._collect_local_changes()):
                st = s._docs[doc_id]
                if st["content"] == old:                 # "push": the server accepts it at once
                    st["content"] = new; v[0] += 1
        time.sleep(0.25)

ts = [threading.Thread(target=f) for f in (writer, feeder, poller)]
[t.start() for t in ts]; time.sleep(secs); stop = True; [t.join() for t in ts]
time.sleep(1.5)
with server_lock:
    for _ in range(4):                                    # let pending pushes settle
        for doc_id, old, new in s._collect_local_changes():
            s._docs[doc_id]["content"] = new
        time.sleep(0.6)
final = (out / "main.tex").read_text()
lost = [t for t in ltoks + rtoks if final.count(t) == 0]
dup = [t for t in ltoks + rtoks if final.count(t) > 1]
print(f"writes={len(ltoks)} remote={len(rtoks)} lost={len(lost)} (remote {sum(t[2]=='R' for t in lost)}, local {sum(t[2]=='L' for t in lost)}) dup={len(dup)} {dup[:6]}")
sys.exit(1 if lost or dup else 0)
