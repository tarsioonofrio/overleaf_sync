"""Create / rename / move / delete local files against a THROW-AWAY project and check Overleaf follows.
Usage: OVERLEAF_PROJECT=<id> python e2e/real_tree_check.py   (cookie from Firefox or OVERLEAF_COOKIE)"""
import os, sys, time, subprocess, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("OVERLEAF_URL", "OVERLEAF_COOKIE_NAME"): os.environ.pop(k, None)
import overleaf_cli, overleaf_sync as osy

PID = os.environ["OVERLEAF_PROJECT"]
COOKIE = osy.load_overleaf_cookie()
OUT = Path("/tmp/olt/ol_tree"); LOG = Path("/tmp/olt/tree_sync.log")
RUN = str(int(time.time())); ok = True


def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name, extra, flush=True)


def wait(c, t=40):
    e = time.time() + t
    while time.time() < e:
        try:
            if c(): return True
        except Exception: pass
        time.sleep(3)
    return False


_conn = {}


def server():
    """({path: (id, text)}, folders {path: id}, all entity paths) as the realtime API sees them.
    The web session is reused (a project-page fetch per poll gets this client rate-limited, HTTP 429);
    joinProject answers once per socket, so each call opens a fresh one."""
    if "sess" not in _conn:
        s = overleaf_cli.OverleafSession(COOKIE); s.get_project_bootstrap(PID); _conn["sess"] = s
    ws = osy.TrackingSocketIOClient(_conn["sess"], PID); ws.on("otUpdateApplied", lambda a: None)
    ws.connect(); ws.run_forever()
    try:
        data = ws.join_project()
        docs = {v: k for k, v in osy.collect_docs_from_project(data).items()}
        texts = {p: (i, "\n".join(ws.join_doc(i)[0])) for p, i in docs.items()}
        folders, paths = osy.collect_tree(data)
    finally:
        ws.disconnect()
    return texts, folders, paths


def logtxt(): return LOG.read_text() if LOG.exists() else ""


shutil.rmtree(OUT, ignore_errors=True); LOG.write_text("")
before, _, _ = server()
proc = subprocess.Popen(["overleaf-sync", PID, "--output-dir", str(OUT)], stdout=open(LOG, "a"), stderr=subprocess.STDOUT,
                        env=dict(os.environ, PYTHONUNBUFFERED="1"))
try:
    assert wait(lambda: "Bidirectional sync active" in logtxt(), 90), logtxt()[-500:]
    time.sleep(4)
    name, name2, folder = f"treetest-{RUN}.tex", f"treetest-{RUN}-renamed.tex", f"treetest-dir-{RUN}"
    text = f"%% created locally {RUN}\nhello tree\n"

    (OUT / name).write_text(text)
    check("1 new local file appears in Overleaf with its text", wait(lambda: server()[0].get(name, (0, None))[1] == text, 60))
    check("1b original docs untouched", all(server()[0][p][1] == before[p][1] for p in before))

    (OUT / name).rename(OUT / name2)
    check("2 rename is a rename (same doc id, old name gone)",
          wait(lambda: name2 in server()[0] and name not in server()[0]) and server()[0][name2][1] == text)

    (OUT / folder).mkdir(); (OUT / name2).rename(OUT / folder / name2)
    check("3 move into a new folder creates the folder and moves the doc",
          wait(lambda: f"{folder}/{name2}" in server()[0]) and name2 not in server()[0])

    # edits still flow after all of that (the doc id survived)
    p = OUT / folder / name2; p.write_text(text + "after the move\n")
    check("4 editing the moved file still syncs", wait(lambda: server()[0].get(f"{folder}/{name2}", (0, ""))[1] == text + "after the move\n"))

    p.unlink()
    time.sleep(12)
    check("5 a deleted file is NOT deleted in Overleaf without confirmation", f"{folder}/{name2}" in server()[0])
    check("5b the warning shows the confirm command", "touch " in logtxt() and "NOT changing them in Overleaf" in logtxt())
    (OUT / ".overleaf-sync-confirm-wipe").touch()
    check("6 after `touch` the doc is deleted", wait(lambda: f"{folder}/{name2}" not in server()[0], 60))
    check("6b a copy of the deleted doc is in the backup", any(OUT.glob(f".overleaf-sync-backup/*/deleted-docs/{folder}/{name2}")))
    # moved AND edited at once: still one doc (same id), with the edit
    nb, nc = f"treetest-{RUN}-b.tex", f"treetest-{RUN}-c.tex"
    body = "".join(f"line {i} of the test chapter\n" for i in range(30))
    (OUT / nb).write_text(body)
    check("8 second file created", wait(lambda: server()[0].get(nb, (0, None))[1] == body, 60))
    ident = server()[0][nb][0]
    (OUT / nb).unlink(); (OUT / nc).write_text(body.replace("line 7 of", "LINE SEVEN of"))
    check("8b moved+edited: same doc id under the new name, with the edit",
          wait(lambda: nc in server()[0] and server()[0][nc][1] == body.replace("line 7 of", "LINE SEVEN of"), 90)
          and server()[0][nc][0] == ident and nb not in server()[0])
    (OUT / nc).unlink(); time.sleep(8); (OUT / ".overleaf-sync-confirm-wipe").touch()
    check("8c cleanup delete of the second file", wait(lambda: nc not in server()[0], 60))
    after, _, _ = server()
    check("7 original docs identical at the end", all(after[p][1] == before[p][1] for p in before) and set(after) == set(before), str(sorted(set(after) ^ set(before))))
finally:
    proc.terminate(); proc.wait()
    # remove the leftover empty folder through the web API (the tool never deletes folders)
    try:
        _, folders, _ = server()
        s = _conn["sess"]
        for pth, fid in folders.items():
            if pth.startswith("treetest-dir-"):
                s._tree_request("DELETE", PID, f"folder/{fid}"); print("cleaned folder", pth)
    except Exception as e:
        print("cleanup problem:", e)
print("ALL OK" if ok else "SOME FAILED")
print(logtxt()[-2500:])
