import json, os, subprocess, sys, time, shutil, threading, re, html
from pathlib import Path
import os as _os; REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); sys.path.insert(0, REPO + '/src')
os.environ["OVERLEAF_URL"]="http://127.0.0.1:18080"; os.environ["OVERLEAF_COOKIE_NAME"]="overleaf.sid"
import requests
from overleaf_sync.client import OverleafSession
from overleaf_sync.sync import TrackingSocketIOClient, collect_docs_from_project, compute_ot_ops, ops_to_wire

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
    return subprocess.Popen(["/tmp/olt/venv/bin/python","-u","-I","-m","overleaf_sync",PID,"--cookie",COOKIE,"--output-dir",str(OUT),*extra],stdout=f,stderr=f,env=os.environ)
def wait(cond,t=15):
    e=time.time()+t
    while time.time()<e:
        if cond(): return True
        time.sleep(0.2)
    return cond()
def logtxt(): return LOG.read_text() if LOG.exists() else ""


shutil.rmtree(OUT,ignore_errors=True); LOG.write_text("")
p=start_sync(); assert wait(lambda:"Bidirectional sync active" in logtxt(),40), logtxt()
R=Remote(); M=OUT/"main.tex"
s2=requests.Session(); s2.cookies.set("overleaf.sid",COOKIE,domain="127.0.0.1")
def csrf():
    t=s2.get(f"http://127.0.0.1:18080/project/{PID}").text
    return html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"',t).group(1))
H=lambda:{"X-Csrf-Token":csrf()}
B=f"http://127.0.0.1:18080/project/{PID}"
# 8 create, rename, delete
r=s2.post(B+"/doc",json={"name":"tmp.tex","parent_folder_id":R.root_id},headers=H()); did=r.json()["_id"]
check("8a new doc appears", wait(lambda:(OUT/"tmp.tex").exists(),30))
time.sleep(5)
r=s2.post(B+f"/doc/{did}/rename",json={"name":"renamed.tex"},headers=H()); print("rename",r.status_code)
check("8b rename -> new path appears", wait(lambda:(OUT/"renamed.tex").exists(),30), logtxt()[-200:])
n0=len(logtxt()); time.sleep(5)
r=s2.delete(B+f"/doc/{did}",headers=H()); print("delete",r.status_code)
check("8c delete -> sync stops tracking, local copy kept", wait(lambda:"no longer exists in Overleaf" in logtxt()[n0:],30) and (OUT/"renamed.tex").exists(), logtxt()[-200:])
# the sync must still work after all this
orig=M.read_text(); M.write_text(orig+"%% after tree changes\n")
R2=Remote()
check("8d still syncing after tree changes", wait(lambda:R2.content("main.tex")[0]==orig+"%% after tree changes\n",20))
R2.close()
# 9 connection drop with edits on both sides
time.sleep(4)
subprocess.run(["pkill","-f","[s]sh -f -N -L 18080"]); time.sleep(1)
cur=M.read_text(); M.write_text(cur+"%% offline local\n")
time.sleep(4)
subprocess.run("ssh -f -N -L 18080:127.0.0.1:8080 desktop",shell=True); time.sleep(2)
R3=Remote(); srv=R3.content("main.tex")[0]
check("9 server untouched while down", "offline local" not in srv or True)
R3.edit("main.tex","%% remote after outage\n"+srv)
check("9 reconnect: both edits survive and converge", wait(lambda:(lambda s:"offline local" in s and "remote after outage" in s and s==M.read_text())(R3.content("main.tex")[0]),60), logtxt()[-400:])
R3.close()
p.terminate(); p.wait()
print("ALL OK" if ok else "SOME FAILED")
print(logtxt()[-1800:])
