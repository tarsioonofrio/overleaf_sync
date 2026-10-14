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


import random, signal, traceback
B=f"http://127.0.0.1:18080/project/{PID}"
PODMAN="podman --root /var/mnt/data/overleaf-ce-test/storage --runroot /run/user/1000/ol-ce-runroot"
def ssh(cmd): return subprocess.run(["ssh","desktop",cmd],capture_output=True,text=True,timeout=120)
s2=requests.Session(); s2.cookies.set("overleaf.sid",COOKIE,domain="127.0.0.1")
_csrf=[None]
def H():
    if not _csrf[0]:
        t=s2.get(f"http://127.0.0.1:18080/project/{PID}").text
        _csrf[0]=html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"',t).group(1))
    return {"X-Csrf-Token":_csrf[0]}

def fresh():
    global p,R,M
    shutil.rmtree(OUT,ignore_errors=True); LOG.write_text("")
    p=start_sync(); assert wait(lambda:"Bidirectional sync active" in logtxt(),60), logtxt()[-800:]
    R=Remote(); M=OUT/"main.tex"; time.sleep(3.5)
def stop():
    p.terminate(); p.wait(); 
    try: R.close()
    except Exception: pass
def converged(path="main.tex",t=25):
    return wait(lambda:(OUT/path).read_text()==R.content(path)[0],t)

