import re, sys, json, html, requests
# usage: setup_local_overleaf.py <activation token> <user id>   (printed by create-user.mjs)
B="http://127.0.0.1:18080"
tok, uid = sys.argv[1], sys.argv[2]
PW="Test-pass-12345!xyz"
s=requests.Session()
def csrf(url):
    t=s.get(B+url).text
    return html.unescape(re.search(r'name="ol-csrfToken"[^>]*content="([^"]*)"',t).group(1))
c=csrf(f"/user/activate?token={tok}&user_id={uid}")
r=s.post(B+"/user/password/set",json={"passwordResetToken":tok,"password":PW,"email":"test@example.com"},headers={"X-Csrf-Token":c})
print("set pw",r.status_code,r.text[:150])
c=csrf("/login")
r=s.post(B+"/login",json={"email":"test@example.com","password":PW,"_csrf":c},headers={"X-Csrf-Token":c})
print("login",r.status_code,r.text[:150])
c=csrf("/project")
r=s.post(B+"/project/new",json={"projectName":"synctest","template":"example"},headers={"X-Csrf-Token":c})
print("new",r.status_code,r.text[:200])
print("cookies",{k.name:k.value for k in s.cookies})
json.dump({"cookie":{k.name:k.value for k in s.cookies},"project":r.json().get("project_id")},open("/tmp/olt/state.json","w"))
