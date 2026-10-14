import re, sys, json, html, requests
B="http://127.0.0.1:18080"
tok="0e5627fcdf6cf784ca024d2f92837ab20e18af96b1448364d57f50d1c8a65e8f"
uid="6aceed6b82099cdc1b1f4bcd"
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
