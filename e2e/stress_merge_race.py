import os, random, sys, threading, time
from pathlib import Path
import os as _os; REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))); sys.path.insert(0, REPO)
import tempfile
from overleaf_sync import OverleafSync
out=Path(tempfile.mkdtemp()); base="\n".join(f"line {i}" for i in range(200))
(out/"main.tex").write_text(base)
s=OverleafSync("p","c",str(out)); s._docs["d1"]={"content":base,"version":0,"path":"main.tex"}
stop=False; ltoks=[]; rtoks=[]
def editor():
    r=random.Random(1); n=0
    while not stop:
        n+=1; cur=(out/"main.tex").read_text(); lines=cur.split("\n"); lines.insert(r.randint(0,len(lines)),f"<<L{n}>>")
        tmp=out/".swp"; tmp.write_text("\n".join(lines)); os.replace(tmp,out/"main.tex"); ltoks.append(f"<<L{n}>>"); time.sleep(r.uniform(0.001,0.01))
t=threading.Thread(target=editor); t.start()
r=random.Random(2); v=0
dbg=[]
orig=s._on_ot_update
def wrapped(args):
    before=list(ltoks)
    file_before=(out/"main.tex").read_text(); state_before=s._docs["d1"]["content"]
    orig(args)
    cur=(out/"main.tex").read_text()
    gone=[t for t in before if t not in cur]
    if gone and not dbg:
        dbg.append((args[0]["v"],gone))
        from overleaf_sync import compute_ot_ops, transform_x, apply_sharejs_ops, wire_to_cp
        remote=wire_to_cp(state_before,args[0]["op"])
        lo=compute_ot_ops(state_before,file_before); _,rt=transform_x(lo,remote)
        pure=apply_sharejs_ops(file_before,rt)
        print("   pure merge keeps tokens?", all(t in pure for t in gone), "| file_before has", [t in file_before for t in gone], "| after has", [t in cur for t in gone])
        print("   local_ops summary:", [(("i",len(o["i"])) if "i" in o else ("d",len(o["d"])),o["p"]) for o in lo], "remote", remote)
s._on_ot_update=wrapped
for n in range(1,300):
    with s._lock: content=s._docs["d1"]["content"]
    ls=content.split("\n"); i=r.randint(0,len(ls)); pos=sum(len(x)+1 for x in ls[:i])
    s._on_ot_update([{"doc":"d1","v":v,"op":[{"p":pos,"i":f"<<R{n}>>\n"}],"meta":{"source":"other"}}]); v+=1; rtoks.append(f"<<R{n}>>")
    time.sleep(0.003)
stop=True; t.join()
final=(out/"main.tex").read_text()
lost=[x for x in ltoks+rtoks if final.count(x)!=1]
print("first L vanish after op:",dbg)
print("L written",len(ltoks),"R applied",len(rtoks),"bad:",len(lost),lost[:8])
