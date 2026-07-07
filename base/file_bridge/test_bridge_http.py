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
c = TestClient(app.app)

def mint(uid, servers, ttl=3600):
    body={"uid":uid,"servers":servers,"exp":int(time.time())+ttl}
    bj=base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    sig=hmac.new(b"test-secret-123",bj.encode(),hashlib.sha256).digest()
    return bj+"."+base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

SRV=[{"vmid":201,"name":"SQX 201","type":"vps-e"},{"vmid":444,"name":"MT Demo","type":"mt"}]
tok=mint("uidA",SRV)
H={"Authorization":f"Bearer {tok}"}

r=[]
r.append(("no-auth 401", c.get("/api/servers").status_code==401))
r.append(("bad-sig 401", c.get("/api/servers",headers={"Authorization":"Bearer x.y"}).status_code==401))
exp=mint("uidA",SRV,ttl=-10)
r.append(("expired 401", c.get("/api/servers",headers={"Authorization":f"Bearer {exp}"}).status_code==401))
sv=c.get("/api/servers",headers=H); r.append(("servers list", sv.status_code==200 and len(sv.json()["servers"])==2))
ls=c.get("/api/list?vmid=201&path=",headers=H); r.append(("list own vm", ls.status_code==200 and ls.json()["total"]==2))
r.append(("list foreign vm 403", c.get("/api/list?vmid=999&path=",headers=H).status_code==403))
# paste cross-server (both owned)
p=c.post("/api/paste",headers=H,json={"op":"copy","items":[{"vmid":201,"path":"NeuraData\\x"}],"destVmid":444,"destPath":"NeuraData"})
r.append(("paste cross-server owned", p.status_code==200 and p.json()["op"]=="copy"))
# paste with a foreign source -> 403
pf=c.post("/api/paste",headers=H,json={"op":"copy","items":[{"vmid":999,"path":"x"}],"destVmid":444,"destPath":""})
r.append(("paste foreign source 403", pf.status_code==403))
# bad op
pb=c.post("/api/paste",headers=H,json={"op":"nuke","items":[],"destVmid":444})
r.append(("bad op 400", pb.status_code==400))
# download token via query param (?t=)
bridge.open_read = lambda v,p: __import__("io").BytesIO(b"hello")
dl=c.get(f"/api/download?vmid=201&path=NeuraData\\a.txt&t={tok}")
r.append(("download via ?t=", dl.status_code==200 and dl.content==b"hello"))

ok=sum(1 for _,v in r if v)
for name,v in r: print(("PASS" if v else "FAIL"), name)
print(f"\n{ok}/{len(r)} passed")
sys.exit(0 if ok==len(r) else 1)
