import base64, hashlib, hmac, json, os, tempfile, time, sys
os.environ["FILE_BRIDGE_REDEEMED_FILE"] = os.path.join(tempfile.mkdtemp(), "redeemed.json")
import bridge
bridge.TOKEN_SECRET = "test-secret-123"
_orig_list_dir = bridge.list_dir  # real one, tested below with a fake smbclient
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

def mint(uid, servers, ttl=3600, host="testserver"):
    body={"uid":uid,"servers":servers,"exp":int(time.time())+ttl}
    if host is not None: body["host"]=host
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
# --- CROSS-SESSION (2026-07-08 leak): with a session cookie already active,
# opening a NEW link (different servers) must REDEEM the new one, not resume
# the stale cookie. Use a dedicated client so cA (uidA, 201/444) is untouched.
cZ=client()
cZ.post("/api/session",headers={"Authorization":f"Bearer {mint('uidZ',SRV)}"})   # cookie = 201/444
OTHER=[{"vmid":999,"name":"Other cust","type":"mt"}]
sb=cZ.post("/api/session",headers={"Authorization":f"Bearer {mint('uidZ2',OTHER)}"})  # present link B
r.append(("new link redeems over stale cookie", sb.status_code==200 and not sb.json().get("resumed")))
new_vmids=sorted(s["vmid"] for s in cZ.get("/api/servers").json()["servers"])
r.append(("session now shows the NEW link's servers only", new_vmids==[999]))
r.append(("stale servers (201/444) gone", 201 not in new_vmids and 444 not in new_vmids))
# --- a session token is NOT a link token
cC=client()
r.append(("session token can't redeem", cC.post("/api/session",headers={"Authorization":f"Bearer {COOKIE}"}).status_code==401))
# --- tampered cookie rejected
cD=client(); cD.cookies.set(app.SESSION_COOKIE, COOKIE[:-2]+"xx", domain="testserver")
r.append(("tampered cookie 401", cD.get("/api/servers").status_code==401))
# --- host binding: link for another host / no host claim -> 401
oth=mint("uidA",SRV,host="files-fsn.neuravps.com")
r.append(("wrong-host link 401", client().post("/api/session",headers={"Authorization":f"Bearer {oth}"}).status_code==401))
noh=mint("uidA",SRV,host=None)
r.append(("hostless link 401", client().post("/api/session",headers={"Authorization":f"Bearer {noh}"}).status_code==401))
# --- persistence: redeemed set survives a "restart" (reload from disk)
tok2=mint("uidB",SRV)
cE=client()
r.append(("2nd link redeems", cE.post("/api/session",headers={"Authorization":f"Bearer {tok2}"}).status_code==200))
app._redeemed.clear()          # simulate process restart
app._load_redeemed()           # reload from REDEEMED_PATH
r.append(("reuse blocked AFTER restart", client().post("/api/session",headers={"Authorization":f"Bearer {tok2}"}).status_code==401))

# --- real list_dir: Windows symlinks are SKIPPED, not a 500 (bug 2026-07-08,
# C:\My Servers cross-server links / Users\All Users blew up the listing)
import types
from smbprotocol.exceptions import SMBLinkRedirectionError
_tree = {r"\\srv\C$": ["VM100 - link", "ok.txt", "SubDir"],
         r"\\srv\C$\SubDir": ["b.txt"]}
fake_smb = types.ModuleType("smbclient")
fake_smb.listdir = lambda unc: _tree[unc]
def _fake_stat(unc):
    name = unc.split("\\")[-1]
    if "link" in name: raise SMBLinkRedirectionError("path", "target")
    st = types.SimpleNamespace()
    st.st_mode, st.st_size, st.st_mtime = (0o040755, 0, 1000) if name=="SubDir" else (0o100644, 10, 1000)
    return st
fake_smb.stat = _fake_stat
sys.modules["smbclient"] = fake_smb
bridge._smb_session = lambda vmid: ("srv", r"\\srv\C$")
d = _orig_list_dir(1, "")
r.append(("real list_dir skips symlinks", [e["name"] for e in d["entries"]]==["ok.txt","SubDir"] and d["total"]==2))
# --- download-zip: multi-selection (file + folder) -> one zip, cookie auth
import io as _io, zipfile as _zf
dz=cA.get("/api/download-zip", params=[("vmid",201),("path","ok.txt"),("path","SubDir")])
names=_zf.ZipFile(_io.BytesIO(dz.content)).namelist() if dz.status_code==200 else []
r.append(("download-zip multi", dz.status_code==200 and sorted(names)==["SubDir/b.txt","ok.txt"]))
r.append(("download-zip no cookie 401", client().get("/api/download-zip", params=[("vmid",201),("path","ok.txt")]).status_code==401))

# --- streamed zip must not amplify (regression: quadratic NUL padding) ------
# The old generator truncated the BytesIO after every member while ZipFile
# kept seeking back to its cumulative start_dir, so each member was emitted
# behind a copy of the whole archive so far as NUL bytes. A 17 MB folder
# streamed as ~1.7 GB. Guard the *ratio*, which is what the customer saw.
_PAYLOAD = b"x" * 8192
_NFILES = 40
_many = {r"\\srv\C$\Many": [f"f{i:03d}.bin" for i in range(_NFILES)]}
_tree.update(_many)
_tree[r"\\srv\C$"] = _tree[r"\\srv\C$"] + ["Many"]
def _fake_stat2(unc):
    name = unc.split("\\")[-1]
    if "link" in name: raise SMBLinkRedirectionError("path", "target")
    st = types.SimpleNamespace()
    isdir = name in ("SubDir", "Many")
    st.st_mode = 0o040755 if isdir else 0o100644
    st.st_size, st.st_mtime = (0 if isdir else len(_PAYLOAD)), 1000
    return st
fake_smb.stat = _fake_stat2
_orig_open_read = bridge.open_read
bridge.open_read = lambda vmid, rel: _io.BytesIO(_PAYLOAD)
try:
    dz2 = cA.get("/api/download-zip", params=[("vmid",201),("path","Many")])
    real = len(_PAYLOAD) * _NFILES
    ratio = len(dz2.content) / real
    zf2 = _zf.ZipFile(_io.BytesIO(dz2.content))
    r.append(("streamed zip size ~= payload (no amplification)",
              dz2.status_code == 200 and ratio < 1.2))
    r.append(("streamed zip is readable and complete",
              len(zf2.namelist()) == _NFILES
              and zf2.testzip() is None
              and zf2.read("Many/f000.bin") == _PAYLOAD))
finally:
    bridge.open_read = _orig_open_read

ok=sum(1 for _,v in r if v)
for name,v in r: print(("PASS" if v else "FAIL"), name)
print(f"\n{ok}/{len(r)} passed")
sys.exit(0 if ok==len(r) else 1)
