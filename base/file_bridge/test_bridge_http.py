import base64, hashlib, hmac, json, time, sys
import bridge
bridge.TOKEN_SECRET = "test-secret-123"
# mock the SMB layer (already validated live) so we test HTTP logic only
STATE = {"list_calls": [], "paste_calls": []}
bridge.list_dir = lambda vmid,path,offset=0,limit=500: {"path":path,"total":2,"offset":offset,"entries":[{"name":"a.txt","isDir":False,"size":10,"mtime":0},{"name":"My Servers","isDir":True,"size":0,"mtime":0}]}
def _paste(items,dv,dp,op): STATE["paste_calls"].append((items,dv,dp,op)); return {"op":op,"items":len(items),"bytesCopied":123,"nativeMoves":0}
bridge.paste = _paste
bridge.mkdir = lambda v,p: None
import app
from fastapi.testclient import TestClient
# https base_url so the Secure session cookie is stored/sent by the client
def client(): return TestClient(app.app, base_url="https://testserver")

def mint(uid, servers, ttl=3600):
    body={"uid":uid,"servers":servers,"exp":int(time.time())+ttl}
    bj=base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    sig=hmac.new(b"test-secret-123",bj.encode(),hashlib.sha256).digest()
    return bj+"."+base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

SRV=[{"vmid":201,"name":"SQX 201","type":"vps-e"},{"vmid":444,"name":"MT Demo","type":"mt"}]
tok=mint("uidA",SRV)
B={"Authorization":f"Bearer {tok}"}

r=[]
cA=client()
# --- pre-session: nothing works without a cookie
r.append(("no-auth 401", cA.get("/api/servers").status_code==401))
r.append(("link token NOT valid on endpoints", cA.get("/api/servers",headers=B).status_code==401))
r.append(("bad-sig link 401", cA.post("/api/session",headers={"Authorization":"Bearer x.y"}).status_code==401))
exp=mint("uidA",SRV,ttl=-10)
r.append(("expired link 401", cA.post("/api/session",headers={"Authorization":f"Bearer {exp}"}).status_code==401))
# --- redeem: link -> session cookie
s=cA.post("/api/session",headers=B)
r.append(("redeem ok", s.status_code==200 and s.json()["ok"] and not s.json().get("resumed")))
r.append(("cookie set", app.SESSION_COOKIE in cA.cookies))
_link_exp=json.loads(base64.urlsafe_b64decode(tok.split(".")[0]+"=="))["exp"]
r.append(("cap = link mint + 12h", s.json()["cap"]==_link_exp-3600+12*3600))
COOKIE=cA.cookies[app.SESSION_COOKIE]
# --- single use: same link on a FRESH client (the incognito copy) -> dead
cB=client()
r.append(("link reuse blocked (incognito copy)", cB.post("/api/session",headers=B).status_code==401))
r.append(("fresh client still no access", cB.get("/api/servers").status_code==401))
# --- session cookie works for everything
sv=cA.get("/api/servers"); r.append(("servers via cookie", sv.status_code==200 and len(sv.json()["servers"])==2))
ls=cA.get("/api/list?vmid=201&path="); r.append(("list own vm", ls.status_code==200 and ls.json()["total"]==2))
r.append(("list foreign vm 403", cA.get("/api/list?vmid=999&path=").status_code==403))
p=cA.post("/api/paste",json={"op":"copy","items":[{"vmid":201,"path":"NeuraData\\x"}],"destVmid":444,"destPath":"NeuraData"})
r.append(("paste cross-server owned", p.status_code==200 and p.json()["op"]=="copy"))
pf=cA.post("/api/paste",json={"op":"copy","items":[{"vmid":999,"path":"x"}],"destVmid":444,"destPath":""})
r.append(("paste foreign source 403", pf.status_code==403))
pb=cA.post("/api/paste",json={"op":"nuke","items":[],"destVmid":444})
r.append(("bad op 400", pb.status_code==400))
# --- downloads ride the cookie (no ?t=)
bridge.open_read = lambda v,p: __import__("io").BytesIO(b"hello")
dl=cA.get("/api/download?vmid=201&path=NeuraData\\a.txt")
r.append(("download via cookie", dl.status_code==200 and dl.content==b"hello"))
r.append(("download w/o cookie 401", client().get(f"/api/download?vmid=201&path=a.txt").status_code==401))
# --- renewal: cookie-only /api/session slides the session
rn=cA.post("/api/session")
r.append(("renew resumes", rn.status_code==200 and rn.json().get("resumed")==True))
r.append(("renew exp <= cap", rn.json()["exp"]<=rn.json()["cap"]))
# --- a session token is NOT a link token
cC=client()
r.append(("session token can't redeem", cC.post("/api/session",headers={"Authorization":f"Bearer {COOKIE}"}).status_code==401))
# --- tampered cookie rejected
cD=client(); cD.cookies.set(app.SESSION_COOKIE, COOKIE[:-2]+"xx", domain="testserver")
r.append(("tampered cookie 401", cD.get("/api/servers").status_code==401))

ok=sum(1 for _,v in r if v)
for name,v in r: print(("PASS" if v else "FAIL"), name)
print(f"\n{ok}/{len(r)} passed")
sys.exit(0 if ok==len(r) else 1)
