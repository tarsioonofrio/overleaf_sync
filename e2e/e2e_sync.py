import json, os, subprocess, sys, time, shutil, threading, re, html
from pathlib import Path
sys.path.insert(0, "/home/tarsio/repos/overleaf_sync")
os.environ["OVERLEAF_URL"]="http://127.0.0.1:18080"; os.environ["OVERLEAF_COOKIE_NAME"]="overleaf.sid"
import requests
from overleaf_cli import OverleafSession
from overleaf_sync import TrackingSocketIOClient, collect_docs_from_project, compute_ot_ops, ops_to_wire

st=json.load(open("/tmp/olt/state.json")); PID=st["project"]; COOKIE=st["cookie"]["overleaf.sid"]
OUT=Path("/tmp/olt/out"); LOG=Path("/tmp/olt/sync.log")
ok=True
def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ")+name, extra, flush=True)

class Remote:
    """Browser-like second client."""
    def __init__(s):
        s.sess=OverleafSession(COOKIE); s.sess.get_project_bootstrap(PID)
        s.ws=TrackingSocketIOClient(s.sess,PID); s.ws.on("otUpdateApplied",lambda a:None)
        s.ws.connect(); s.ws.run_forever()
        pd=s.ws.join_project(); s.root_id=pd["rootFolder"][0]["_id"]; s.docs=collect_docs_from_project(pd); s.by={v:k for k,v in s.docs.items()}
        s.events=[]
    def content(s,path):
        lines,v=s.ws.join_doc(s.by[path]); return "\n".join(lines),v
    def edit(s,path,new):
        old,v=s.content(path); ops=ops_to_wire(old,compute_ot_ops(old,new))
        ev=threading.Event(); r=[]
        s.ws.send_event("applyOtUpdate",[s.by[path],{"doc":s.by[path],"op":ops,"v":v}],callback=lambda d:(r.append(d),ev.set()))
        ev.wait(10); return r
    def close(s): s.ws.disconnect()

def start_sync(extra=()):
    f=open(LOG,"a")
    return subprocess.Popen(["/tmp/olt/venv/bin/python","-u","-I","/home/tarsio/repos/overleaf_sync/overleaf_sync.py",PID,"--cookie",COOKIE,"--output-dir",str(OUT),*extra],stdout=f,stderr=f,env=os.environ)
def wait(cond,t=15):
    e=time.time()+t
    while time.time()<e:
        if cond(): return True
        time.sleep(0.2)
    return cond()
def logtxt(): return LOG.read_text() if LOG.exists() else ""

shutil.rmtree(OUT,ignore_errors=True); LOG.write_text("")
p=start_sync()
assert wait(lambda:"Bidirectional sync active" in logtxt(),40), logtxt()
R=Remote(); M=OUT/"main.tex"
orig,_=R.content("main.tex"); check("initial local==server", M.read_text()==orig)

# 1 local -> server
M.write_text(orig+"\n%% local edit 1\n")
check("1 local edit reaches server", wait(lambda:R.content("main.tex")[0]==orig+"\n%% local edit 1\n"))
cur=M.read_text()
# 2 remote -> local
R.edit("main.tex","%% remote head\n"+cur)
check("2 remote edit reaches local", wait(lambda:M.read_text()=="%% remote head\n"+cur))
cur=M.read_text()
# 3 concurrent: local appends, remote prepends, near-simultaneous
M.write_text(cur+"%% L\n"); R.edit("main.tex","%% R\n"+cur)
exp_ok=lambda: (lambda s:("%% L" in s and "%% R" in s))(R.content("main.tex")[0]) and M.read_text()==R.content("main.tex")[0]
check("3 concurrent edits converge, both kept", wait(exp_ok,20), repr(R.content("main.tex")[0][:40]))
good=M.read_text()
# 4 non-atomic save: truncate then rewrite; server must NEVER be empty/short
mins=[len(good)]; stop=[False]
def watch():
    while not stop[0]:
        mins.append(len(R.content("main.tex")[0])); time.sleep(0.15)
th=threading.Thread(target=watch); th.start()
for i in range(5):
    with open(M,"w") as f: pass          # truncate
    time.sleep(0.4)                       # editor "slow" between truncate and write
    M.write_text(good+f"%% t{i}\n"); good=good+f"%% t{i}\n"; time.sleep(0.3)
time.sleep(3); stop[0]=True; th.join()
check("4 truncate/rewrite never emptied server", min(mins)>len(good)*0.5, f"min={min(mins)} final={len(good)}")
check("4 server final == local", wait(lambda:R.content("main.tex")[0]==M.read_text()))
# 5 an emptied file is never pushed on its own; the user confirms with a sentinel file
M.write_text(""); time.sleep(12)
check("5 wipe held (server intact after 12s, hold is 5s)", len(R.content("main.tex")[0])>100)
check("5 the sync says how to confirm", "confirm with: touch" in logtxt())
(OUT/".overleaf-sync-confirm-wipe").touch()
check("5 wipe pushed after confirmation", wait(lambda:R.content("main.tex")[0]=="",20))
check("5 sentinel consumed", not (OUT/".overleaf-sync-confirm-wipe").exists())
M.write_text(good); wait(lambda:R.content("main.tex")[0]==good,15)
# 6 edits on both sides while stopped are merged on restart
p.terminate(); p.wait(); time.sleep(1)
M.write_text(good+"%% OFFLINE LOCAL\n"); sc=R.content("main.tex")[0]; R.edit("main.tex","%% OFFLINE REMOTE\n"+sc)
n0=len(logtxt()); p=start_sync(); wait(lambda:"Bidirectional sync active" in logtxt()[n0:],40)
both=lambda t:"%% OFFLINE LOCAL" in t and "%% OFFLINE REMOTE" in t
check("6 both offline edits reach the server", wait(lambda:both(R.content("main.tex")[0]),30))
check("6 local == server, each edit once", wait(lambda:M.read_text()==R.content("main.tex")[0],20) and M.read_text().count("OFFLINE LOCAL")==1 and M.read_text().count("OFFLINE REMOTE")==1)
bk=list(OUT.glob(".overleaf-sync-backup/*/main.tex"))
check("6 pre-merge local file kept in backup", any("OFFLINE LOCAL" in b.read_text() for b in bk))
# 7 new doc created in Overleaf
s2=requests.Session(); s2.cookies.set("overleaf.sid",COOKIE,domain="127.0.0.1")
t=s2.get(f"http://127.0.0.1:18080/project/{PID}").text
csrf=html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"',t).group(1))
rid=R.root_id
if rid:
    r=s2.post(f"http://127.0.0.1:18080/project/{PID}/doc",json={"name":"newdoc.tex","parent_folder_id":rid},headers={"X-Csrf-Token":csrf})
    print("create doc http",r.status_code,r.text[:80])
    check("7 tree refresh sees new doc", wait(lambda:(OUT/"newdoc.tex").exists(),30), logtxt()[-300:])
else:
    print("SKIP 7: no rootFolder meta")
p.terminate(); p.wait()
print("ALL OK" if ok else "SOME FAILED")
print("---- sync log tail ----"); print(logtxt()[-2500:])