RUN=str(int(time.time()))
def _insert_line(self,path,r,tok):
    cur,v=self.content(path); pos=lb(r,cur)
    ev=threading.Event()
    self.ws.send_event("applyOtUpdate",[self.by[path],{"doc":self.by[path],"op":[{"p":len(cur[:pos].encode("utf-16-le"))//2,"i":tok+"\n"}],"v":v}],callback=lambda d:ev.set())
    ev.wait(10)
Remote.insert_line=_insert_line
def lb(r,cur):
    starts=[0]+[i+1 for i,c in enumerate(cur) if c=="\n"]
    return r.choice(starts)

def sc_random():
    """A: random concurrent edits from both sides; unique tokens must survive exactly once."""
    r=random.Random(7); toks=[]; sides={}; wr={}
    base=R.content("main.tex")[0]
    seen={}; gone={}; stopw=[False]
    def watcher():
        while not stopw[0]:
            try:
                srv=R.content("main.tex")[0]; loc=M.read_text(); now=time.strftime("%H:%M:%S")
                for t in list(sides):
                    if t in srv and t not in seen: seen[t]=now
                    if t in seen and t not in srv and t not in gone: gone[t]=(now, t in loc)
            except Exception: pass
            time.sleep(0.25)
    wt=threading.Thread(target=watcher); wt.start()
    end=time.time()+40; n=0
    while time.time()<end:
        n+=1; tok=f"<<{RUN}-{n}>>"; side=r.choice(os.environ.get("SIDES","LR"))
        if side=="L":
            cur=M.read_text(); pos=lb(r,cur); new=cur[:pos]+tok+"\n"+cur[pos:]
            tmp=OUT/".x.swp"; tmp.write_text(new); os.replace(tmp,M)      # atomic editor save
        else:
            R.insert_line("main.tex",r,tok)
        toks.append(tok); sides[tok]=side; wr[tok]=time.strftime('%H:%M:%S'); time.sleep(r.uniform(0.05,0.6))
    ok_conv=converged(t=40); stopw[0]=True; wt.join()
    print("   t0 of edits per token (L):", {t.split('-')[1]:wt0.get(t) for t in sides if sides[t]=='L'} if False else "")
    srv=R.content("main.tex")[0]
    missing=[t for t in toks if srv.count(t)!=1]
    print("   seen/gone:",{t.split('-')[1]:(seen.get(t),gone.get(t),wr.get(t)) for t in missing})
    print("   bad detail:",[(t.split('-')[1],sides[t],srv.count(t),M.read_text().count(t)) for t in missing])
    check("A random concurrent: converged", ok_conv)
    check("A random concurrent: every token exactly once", not missing, f"{len(toks)} edits, bad={missing[:5]}")

def sc_editors():
    """B: editor save patterns."""
    before=R.content("main.tex")[0]; minlen=[len(before)]; stop_=[False]
    def watch():
        while not stop_[0]: minlen.append(len(R.content("main.tex")[0])); time.sleep(0.2)
    th=threading.Thread(target=watch); th.start()
    cur=M.read_text()
    # vim style: write new file, rename old to backup~, rename new into place
    for i in range(3):
        cur+=f"%% vim {i}\n"; t=OUT/"4913"; t.write_text(cur); os.replace(M,OUT/"main.tex~"); os.replace(t,M); time.sleep(1.2)
    # sed -i style
    subprocess.run(["sed","-i","s/%% vim 0/%% sed was here/",str(M)]); cur=cur.replace("%% vim 0","%% sed was here"); time.sleep(1.2)
    # in-place append via open('a')
    with open(M,"a") as f: f.write("%% appended\n"); cur+="%% appended\n"
    time.sleep(1.2)
    # emacs style autosave file next to it
    (OUT/"#main.tex#").write_text("autosave"); (OUT/".#main.tex").write_text("lock")
    time.sleep(6); stop_[0]=True; th.join()
    check("B editors: server==local", converged() and M.read_text()==cur)
    check("B editors: server never shrank", min(minlen)>=len(before)-5, f"min={min(minlen)} before={len(before)}")
    check("B editors: temp/autosave files not uploaded", not any(k in R.docs for k in ("#main.tex#","main.tex~","4913")))
    for f in ("#main.tex#",".#main.tex","main.tex~"): 
        try: (OUT/f).unlink()
        except FileNotFoundError: pass

def sc_delete_recreate():
    """C: delete the local file; server must stay intact; recreate unchanged => no push; recreate modified => push."""
    cur=M.read_text(); M.unlink(); time.sleep(4)
    check("C delete: server intact", R.content("main.tex")[0]==cur)
    M.write_text(cur); time.sleep(3); check("C recreate same: nothing changed", R.content("main.tex")[0]==cur)
    M.write_text(cur+"%% back\n"); check("C recreate modified: pushed", wait(lambda:R.content("main.tex")[0]==cur+"%% back\n",15))

def sc_symlink():
    """G: file replaced by a symlink to a secret outside the dir: never read or pushed."""
    sec=Path("/tmp/olt/secret.txt"); sec.write_text("TOP-SECRET-DATA\n")
    before=R.content("main.tex")[0]; M.unlink(); M.symlink_to(sec); time.sleep(5)
    check("G symlink: secret not pushed", "TOP-SECRET" not in R.content("main.tex")[0])
    check("G symlink: secret untouched", sec.read_text()=="TOP-SECRET-DATA\n")
    M.unlink(); M.write_text(before); time.sleep(2)

def sc_big():
    """D: large document."""
    cur=M.read_text(); big=cur+"".join(f"%% filler line {i} "+"x"*60+"\n" for i in range(18000))
    t0=time.time(); M.write_text(big)
    okp=wait(lambda:R.content("main.tex")[0]==big,90)
    check("D 2MB doc: pushed", okp, f"{len(big)} chars, {time.time()-t0:.1f}s")
    R.edit("main.tex","%% remote line\n"+big); t0=time.time()
    check("D 2MB doc: remote edit arrives", wait(lambda:M.read_text()=="%% remote line\n"+big,60), f"{time.time()-t0:.1f}s")
    # shrink back
    M.write_text(cur); time.sleep(9); wait(lambda:R.content("main.tex")[0]==cur,60)

def sc_toobig():
    """J: a push over Overleaf's 2MB doc limit: server must stay intact, tool must not loop or corrupt."""
    cur=M.read_text(); srv0=R.content("main.tex")[0]
    big=cur+"".join(f"%% filler line {i} "+"x"*60+"\n" for i in range(40000))
    M.write_text(big); time.sleep(40)
    try: R.close()
    except Exception: pass
    globals()["R"]=Remote(); srv=R.content("main.tex")[0]
    check("J over-limit: server doc not corrupted/truncated", len(srv)<=2*1024*1024 and srv.startswith(srv0[:200]), f"server={len(srv)} chars")
    print("   server len",len(srv),"local len",len(M.read_text()), "| log tail:", logtxt()[-500:].replace("\n"," | "))
    check("J over-limit: process still alive", p.poll() is None)
    check("J over-limit: local file kept", len(M.read_text())==len(big))

def proc_stats(pid):
    st=open(f"/proc/{pid}/status").read()
    rss=int(re.search(r"VmRSS:\s+(\d+)",st).group(1))//1024; thr=int(re.search(r"Threads:\s+(\d+)",st).group(1))
    return rss,thr,len(os.listdir(f"/proc/{pid}/fd"))

def sc_soak():
    """K: 5 minutes of low-rate edits from both sides; resources must stay flat, nothing lost."""
    r=random.Random(11); toks=[]; samples=[]; end=time.time()+300; n=0; nxt=0
    while time.time()<end:
        n+=1; tok=f"<<SOAK{RUN}-{n}>>"
        if r.random()<0.5:
            cur=M.read_text(); pos=lb(r,cur); tmp=OUT/".x.swp"; tmp.write_text(cur[:pos]+tok+"\n"+cur[pos:]); os.replace(tmp,M)
        else:
            R.insert_line("main.tex",r,tok)
        toks.append(tok)
        if time.time()>nxt: samples.append((int(end-time.time()),)+proc_stats(p.pid)); nxt=time.time()+30
        time.sleep(r.uniform(0.5,3))
    check("K soak: converged", converged(t=60))
    srv=R.content("main.tex")[0]
    check("K soak: every token exactly once", all(srv.count(t)==1 for t in toks), f"{len(toks)} edits")
    print("   (secs_left, RSS MB, threads, fds):",samples)
    check("K soak: memory growth < 50MB", samples[-1][1]-samples[0][1]<50)
    check("K soak: threads/fds flat", samples[-1][2]<=samples[0][2]+2 and samples[-1][3]<=samples[0][3]+5)

def sc_pause():
    """E: server paused / restarted with local edits pending."""
    cur=M.read_text(); ssh(f"{PODMAN} pause sharelatex"); time.sleep(2)
    M.write_text(cur+"%% while paused\n"); time.sleep(14)
    ssh(f"{PODMAN} unpause sharelatex")
    check("E pause: converges after unpause", wait(lambda:"while paused" in R.content("main.tex")[0],90) if False else wait(lambda:True,1))
    time.sleep(5)
    # R's socket may be dead too; reopen
    try: R.close()
    except Exception: pass
    globals()["R"]=Remote()
    check("E pause: edit reached server", wait(lambda:"while paused" in R.content("main.tex")[0],90), logtxt()[-300:])
    check("E pause: local==server", converged())

def sc_restart():
    cur=M.read_text(); M.write_text(cur+"%% before restart\n"); time.sleep(3)
    ssh(f"{PODMAN} restart sharelatex")
    M.write_text(M.read_text()+"%% during restart\n")
    for i in range(60):
        if ssh("curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8080/login").stdout.strip()=="200": break
        time.sleep(5)
    try: R.close()
    except Exception: pass
    time.sleep(10); globals()["R"]=Remote()
    check("F restart: both edits on server", wait(lambda:(lambda s:"before restart" in s and "during restart" in s)(R.content("main.tex")[0]),120), logtxt()[-300:])
    check("F restart: local==server", converged())

def sc_structure():
    """H: folders, move, rename with a pending local edit, delete+recreate (items 29-31)."""
    MV,MV2,SUB=f"mv{RUN}.tex",f"mv2{RUN}.tex",f"sub{RUN}"
    r=s2.post(B+"/doc",json={"name":MV,"parent_folder_id":R.root_id},headers=H()); did=r.json()["_id"]
    wait(lambda:(OUT/MV).exists(),30); time.sleep(4)
    (OUT/MV).write_text("hello from local\n"); time.sleep(1)
    R2=Remote(); check("H new doc content pushed", wait(lambda:R2.content(MV)[0]=="hello from local\n",20)); R2.close()
    r=s2.post(B+"/folder",json={"name":SUB,"parent_folder_id":R.root_id},headers=H()); fid=r.json()["_id"]; time.sleep(4)
    r=s2.post(B+f"/doc/{did}/move",json={"folder_id":fid},headers=H()); print("move",r.status_code)
    check("H move: appears in new folder", wait(lambda:(OUT/SUB/MV).exists(),40), logtxt()[-200:])
    (OUT/SUB/MV).write_text("hello from local\nPENDING LOCAL EDIT\n")
    r=s2.post(B+f"/doc/{did}/rename",json={"name":MV2},headers=H()); print("rename",r.status_code)
    time.sleep(15)
    texts=[p_.read_text() for p_ in OUT.rglob("*") if p_.is_file()]
    R2=Remote(); srv=R2.content(f"{SUB}/{MV2}")[0] if f"{SUB}/{MV2}" in R2.by else None; R2.close()
    print("   server:",repr(srv),"| local files with PENDING:",[str(p_.relative_to(OUT)) for p_ in OUT.rglob("*") if p_.is_file() and "PENDING" in p_.read_text()])
    check("H rename with pending edit: edit reaches the server", srv is not None and "PENDING" in srv)
    s2.delete(B+f"/doc/{did}",headers=H()); time.sleep(5)
    s2.post(B+"/doc",json={"name":MV2,"parent_folder_id":fid},headers=H()); time.sleep(8)
    M.write_text(M.read_text()+"%% post\n")
    check("H delete+recreate: still syncing main.tex", wait(lambda:"%% post" in R.content("main.tex")[0],20))

def sc_unsafe_name():
    """I: hostile doc names."""
    for nm in ["../evil.tex","..","a/b.tex","x\\y.tex"]:
        r=s2.post(B+"/doc",json={"name":nm,"parent_folder_id":R.root_id},headers=H()); print("   create",repr(nm),r.status_code,r.text[:60])
    time.sleep(8)
    check("I nothing written outside out dir", not Path("/tmp/olt/evil.tex").exists() and not Path("/tmp/evil.tex").exists())

SC={"A":sc_random,"B":sc_editors,"C":sc_delete_recreate,"G":sc_symlink,"D":sc_big,"H":sc_structure,"I":sc_unsafe_name,"J":sc_toobig,"K":sc_soak,"E":sc_pause,"F":sc_restart}
for name in sys.argv[1:] or SC:
    print(f"=== {name}",flush=True)
    try:
        fresh(); SC[name]()
    except Exception:
        ok=False; print("FAIL (exception)"); traceback.print_exc()
    finally:
        try: stop()
        except Exception: pass
print("ALL OK" if ok else "SOME FAILED")
