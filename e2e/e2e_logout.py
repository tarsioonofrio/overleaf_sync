import json, os, re, html, subprocess, sys, time, requests
B="http://127.0.0.1:18080"; st=json.load(open("/tmp/olt/state.json")); PID=st["project"]; C=st["cookie"]["overleaf.sid"]
env=dict(os.environ,OVERLEAF_URL=B,OVERLEAF_COOKIE_NAME="overleaf.sid")
shutil_out="/tmp/olt/out3"; subprocess.run(["rm","-rf",shutil_out])
log=open("/tmp/olt/log3.txt","w")
p=subprocess.Popen(["/tmp/olt/venv/bin/python","-u","-I","/home/tarsio/repos/overleaf_sync/overleaf_sync.py",PID,"--cookie",C,"--output-dir",shutil_out],stdout=log,stderr=log,env=env)
time.sleep(12)
s=requests.Session(); s.cookies.set("overleaf.sid",C,domain="127.0.0.1")
t=s.get(f"{B}/project").text; csrf=html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"',t).group(1))
r=s.post(f"{B}/logout",headers={"X-Csrf-Token":csrf},json={}); print("logout",r.status_code)
before=open(f"{shutil_out}/main.tex").read()
subprocess.run(["ssh","desktop","podman --root /var/mnt/data/overleaf-ce-test/storage --runroot /run/user/1000/ol-ce-runroot restart sharelatex"],capture_output=True)
try: rc=p.wait(timeout=240)
except subprocess.TimeoutExpired: p.kill(); rc="STILL RUNNING after 240s"
print("exit code:",rc); print("local file intact:", open(f"{shutil_out}/main.tex").read()==before)
print(open("/tmp/olt/log3.txt").read()[-700:])
# log in again for later tests
PW="Test-pass-12345!xyz"
s2=requests.Session()
for i in range(40):
    try:
        t=s2.get(f"{B}/login").text; break
    except Exception: time.sleep(5)
csrf=html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"',t).group(1))
r=s2.post(f"{B}/login",json={"email":"test@example.com","password":PW,"_csrf":csrf},headers={"X-Csrf-Token":csrf}); print("relogin",r.status_code)
st["cookie"]={k.name:k.value for k in s2.cookies}; json.dump(st,open("/tmp/olt/state.json","w"))
