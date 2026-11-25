"""scripts/coldmove (kit rtmig de traslado en frío, 25/11/2026, revisado en el PR #291): los scripts se
ejecutan de verdad contra `qm`, `zfs`, `ssh` y `hostname` falsos. «src» y «dst» son dos árboles de
estado; el `ssh` falso ejecuta la orden remota con FAKE_SIDE=dst y las rutas del destino (PVE_ETC,
CPUINFO, ...). FAKE_SSH_FAIL_MATCH hace fallar (rc 255) las órdenes remotas que contengan ese texto, y
FAKE_SSH_TRUNCATE_MATCH les pasa solo la mitad de su stdin: SSH que cae a mitad."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "scripts/coldmove"

GENOA = "vendor_id\t: AuthenticAMD\ncpu family\t: 25\nmodel\t\t: 17\nmodel name\t: AMD EPYC 9454P\nflags\t\t: fpu sse avx2 avx512f avx512bw avx512cd avx512dq avx512vl\n"
TURIN = "vendor_id\t: AuthenticAMD\ncpu family\t: 26\nmodel\t\t: 2\nmodel name\t: AMD EPYC 9755\nflags\t\t: fpu sse avx2 avx512f avx512bw avx512cd avx512dq avx512vl\n"
INTEL_NO512 = "vendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 183\nmodel name\t: i5-13500\nflags\t\t: fpu sse avx2\n"

FAKE_SSH = r'''#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]; i = 0; no_stdin = False
while i < len(args) and args[i].startswith("-"):
    if args[i] == "-n": no_stdin = True; i += 1
    elif args[i] in ("-p", "-c", "-o", "-i", "-J", "-l", "-F"): i += 2
    else: i += 1
host = args[i]; cmd = " ".join(args[i + 1:])
root = os.environ["FAKE_ROOT"]
with open(os.path.join(root, "ssh.log"), "a") as f:
    f.write("%s> %s :: %s\n" % (os.environ.get("FAKE_SIDE", "src"), host, cmd.replace("\n", "\\n")))
if os.environ.get("FAKE_REMOTE"):
    # a command that arrived over SSH is now opening ANOTHER ssh: in pull mode that would be the
    # origin (no fleet key) connecting out; in push mode, the destination. Never allowed.
    with open(os.path.join(root, "nested.log"), "a") as f:
        f.write("%s -> %s :: %s\n" % (os.environ.get("FAKE_SIDE"), host, cmd))
    sys.stderr.write("Permission denied (publickey).\n"); sys.exit(255)
fail = os.environ.get("FAKE_SSH_FAIL_MATCH")
dead = os.path.join(root, "ssh-dead")          # FAKE_SSH_DIE_AFTER: the link dies after that command
if os.path.exists(dead):
    sys.stderr.write("Connection timed out\n"); sys.exit(255)
if os.environ.get("FAKE_SSH_DIE_AFTER") and os.environ["FAKE_SSH_DIE_AFTER"] in cmd:
    open(dead, "w").close()
if fail and fail in cmd:
    sys.stderr.write("Connection closed by remote host\n"); sys.exit(255)
def side_env(side):
    d = os.path.join(root, side)
    e = dict(os.environ, FAKE_SIDE=side, PVE_ETC=os.path.join(d, "etc/pve"), CPUINFO=os.path.join(d, "cpuinfo"),
             NV_RAM_GUARD=os.path.join(d, "ram-guard.py"), NV_RECONCILER=os.path.join(d, "reconciler.sh"),
             QEMU_PIDDIR=os.path.join(d, "run"), PROC_DIR=os.path.join(d, "proc"))
    if os.environ.get("FAKE_JUMP_CHAIN") != "1":
        e["FAKE_REMOTE"] = "1"
    return e
if host == os.environ.get("FAKE_JUMP", "-"):
    env = dict(os.environ, FAKE_JUMP_CHAIN="1")      # a BASE: it may ssh on to the nodes (deploy)
elif host == "root@" + os.environ.get("FAKE_SRC_IP", "-"):
    env = side_env("src")
elif host == "root@" + os.environ["FAKE_DST_IP"]:
    env = side_env("dst")
else:
    sys.stderr.write("ssh: connect to host %s: No route\n" % host); sys.exit(255)
lose = os.environ.get("FAKE_SSH_LOSE_MATCH")
if lose and lose in cmd:                 # the command runs, but its answer never comes back
    subprocess.run(["bash", "-c", cmd], env=env, capture_output=True,
                   stdin=subprocess.DEVNULL if no_stdin else None)
    sys.exit(255)
trunc = os.environ.get("FAKE_SSH_TRUNCATE_MATCH")
if trunc and trunc in cmd and not no_stdin:
    data = sys.stdin.buffer.read()
    r = subprocess.run(["bash", "-c", cmd], env=env, input=data[: len(data) // 2])
    sys.exit(r.returncode)
sys.exit(subprocess.run(["bash", "-c", cmd], env=env,
                        stdin=subprocess.DEVNULL if no_stdin else None).returncode)
'''

FAKE_HOSTNAME = r'''#!/usr/bin/env python3
import os
print(os.environ["FAKE_HOST_" + os.environ.get("FAKE_SIDE", "src").upper()])
'''

FAKE_QM = r'''#!/usr/bin/env python3
import os, sys
side = os.environ.get("FAKE_SIDE", "src"); root = os.environ["FAKE_ROOT"]
pve = os.environ.get("PVE_ETC", "/nonexistent")
a = sys.argv[1:]
with open(os.path.join(root, "qm.log"), "a") as f: f.write(side + " " + " ".join(a) + "\n")
vmid = a[1]; conf = os.path.join(pve, "qemu-server", vmid + ".conf")
stf = os.path.join(root, side, "state-" + vmid)
def state(): return open(stf).read().strip() if os.path.exists(stf) else "stopped"
if a[0] == "status":
    if os.environ.get("FAKE_QM_STATUS_FAIL") == side: sys.stderr.write("ipcc_send_rec failed\n"); sys.exit(255)
    if not os.path.exists(conf): sys.stderr.write("Configuration file does not exist\n"); sys.exit(2)
    print("status: " + state())
elif a[0] == "config":
    if not os.path.exists(conf): sys.exit(2)
    sys.stdout.write(open(conf).read())
elif a[0] == "shutdown":
    if os.environ.get("FAKE_SHUTDOWN_FAIL"): sys.exit(255)
    open(stf, "w").write("stopped")
elif a[0] == "start":
    if os.environ.get("FAKE_QM_START_FAIL") == side: sys.exit(1)
    if not os.path.exists(conf): sys.exit(2)
    open(stf, "w").write("running")
else:
    sys.exit(3)
'''

FAKE_ZFS = r'''#!/usr/bin/env python3
import json, os, random, sys
side = os.environ.get("FAKE_SIDE", "src"); root = os.environ["FAKE_ROOT"]
path = os.path.join(root, side, "zfs.json")
st = json.load(open(path)) if os.path.exists(path) else {}
def save(): json.dump(st, open(path, "w"))
a = sys.argv[1:]
with open(os.path.join(root, "zfs.log"), "a") as f: f.write(side + " " + " ".join(a) + "\n")
def split(n): return n.split("@", 1) if "@" in n else (n, None)
def new_ds(t="volume", volsize=1 << 30): return {"snaps": {}, "order": [], "written": 0, "token": "-", "type": t, "volsize": volsize, "props": {}}
def typ(ds): return st[ds].get("type", "volume")
def missing(n): sys.stderr.write("cannot open '%s': dataset does not exist\n" % n); sys.exit(1)
if a[0] == "list":
    if "-t" in a:
        ds = a[-1]
        if ds not in st: missing(ds)
        for s_ in st[ds]["order"]: print(ds + "@" + s_)
    elif "-r" in a:
        r = a[-1]
        if "name,type" in a:
            if r not in st: missing(r)
            for ds in sorted(st):
                if ds == r or ds.startswith(r + "/"): print("%s\t%s" % (ds, typ(ds)))
        else:
            for ds in sorted(st): print(ds + "\t1G")
    else:
        ds, sn = split(a[-1])
        sys.exit(0 if ds in st and (sn is None or sn in st[ds]["snaps"]) else 1)
elif a[0] == "create":
    name = a[-1]
    if name in st: sys.stderr.write("exists\n"); sys.exit(1)
    st[name] = new_ds("filesystem", 0); save()
elif a[0] == "rename":
    src_, dst_ = a[-2], a[-1]
    if src_ not in st: missing(src_)
    if dst_ in st: sys.stderr.write("exists\n"); sys.exit(1)
    st[dst_] = st.pop(src_); save()
elif a[0] == "snapshot":
    for n in a[1:]:
        ds, sn = split(n)
        if ds not in st or sn in st[ds]["snaps"]: sys.stderr.write("cannot snapshot %s\n" % n); sys.exit(1)
        st[ds]["snaps"][sn] = str(random.getrandbits(60)); st[ds]["order"].append(sn)
        st[ds]["written"] = 0
    save()
elif a[0] == "set":
    k, v = a[1].split("=", 1); ds = a[2]
    if ds not in st: missing(ds)
    st[ds].setdefault("props", {})[k] = v; save()
elif a[0] == "get":
    prop = a[a.index("value") + 1]; ds, sn = split(a[-1])
    fail = os.environ.get("FAKE_ZFS_FAIL_MATCH")
    if fail and side == os.environ.get("FAKE_ZFS_FAIL_SIDE", "dst") and fail in " ".join(a):
        sys.stderr.write("internal error: Input/output error\n"); sys.exit(1)
    if ds not in st or (sn and sn not in st[ds]["snaps"]): missing(a[-1])
    hostile = os.environ.get("FAKE_HOSTILE_" + prop.split("@")[0].upper())
    if hostile is not None and side == "src":     # a compromised origin answering garbage
        print(hostile); sys.exit()
    if prop == "guid":
        if not sn: sys.exit(1)
        print(st[ds]["snaps"][sn])
    elif prop.startswith("written@"):
        print(st[ds].get("written", 0))
    elif prop == "receive_resume_token":
        print(st[ds].get("token", "-"))
    elif prop == "type":
        print(typ(ds))
    elif prop == "volsize":
        print(st[ds].get("volsize", 1 << 30))
    elif prop in ("quota", "reservation"):
        print(st[ds].get("props", {}).get(prop, 0))
    else:
        print("1G"); print("2G")
elif a[0] == "send":
    if "-t" in a:                       # resume from token: "1-" + hex(json fields)
        fields = json.loads(bytes.fromhex(a[a.index("-t") + 1][2:]).decode())
        sys.stdout.write("STREAM " + json.dumps(fields) + "\n"); sys.exit()
    ds, sn = split(a[-1])
    prev = a[a.index("-i") + 1].lstrip("@") if "-i" in a else ""
    if "-nP" in a: print("size\t1000"); sys.exit()
    if sn not in st.get(ds, {}).get("snaps", {}): sys.exit(1)
    fields = {"ds": ds, "sn": sn, "guid": st[ds]["snaps"][sn], "prev": prev or "-",
              "volsize": st[ds].get("volsize", 1 << 30), "type": typ(ds), "extra": False}
    hs = os.environ.get("FAKE_HOSTILE_STREAM") if side == "src" else None
    if hs == "filesystem": fields["type"] = "filesystem"
    if hs == "extra": fields["extra"] = True
    if hs == "big": fields["volsize"] = 1 << 50
    sys.stdout.write("STREAM " + json.dumps(fields) + "\n")
elif a[0] == "recv":
    ds = a[-1]; f = json.loads(sys.stdin.read().split(" ", 1)[1])
    cut = os.path.join(root, "recv-cut")
    if os.path.exists(cut) and open(cut).read().strip() == ds:   # cut half way: leave a token
        os.remove(cut)
        if ds not in st: st[ds] = new_ds(f["type"], f["volsize"])
        st[ds]["token"] = "1-" + json.dumps(f).encode().hex(); save()
        sys.stderr.write("connection reset\n"); sys.exit(1)
    if st.get(ds, {}).get("token", "-") != "-":
        if "-s" not in a: sys.exit(1)
        st[ds]["token"] = "-"
    elif f["prev"] == "-":
        if ds in st: sys.stderr.write("exists\n"); sys.exit(1)
        st[ds] = new_ds(f["type"], f["volsize"])
    elif ds not in st or f["prev"] not in st[ds]["snaps"]:
        sys.stderr.write("no base\n"); sys.exit(1)
    st[ds]["snaps"][f["sn"]] = f["guid"]; st[ds]["order"].append(f["sn"]); st[ds]["written"] = 0
    if f.get("extra"):
        st[ds.rsplit("/", 1)[0] + "/evil"] = new_ds("filesystem", 0)
    save()
elif a[0] == "destroy":
    if "-r" in a:
        r = a[-1]
        for ds in [d for d in st if d == r or d.startswith(r + "/")]: del st[ds]
        save(); sys.exit()
    ds, sn = split(a[-1])
    if sn is None:
        if ds not in st: missing(ds)
        if any(d.startswith(ds + "/") for d in st): sys.stderr.write("has children\n"); sys.exit(1)
        del st[ds]; save(); sys.exit()
    del st[ds]["snaps"][sn]; st[ds]["order"].remove(sn); save()
else:
    sys.exit(3)
'''

FAKE_RAM_GUARD = r'''#!/usr/bin/env python3
import base64, os, sys
side = os.environ.get("FAKE_SIDE", "src"); root = os.environ["FAKE_ROOT"]
with open(os.path.join(root, "ramguard.log"), "a") as f: f.write(side + " " + " ".join(sys.argv[1:]) + "\n")
act, vmid, node = sys.argv[1:4]
assert node == os.environ["FAKE_HOST_" + side.upper()], "node identity mismatch"
if act == "export-state":
    print(base64.b64encode(b'{"uuid":"u","floor":30000}').decode())
'''

VMID = "4242"
CONF = textwrap.dedent("""\
    boot: order=scsi0
    cores: 8
    cpu: {cpu}
    efidisk0: local-zfs:vm-4242-disk-1,efitype=4m,pre-enrolled-keys=1,size=1M
    hookscript: shared:snippets/sync-dnat.py
    ide2: none,media=cdrom
    memory: 61440
    name: E-test
    net0: virtio=BC:24:11:00:00:01,bridge=vmbr0
    ostype: win11
    scsi0: local-zfs:vm-4242-disk-0,discard=on,size=512G
    smbios1: uuid=11111111-2222-3333-4444-555555555555
    tpmstate0: local-zfs:vm-4242-disk-2,size=4M,version=v2.0
    """)
FINAL = "rtmig-final-20990101T0200Z"


def out(r):
    return r.stdout + r.stderr


CURRENT_MODE = "push"     # set per test class (see ModeCase); every scenario runs in both modes


class ModeCase(unittest.TestCase):
    MODE = "push"

    def setUp(self):
        global CURRENT_MODE
        CURRENT_MODE = self.MODE


def push_only(fn):
    """The test injects faults on the SSH link to the DESTINATION; in pull mode the destination is
    local (no link). Pull has its own transport-fault tests on the ORIGIN side (PullMode)."""
    def wrapper(self, *a, **kw):
        if self.MODE == "pull":
            self.skipTest("fault on the ssh link to the destination: push only")
        return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper
# (also used for tests whose config carries [PENDING]/snapshot sections: in pull mode the origin's
#  config goes through the local profile, which refuses [PENDING] and drops snapshot sections; the
#  pull behaviour is covered in PullHostile.)


class Kit:
    """push: the kit runs on the source (FAKE_SIDE=src) and talks to dst over ssh.
    pull: the kit runs on the destination (FAKE_SIDE=dst) and talks to src over ssh."""

    def __init__(self, tmp, cpu="x86-64-v4", src_cpu=GENOA, dst_cpu=TURIN,
                 src_reconciler=True, dst_reconciler=True, conf=None, run_cpu=None, mode=None,
                 src_host="0000999-AX162-TEST", dst_host="0000266-KDM-9755"):
        self.mode = mode or CURRENT_MODE
        self.t = Path(tmp)
        b = self.t / "bin"; b.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("hostname", FAKE_HOSTNAME), ("qm", FAKE_QM),
                           ("zfs", FAKE_ZFS)):
            p = b / name; p.write_text(body); p.chmod(p.stat().st_mode | stat.S_IEXEC)
        for name, body in (("pv", "#!/bin/sh\nexec cat\n"), ("zpool", "#!/bin/sh\necho '10% 1T'\n")):
            p = b / name; p.write_text(body); p.chmod(0o755)
        for side, cpuinfo, rec in (("src", src_cpu, src_reconciler), ("dst", dst_cpu, dst_reconciler)):
            d = self.t / side
            (d / "etc/pve/qemu-server").mkdir(parents=True)
            (d / "etc/pve/firewall").mkdir(parents=True)
            (d / "etc/pve/.version").write_text("1\n")
            (d / "cpuinfo").write_text(cpuinfo)
            (d / "ram-guard.py").write_text(FAKE_RAM_GUARD)
            if rec:
                r = d / "reconciler.sh"; r.write_text("#!/bin/sh\n"); r.chmod(0o755)
        self.src_conf = self.t / f"src/etc/pve/qemu-server/{VMID}.conf"
        self.dst_conf = self.t / f"dst/etc/pve/qemu-server/{VMID}.conf"
        self.src_conf.write_text(conf if conf is not None else CONF.format(cpu=cpu))
        (self.t / "src/etc/pve/firewall" / f"{VMID}.fw").write_text("[OPTIONS]\nenable: 1\n")
        (self.t / f"src/state-{VMID}").write_text("running")
        sizes = {0: 512 << 30, 1: 1 << 20, 2: 4 << 20}
        (self.t / "src/zfs.json").write_text(json.dumps(
            {f"rpool/data/vm-{VMID}-disk-{n}": {"snaps": {}, "order": [], "written": 7, "token": "-",
                                                 "type": "volume", "volsize": sizes[n]}
             for n in (0, 1, 2)}))
        # what QEMU runs with on the source (PVE runs x86-64-v* as qemu64,+flags)
        if run_cpu is None:
            run_cpu = "qemu64,+avx512f" if cpu.startswith("x86-64-v") else cpu + ",+kvm_pv_eoi"
        self.pidfile = self.t / "src/run" / f"{VMID}.pid"
        self.pidfile.parent.mkdir(); self.pidfile.write_text("31337\n")
        (self.t / "src/proc/31337").mkdir(parents=True)
        (self.t / "src/proc/31337/cmdline").write_bytes(
            b"\0".join([b"/usr/bin/kvm", b"-id", VMID.encode(), b"-cpu", run_cpu.encode(), b"-m", b"61440"]) + b"\0")
        self.work = self.t / "work"
        self.src_host, self.dst_host = src_host, dst_host
        here = "src" if self.mode == "push" else "dst"            # where the kit runs
        key = self.t / here / "neuravps_id"; key.write_text("fleet key\n")
        d = self.t / here
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{b}:{os.environ['PATH']}", "FAKE_ROOT": str(self.t),
            "FAKE_SRC_IP": "2001:db8:1::2", "FAKE_DST_IP": "2001:db8:2::2",
            "FAKE_HOST_SRC": src_host, "FAKE_HOST_DST": dst_host, "FAKE_SIDE": here,
            "VMID": VMID, "WORK": str(self.work), "RTMIG_MODE": self.mode,
            "PVE_ETC": str(d / "etc/pve"), "CPUINFO": str(d / "cpuinfo"),
            "NV_RAM_GUARD": str(d / "ram-guard.py"), "NV_RECONCILER": str(d / "reconciler.sh"),
            "QEMU_PIDDIR": str(d / "run"), "PROC_DIR": str(d / "proc"), "NV_FLEET_KEY": str(key),
        })
        if self.mode == "push":
            self.env.update({"DST": "2001:db8:2::2", "DST_NODE": dst_host})
        else:
            self.env.update({"SRC": "2001:db8:1::2", "SRC_NODE": src_host, "DST_NODE": dst_host,
                             "ALLOC_MEMORY_MB": "61440", "ALLOC_CORES": "8", "ALLOC_DISK_GB": "512"})
        for k in ("CHANGE_CPU", "ACK_HWID_CHANGE", "CPU_NOTICE", "RATE", "DISKS", "ZSTD",
                  "FAKE_REMOTE", "FAKE_JUMP_CHAIN", "FAKE_JUMP",
                  "DST" if self.mode == "pull" else "SRC", "SRC_NODE" if self.mode == "push" else "DST",
                  "ACK_DISCARD_DEST_WRITES", "FAKE_SSH_FAIL_MATCH", "FAKE_SSH_TRUNCATE_MATCH",
                  "FAKE_SSH_LOSE_MATCH", "FAKE_SSH_DIE_AFTER", "FAKE_ZFS_FAIL_MATCH", "FAKE_ZFS_FAIL_SIDE"):
            self.env.pop(k, None)

    def run(self, script, *args, **env):
        e = dict(self.env); e.update(env)
        r = subprocess.run(["bash", str(KIT / script), *args], capture_output=True, text=True,
                           timeout=60, env=e)
        nested = self.read("nested.log")
        assert not nested, "a node reached over SSH opened another ssh:\n" + nested
        return r

    # the variables that name the OTHER node, in this mode
    def peer_env(self, ip=None, node=None):
        e = {}
        if self.mode == "push":
            if ip: e.update(DST=ip, FAKE_DST_IP=ip)
            if node: e["DST_NODE"] = node
        else:
            if ip: e.update(SRC=ip, FAKE_SRC_IP=ip)
            if node: e["SRC_NODE"] = node
        return e

    def dst_ds(self, n):
        """where the destination receives disk n before the push (pull: the receive container)"""
        return f"rpool/data/vm-{VMID}-disk-{n}" if self.mode == "push" else f"rpool/rtmig-recv/{VMID}/disk-{n}"

    @property
    def peer_host(self):
        return self.dst_host if self.mode == "push" else self.src_host

    @property
    def peer_ip_var(self):
        return "DST" if self.mode == "push" else "SRC"

    def ok(self, script, *args, **env):
        r = self.run(script, *args, **env)
        assert r.returncode == 0, (script, args, out(r))
        return r

    def read(self, rel):
        p = self.t / rel
        return p.read_text() if p.exists() else ""

    def state(self):
        return dict(l.split("=", 1) for l in self.read(f"work/{VMID}.state").splitlines() if "=" in l)

    def set_written(self, side, n):
        p = self.t / f"{side}/zfs.json"; st = json.loads(p.read_text())
        for d in st.values(): d["written"] = n
        p.write_text(json.dumps(st))

    def to_final(self, **env):
        """precopy, hot incr, shutdown, park, final incr"""
        self.ok("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
        self.ok("rtmig_send.sh", "incr", "auto", "rtmig-20990101T0100Z")
        self.ok("rtmig_cutover.sh", "shutdown", "60", **env)
        self.ok("rtmig_cutover.sh", "park")
        self.ok("rtmig_send.sh", "incr", "auto", FINAL, RATE="0")
        return FINAL

    def to_started(self, **env):
        self.to_final(**env)
        self.ok("rtmig_cutover.sh", "push", FINAL, **env)
        self.ok("rtmig_cutover.sh", "start")


class HappyPath(ModeCase):
    def test_full_cold_move_v4_genoa_to_turin(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.ok("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            self.assertEqual(r.stdout.count("VERIFY OK"), 3)
            self.assertIn("discos: 0 1 2", r.stdout)                  # inventory of the config
            self.assertIn("rate=30m", r.stdout)
            r = k.ok("rtmig_send.sh", "incr", "auto", "rtmig-20990101T0100Z", DROP_PREV="1")
            self.assertIn("PREV=auto -> @rtmig-20990101T0000Z", r.stdout)
            r = k.ok("rtmig_cutover.sh", "shutdown", "60")
            self.assertIn("previas OK", r.stdout)
            r = k.ok("rtmig_cutover.sh", "park")
            if k.mode == "push":
                self.assertIn("historial del globo exportado", r.stdout)
            else:                                                     # untrusted origin: never imported
                self.assertIn("no se importa (origen no fiable)", r.stdout)
            self.assertFalse(k.src_conf.exists())
            st = k.state()
            self.assertEqual((st["PHASE"], st["UUID"], st["SRC_NODE"], st["DST_NODE"], st["DISKS"]),
                             ("parked", "11111111-2222-3333-4444-555555555555", "0000999-AX162-TEST",
                              "0000266-KDM-9755", "0 1 2"))
            if k.mode == "push":
                log = k.read("ramguard.log").splitlines()
                self.assertEqual(log[0].split()[:4], ["src", "export-state", VMID, "0000999-AX162-TEST"])
            k.ok("rtmig_send.sh", "incr", "auto", FINAL, RATE="0")
            r = k.ok("rtmig_cutover.sh", "push", FINAL, "47104")
            self.assertEqual(r.stdout.count("disco OK"), 3)
            dconf = k.read(f"dst/etc/pve/qemu-server/{VMID}.conf")
            self.assertIn("balloon: 47104", dconf)
            self.assertIn("cpu: x86-64-v4", dconf)
            self.assertIn("memory: 61440", dconf)
            self.assertEqual(k.state()["CONF_SHA"], hashlib.sha256(dconf.encode()).hexdigest())
            self.assertEqual(k.state()["PHASE"], "pushed")
            self.assertTrue(k.read(f"dst/etc/pve/firewall/{VMID}.fw"))
            self.assertNotIn("dst set", k.read("qm.log"))            # published in one go, no qm set
            imp = [l for l in k.read("ramguard.log").splitlines() if "import-state" in l]
            if k.mode == "push":
                self.assertEqual(imp[0].split(), ["dst", "import-state", VMID, "0000266-KDM-9755",
                                                  k.read(f"work/{VMID}.parked/balloon-state").strip()])
            else:
                self.assertEqual(imp, [])
            k.ok("rtmig_cutover.sh", "start")
            self.assertEqual(k.read(f"dst/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "started")
            k.ok("rtmig_cutover.sh", "close")
            self.assertEqual(k.state()["PHASE"], "done")
            self.assertFalse((k.work / f"{VMID}.parked/conf").exists())
            self.assertEqual(len(json.loads(k.read("src/zfs.json"))), 3)   # source zvols kept

    def test_no_reconciler_on_source_means_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, src_reconciler=False, src_cpu=INTEL_NO512, cpu="x86-64-v3")
            k.to_final()
            k.ok("rtmig_cutover.sh", "push", FINAL, "47104")
            self.assertNotIn("import-state", k.read("ramguard.log"))
            self.assertFalse((k.work / f"{VMID}.parked/balloon-state").exists())

    @push_only
    def test_dest_without_reconciler_skips_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, dst_reconciler=False)
            k.to_final()
            r = k.ok("rtmig_cutover.sh", "push", FINAL)
            self.assertIn("no tiene reconciliador de globo: el historial no se importa", r.stdout)
            self.assertNotIn("import-state", k.read("ramguard.log"))


class ResumableSend(ModeCase):
    def test_base_cut_half_way_resumes_with_the_same_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            (k.t / "recv-cut").write_text(k.dst_ds(1))
            r = k.run("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            self.assertEqual(r.returncode, 1, out(r))
            r = k.ok("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            self.assertIn("ya existe en los 3 discos: lo reutilizo", r.stdout)
            self.assertIn("ya está en el destino", r.stdout)            # disk-2 went before the cut
            self.assertIn("completado al reanudar", r.stdout)            # disk-1 from its token
            self.assertEqual(r.stdout.count("VERIFY OK"), 3)

    def test_resume_command_then_same_base_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            (k.t / "recv-cut").write_text(k.dst_ds(2))
            self.assertEqual(k.run("rtmig_send.sh", "base", "rtmig-20990101T0000Z").returncode, 1)
            r = k.ok("rtmig_send.sh", "resume")
            self.assertIn("vuelve a lanzar la MISMA orden", r.stdout)
            r = k.ok("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            self.assertEqual(r.stdout.count("VERIFY OK"), 3)

    def test_partial_source_snapshot_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            p = k.t / "src/zfs.json"; st = json.loads(p.read_text())
            st[f"rpool/data/vm-{VMID}-disk-0"]["snaps"]["rtmig-x"] = "1"
            st[f"rpool/data/vm-{VMID}-disk-0"]["order"].append("rtmig-x")
            p.write_text(json.dumps(st))
            r = k.run("rtmig_send.sh", "base", "rtmig-x")
            self.assertEqual(r.returncode, 1); self.assertIn("no sería atómico", out(r))

    def test_prev_must_be_an_rtmig_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            k.ok("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            r = k.run("rtmig_send.sh", "incr", "sup-20261117T064954Z", "rtmig-b", DROP_PREV="1")
            self.assertEqual(r.returncode, 1); self.assertIn("debe empezar por rtmig-", out(r))
            self.assertNotIn("destroy", k.read("zfs.log"))

    def test_pending_estimates_with_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            k.ok("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            k.set_written("src", 31457280 * 60)                       # 60 s at 30m
            r = k.ok("rtmig_send.sh", "pending")
            self.assertIn("~60s a RATE=30m", r.stdout)


class Inventory(ModeCase):
    def test_disks_must_match_the_whole_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            for disks in ("0 1", "0", "0 1 2 3"):
                r = k.run("rtmig_send.sh", "base", "rtmig-x", DISKS=disks)
                self.assertEqual(r.returncode, 1, disks); self.assertIn("no coincide con el inventario", out(r))
            r = k.run("rtmig_send.sh", "base", "rtmig-x", DISKS="   ")
            self.assertEqual(r.returncode, 1); self.assertIn("DISKS no válido", out(r))
            self.assertEqual(k.run("rtmig_send.sh", "base", "rtmig-x", DISKS="2 0 1").returncode, 0)

    def test_unsupported_volumes_are_refused(self):
        for extra in ("scsi1: local-lvm:vm-4242-disk-3,size=10G", "scsi1: local-zfs:vm-77-disk-0,size=1G",
                      "unused0: local-zfs:base-9000-disk-0", "vmstate: local-zfs:vm-4242-state-s1",
                      "sata0: /dev/disk/by-id/nvme-x,size=1T"):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + extra + "\n")
                r = k.run("rtmig_send.sh", "base", "rtmig-x")
                self.assertEqual(r.returncode, 1, extra); self.assertIn("volúmenes no soportados", out(r))

    def test_unused_zvol_of_the_vm_travels_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "unused0: local-zfs:vm-4242-disk-3\n")
            p = k.t / "src/zfs.json"; st = json.loads(p.read_text())
            st[f"rpool/data/vm-{VMID}-disk-3"] = {"snaps": {}, "order": [], "written": 0, "token": "-"}
            p.write_text(json.dumps(st))
            r = k.ok("rtmig_send.sh", "base", "rtmig-x")
            self.assertIn("discos: 0 1 2 3", r.stdout); self.assertEqual(r.stdout.count("VERIFY OK"), 4)


class Phases(ModeCase):
    def test_start_needs_a_complete_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "start")
            self.assertEqual(r.returncode, 1); self.assertIn("fase actual 'parked'", out(r))
            self.assertNotIn("dst start", k.read("qm.log"))

    def test_start_refuses_a_config_that_is_not_the_published_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.dst_conf.write_text(k.dst_conf.read_text() + "args: -foo\n")
            r = k.run("rtmig_cutover.sh", "start")
            self.assertEqual(r.returncode, 1); self.assertIn("no es la publicada", out(r))

    def test_push_refuses_if_the_source_config_came_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            k.src_conf.write_text((k.work / f"{VMID}.parked/conf").read_text())
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("dos configs arrancables", out(r))
            self.assertFalse(k.dst_conf.exists())

    def test_state_bound_to_destination_and_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, **k.peer_env(ip="2001:db8:9::3"))
            self.assertEqual(r.returncode, 1); self.assertIn("con el otro nodo en", out(r))
            p = k.work / f"{VMID}.parked/conf"
            p.write_text(p.read_text().replace("uuid=1111", "uuid=9999"))
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("no es la de este traslado", out(r))

    @push_only
    def test_fw_write_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, FAKE_SSH_FAIL_MATCH="firewall/4242.fw.rtmig-tmp")
            self.assertEqual(r.returncode, 1); self.assertIn("la config NO se ha publicado", out(r))
            self.assertFalse(k.dst_conf.exists()); self.assertEqual(k.state()["PHASE"], "publishing")
            k.ok("rtmig_cutover.sh", "push", FINAL)                     # retried from «publishing»
            self.assertEqual(k.state()["PHASE"], "pushed")

    @push_only
    def test_truncated_publish_is_detected_and_withdrawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, FAKE_SSH_TRUNCATE_MATCH="qemu-server/4242.conf.rtmig-tmp")
            self.assertEqual(r.returncode, 1); self.assertIn("no se publicó (BADSHA)", out(r))
            self.assertFalse(k.dst_conf.exists()); self.assertEqual(k.state()["PHASE"], "publishing")
            self.assertFalse((k.t / f"dst/etc/pve/qemu-server/{VMID}.conf.rtmig-tmp").exists())
            k.ok("rtmig_cutover.sh", "push", FINAL)                     # a clean retry works
            self.assertEqual(k.state()["PHASE"], "pushed")

    def test_lock_blocks_a_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.work.mkdir()
            holder = subprocess.Popen(["flock", str(k.work / f"{VMID}.lock"), "sleep", "5"])
            try:
                time.sleep(0.3)
                r = k.run("rtmig_cutover.sh", "status")
                self.assertEqual(r.returncode, 1); self.assertIn("cerrojo", out(r))
            finally:
                holder.kill(); holder.wait()


class Safety(ModeCase):
    def test_wrong_destination_name_stops_before_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-x", **k.peer_env(node="0000234-AX162-2-LTD"))
            self.assertEqual(r.returncode, 1); self.assertIn(f"dice llamarse '{k.peer_host}'", out(r))
            self.assertNotIn(" snapshot ", k.read("zfs.log"))

    def test_parameters_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            node_var = "DST_NODE" if k.mode == "push" else "SRC_NODE"
            for env, needle in (({k.peer_ip_var: "nodo-kademar"}, "IP literal"), ({"WORK": "/root/a b"}, "WORK no válido"),
                                ({"WORK": "/root/../etc"}, "WORK no válido"), ({"POOLDS": "rpool/data;rm"}, "POOLDS no válido"),
                                ({"VMID": "42x"}, "VMID no válida"), ({node_var: "a b"}, f"{node_var} no válido"),
                                ({"RTMIG_MODE": "both"}, "solo admite push o pull")):
                r = k.run("rtmig_send.sh", "base", "rtmig-x", **env)
                self.assertEqual(r.returncode, 2, env); self.assertIn(needle, out(r))
            r = k.run("rtmig_send.sh", "base", "rtmig-x';rm -rf /;'")
            self.assertEqual(r.returncode, 1); self.assertIn("debe empezar por rtmig-", out(r))
            r = k.run("rtmig_cutover.sh", "push", "x';id;'")
            self.assertEqual(r.returncode, 1); self.assertIn("debe empezar por rtmig-", out(r))

    def test_shutdown_never_forces(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "5", FAKE_SHUTDOWN_FAIL="1")
            self.assertEqual(r.returncode, 1); self.assertIn("NO forzar", out(r))
            self.assertNotIn("stop", [l.split()[1] for l in k.read("qm.log").splitlines()])

    def test_push_refuses_if_written_after_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            k.set_written("src", 4096)
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("la final no es la última", out(r))
            self.assertFalse(k.dst_conf.exists())

    def test_park_refuses_a_stale_parked_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); (k.work / f"{VMID}.parked").mkdir(parents=True)
            (k.work / f"{VMID}.parked/fw").write_text("[OPTIONS]\nenable: 0\n")   # left over from an old move
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            k.ok("rtmig_cutover.sh", "shutdown", "60")
            r = k.run("rtmig_cutover.sh", "park")
            self.assertEqual(r.returncode, 1); self.assertIn("queda una copia aparcada anterior", out(r))
            self.assertTrue(k.src_conf.exists())


class Rollback(ModeCase):
    def test_rollback_after_push_without_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertTrue(k.src_conf.exists()); self.assertFalse(k.dst_conf.exists())
            self.assertFalse((k.t / f"dst/etc/pve/firewall/{VMID}.fw").exists())
            self.assertTrue(list(k.work.glob(f"{VMID}.conf.dst-*")))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")
            self.assertFalse((k.work / f"{VMID}.parked/conf").exists())          # archived, never reused
            self.assertTrue(list(k.work.glob(f"{VMID}.parked.rolledback-*")))

    def test_rollback_after_start_needs_explicit_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_started()
            (k.t / f"dst/state-{VMID}").write_text("stopped")             # shut down by a person
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn("ACK_DISCARD_DEST_WRITES=4242", out(r))
            self.assertTrue(k.dst_conf.exists()); self.assertFalse(k.src_conf.exists())
            k.ok("rtmig_cutover.sh", "rollback", ACK_DISCARD_DEST_WRITES=VMID)
            self.assertTrue(k.src_conf.exists()); self.assertIn("DISCARDED", k.read(f"work/{VMID}.state"))

    def test_dest_writes_after_final_need_consent_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.set_written("dst", 12345)
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn("12345 B escritos en destino", out(r))

    def test_rollback_refuses_while_running_on_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_started()
            r = k.run("rtmig_cutover.sh", "rollback", ACK_DISCARD_DEST_WRITES=VMID)
            self.assertEqual(r.returncode, 1); self.assertIn("ORDENADAMENTE", out(r))
            self.assertFalse(k.src_conf.exists())

    def test_ssh_dropping_mid_rollback_never_starts_the_source(self):
        # the reviewer's blocker: SSH answers hostname, then the probe of the destination fails
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_started()
            r = k.run("rtmig_cutover.sh", "rollback", ACK_DISCARD_DEST_WRITES=VMID,
                      FAKE_SSH_FAIL_MATCH="PROBE-END")
            self.assertEqual(r.returncode, 1); self.assertIn("INDETERMINADO", out(r))
            self.assertFalse(k.src_conf.exists())
            self.assertNotIn("src start", k.read("qm.log"))
            self.assertEqual(k.read(f"dst/state-{VMID}").strip(), "running")

    def test_qm_status_failing_on_dest_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_started()
            (k.t / f"dst/state-{VMID}").write_text("stopped")
            r = k.run("rtmig_cutover.sh", "rollback", ACK_DISCARD_DEST_WRITES=VMID, FAKE_QM_STATUS_FAIL="dst")
            self.assertEqual(r.returncode, 1); self.assertIn("qm status no responde", out(r))
            self.assertFalse(k.src_conf.exists())

    @push_only
    def test_written_query_failing_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_SSH_FAIL_MATCH="written@")
            self.assertEqual(r.returncode, 1); self.assertIn("INDETERMINADO", out(r))
            self.assertFalse(k.src_conf.exists())

    @push_only
    def test_ssh_failing_while_parking_dest_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_SSH_FAIL_MATCH="conf.dst-")
            self.assertEqual(r.returncode, 1); self.assertIn("no se ha restaurado nada en origen", out(r))
            self.assertFalse(k.src_conf.exists()); self.assertTrue(k.dst_conf.exists())

    def test_a_new_move_after_rollback_starts_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            # the VM ran again on the origin: a fresh incremental before the next cut (in pull it also
            # adopts the promoted zvols back into the receive container)
            k.ok("rtmig_send.sh", "incr", "auto", "rtmig-again")
            k.ok("rtmig_cutover.sh", "shutdown", "60")
            k.ok("rtmig_cutover.sh", "park")
            self.assertEqual(k.state()["PHASE"], "parked")


class SecondReview(ModeCase):
    """2nd review of PR #291."""

    def test_zfs_error_on_dest_is_indeterminate_not_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_ZFS_FAIL_MATCH="written@")
            self.assertEqual(r.returncode, 1); self.assertIn("zfs falló en destino", out(r))
            self.assertFalse(k.src_conf.exists()); self.assertTrue(k.dst_conf.exists())

    def test_disk_vanished_on_dest_after_push_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            p = k.t / "dst/zfs.json"; st = json.loads(p.read_text())
            del st[f"rpool/data/vm-{VMID}-disk-0"]; p.write_text(json.dumps(st))
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn("INDETERMINADO", out(r))
            self.assertFalse(k.src_conf.exists())

    def test_state_bound_to_the_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            self.assertEqual(k.state()["POOLDS"], "rpool/data")
            r = k.run("rtmig_cutover.sh", "push", FINAL, POOLDS="tank/vms")
            self.assertEqual(r.returncode, 1); self.assertIn("es del pool rpool/data", out(r))

    def test_rollback_never_removes_a_foreign_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()                                   # parked: nothing published yet
            foreign = CONF.format(cpu="host").replace("uuid=1111", "uuid=aaaa")
            k.dst_conf.write_text(foreign)
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn("no es nuestra", out(r))
            self.assertEqual(k.dst_conf.read_text(), foreign); self.assertFalse(k.src_conf.exists())
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.dst_conf.write_text(CONF.format(cpu="host").replace("uuid=1111", "uuid=bbbb"))
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn("no es la de este traslado", out(r))
            self.assertIn("uuid=bbbb", k.dst_conf.read_text())

    def test_rollback_never_removes_a_foreign_fw(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            fw = k.t / f"dst/etc/pve/firewall/{VMID}.fw"; fw.write_text("[OPTIONS]\nenable: 0\n")
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn(".fw de la VM 4242 en destino que no es de este traslado", out(r))
            self.assertTrue(fw.exists())

    def test_close_checks_the_remote_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_started()
            k.dst_conf.write_text(k.dst_conf.read_text().replace("uuid=1111", "uuid=cccc"))
            r = k.run("rtmig_cutover.sh", "close")
            self.assertEqual(r.returncode, 1); self.assertIn("no es la de este traslado", out(r))

    def test_stale_dest_fw_when_source_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            (k.t / f"src/etc/pve/firewall/{VMID}.fw").unlink()
            k.to_final()
            (k.t / f"dst/etc/pve/firewall/{VMID}.fw").write_text("[OPTIONS]\nenable: 1\n")
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("no es de este traslado", out(r))
            self.assertFalse(k.dst_conf.exists())

    def test_fw_hash_recorded_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            # the hash of what was PUBLISHED (in pull, the .fw rewritten from the allow-list)
            fw = k.read(f"dst/etc/pve/firewall/{VMID}.fw")
            self.assertTrue(fw)
            self.assertEqual(k.state()["FW_SHA"], hashlib.sha256(fw.encode()).hexdigest())

    def test_start_reconciles_when_the_confirmation_was_lost(self):
        for phase in ("starting", "pushed"):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
                (k.t / f"dst/state-{VMID}").write_text("running")       # it did start
                st = k.read(f"work/{VMID}.state").replace("PHASE=pushed", f"PHASE={phase}")
                (k.work / f"{VMID}.state").write_text(st)
                r = k.ok("rtmig_cutover.sh", "start")
                self.assertIn("ya está running en destino: reconcilio", r.stdout)
                self.assertEqual(k.state()["PHASE"], "started")
                self.assertNotIn("dst start", k.read("qm.log"))

    def test_close_from_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_started()
            st = k.read(f"work/{VMID}.state").replace("PHASE=started", "PHASE=starting")
            (k.work / f"{VMID}.state").write_text(st)
            k.ok("rtmig_cutover.sh", "close")
            self.assertEqual(k.state()["PHASE"], "done")

    def test_rollback_is_reentrant_if_the_source_start_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_QM_START_FAIL="src")
            self.assertEqual(r.returncode, 1); self.assertIn("fase rollingback", out(r))
            self.assertEqual(k.state()["PHASE"], "rollingback")
            self.assertTrue(k.src_conf.exists()); self.assertFalse(k.dst_conf.exists())
            r = k.ok("rtmig_cutover.sh", "rollback")
            self.assertIn("rollback a medias", r.stdout)
            self.assertEqual(k.state()["PHASE"], "rolledback")
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    @push_only
    def test_pending_cpu_decides_before_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = CONF.format(cpu="x86-64-v4") + "\n[PENDING]\ncpu: host\n"
            k = Kit(tmp, conf=conf); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("cpu=host", out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))
        with tempfile.TemporaryDirectory() as tmp:
            conf = CONF.format(cpu="host") + "\n[PENDING]\ndelete: cpu\n"            # → kvm64 on next boot
            k = Kit(tmp, conf=conf, dst_cpu=GENOA, run_cpu="host"); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("corría con host y arrancará con kvm64", out(r))
            k.ok("rtmig_cutover.sh", "shutdown", "60", ACK_HWID_CHANGE=VMID, CPU_NOTICE="correo")

    @push_only
    def test_pending_balloon_blocks_setting_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[PENDING]\nballoon: 2048\n")
            k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, "47104")
            self.assertEqual(r.returncode, 1); self.assertIn("PENDIENTE de balloon", out(r))

    @push_only
    def test_conf_set_key_ignores_snapshot_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[snap1]\nballoon: 47104\ncpu: host\n")
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL, "47104")
            main, _, snap = k.dst_conf.read_text().partition("[snap1]")
            self.assertIn("balloon: 47104", main)                      # added to the main section
            self.assertIn("cpu: host", snap); self.assertEqual(snap.count("balloon:"), 1)

    def test_repeating_incr_after_all_disks_arrived(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            k.ok("rtmig_send.sh", "incr", "auto", "rtmig-b")
            r = k.ok("rtmig_send.sh", "incr", "auto", "rtmig-b", DROP_PREV="1")   # same order again
            self.assertIn("PREV=@rtmig-a", r.stdout); self.assertEqual(r.stdout.count("VERIFY OK"), 3)
            r = k.ok("rtmig_send.sh", "incr", "auto", "rtmig-b", DROP_PREV="1")   # cleanup idempotent
            r = k.run("rtmig_send.sh", "incr", "rtmig-x", "rtmig-b")
            self.assertEqual(r.returncode, 1); self.assertIn("ya se lanzó con PREV=@rtmig-a", out(r))

    def test_verify_validates_the_snapshot_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Kit(tmp).run("rtmig_send.sh", "verify", "x';id;'")
            self.assertEqual(r.returncode, 1); self.assertIn("debe empezar por rtmig-", out(r))

    def test_dst_must_be_canonical_and_without_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            for bad in ("2001:db8:2:0::2", "fe80::1%eth0", "2001:DB8:2::2", " 2001:db8:2::2"):
                r = k.run("rtmig_send.sh", "base", "rtmig-a", **{k.peer_ip_var: bad})
                self.assertEqual(r.returncode, 2, bad); self.assertIn("canónica", out(r))


class ThirdReview(ModeCase):
    """3rd review of PR #291."""

    def test_rollback_starts_only_with_conf_and_fw_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_QM_START_FAIL="src")      # cut after restoring
            self.assertEqual(r.returncode, 1); self.assertEqual(k.state()["PHASE"], "rollingback")
            fw = k.t / f"src/etc/pve/firewall/{VMID}.fw"
            fw.write_text("[OPTIONS]\nen")                                          # truncated firewall
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1); self.assertIn("no arranco sin su firewall", out(r))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "stopped")
            fw.unlink()                                                             # missing firewall
            r = k.ok("rtmig_cutover.sh", "rollback")
            self.assertEqual(fw.read_text(), k.read(f"work/{VMID}.parked/fw") or "[OPTIONS]\nenable: 1\n")
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")

    def test_rollback_keeps_a_copy_of_the_dest_fw(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertTrue(list(k.work.glob(f"{VMID}.fw.dst-*")))
            self.assertTrue(list(k.work.glob(f"{VMID}.conf.dst-*")))

    @push_only
    def test_running_host_with_pending_v4_needs_the_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = CONF.format(cpu="host") + "\n[PENDING]\ncpu: x86-64-v4\n"
            k = Kit(tmp, conf=conf, dst_cpu=GENOA, run_cpu="host"); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("corría con host y arrancará con x86-64-v4", out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=conf, dst_cpu=GENOA, run_cpu="host")
            env = {"ACK_HWID_CHANGE": VMID, "CPU_NOTICE": "correo 21/11"}
            k.to_final(**env)
            self.assertEqual(k.state()["RUN_CPU"], "host")
            r = k.run("rtmig_cutover.sh", "push", FINAL)                  # without ACK: push re-checks
            self.assertEqual(r.returncode, 1); self.assertIn("cambio de modelo pendiente", out(r))
            k.ok("rtmig_cutover.sh", "push", FINAL, **env)
            self.assertIn("cambio de modelo pendiente: corría con host", k.read("work/cpu-change.txt"))

    def test_unreadable_running_cpu_refuses_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.pidfile.unlink()
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("no pude leer con qué CPU corre", out(r))

    @push_only
    def test_lost_answer_after_rename_still_finishes_the_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, cpu="host", dst_cpu=TURIN)
            env = {"ACK_HWID_CHANGE": VMID, "CPU_NOTICE": "correo 22/11"}
            k.to_final(**env)
            r = k.run("rtmig_cutover.sh", "push", FINAL, FAKE_SSH_LOSE_MATCH="qemu-server/4242.conf.rtmig-tmp", **env)
            self.assertEqual(r.returncode, 0, out(r)); self.assertIn("se perdió", out(r))
            self.assertEqual(k.state()["PHASE"], "pushed")
            self.assertEqual(sum("import-state" in l for l in k.read("ramguard.log").splitlines()), 1)
            self.assertEqual(k.read("work/cpu-change.txt").count("ACK_HWID_CHANGE"), 1)

    @push_only
    def test_resumed_push_runs_the_same_finish_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, cpu="host", dst_cpu=TURIN)
            env = {"ACK_HWID_CHANGE": VMID, "CPU_NOTICE": "correo 22/11"}
            k.to_final(**env)
            # the first push dies right after the rename, before its finish (answer AND probe lost)
            r = k.run("rtmig_cutover.sh", "push", FINAL, FAKE_SSH_LOSE_MATCH="qemu-server/4242.conf.rtmig-tmp",
                      FAKE_SSH_DIE_AFTER="qemu-server/4242.conf.rtmig-tmp", **env)
            self.assertEqual(r.returncode, 1); self.assertTrue(k.dst_conf.exists())
            (k.t / "ssh-dead").unlink()                                     # the link comes back
            self.assertEqual(k.state()["PHASE"], "publishing")
            r = k.ok("rtmig_cutover.sh", "push", FINAL)                    # retried WITHOUT the variables
            self.assertIn("completo el cierre del push", r.stdout)
            self.assertEqual(k.state()["PHASE"], "pushed")
            self.assertEqual(sum("import-state" in l for l in k.read("ramguard.log").splitlines()), 1)
            self.assertEqual(k.read("work/cpu-change.txt").count("ACK_HWID_CHANGE"), 1)
            self.assertIn("correo 22/11", k.read("work/cpu-change.txt"))

    def test_start_verifies_the_fw_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            (k.t / f"dst/etc/pve/firewall/{VMID}.fw").write_text("[OPTIONS]\nenable: 0\n")
            r = k.run("rtmig_cutover.sh", "start")
            self.assertEqual(r.returncode, 1); self.assertIn("el .fw del destino no es el publicado", out(r))
            self.assertNotIn("dst start", k.read("qm.log"))

    def test_drop_prev_aborts_on_zfs_errors_before_destroying(self):
        for side in ("dst", "src"):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp)
                k.ok("rtmig_send.sh", "base", "rtmig-a")
                k.ok("rtmig_send.sh", "incr", "auto", "rtmig-b")
                r = k.run("rtmig_send.sh", "incr", "auto", "rtmig-b", DROP_PREV="1",
                          FAKE_ZFS_FAIL_MATCH="@rtmig-a", FAKE_ZFS_FAIL_SIDE=side)
                self.assertEqual(r.returncode, 1, side); self.assertIn("INDETERMINADO", out(r))
                self.assertNotIn("destroy", k.read("zfs.log"), side)

    def test_archive_interrupted_after_the_terminal_phase_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            for f in k.work.glob(f"{VMID}.*.rolledback-*"):              # pretend the archive never ran
                f.rename(str(f).split(".rolledback-")[0])
            st = k.read(f"work/{VMID}.state").replace("ARCHIVE_PENDING=", "ARCHIVE_PENDING=rolledback")
            (k.work / f"{VMID}.state").write_text(st)
            self.assertTrue((k.work / f"{VMID}.parked/conf").exists())
            k.ok("rtmig_cutover.sh", "status")
            self.assertFalse((k.work / f"{VMID}.parked/conf").exists())
            self.assertEqual(k.state()["ARCHIVE_PENDING"], "")
            k.ok("rtmig_send.sh", "incr", "auto", "rtmig-again")
            k.ok("rtmig_cutover.sh", "shutdown", "60"); k.ok("rtmig_cutover.sh", "park")


class CpuGuard(ModeCase):
    def push(self, cpu, src_cpu=GENOA, dst_cpu=TURIN, **env):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        k = Kit(tmp.name, cpu=cpu, src_cpu=src_cpu, dst_cpu=dst_cpu)
        k.to_final(**env)
        return k, k.run("rtmig_cutover.sh", "push", FINAL, **env)

    def test_checked_before_shutting_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, cpu="host")
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("ACK_HWID_CHANGE=4242", out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    def test_ack_needs_the_notice_and_the_right_vmid(self):
        for env, needle in (({"ACK_HWID_CHANGE": VMID}, "CPU_NOTICE"),
                            ({"ACK_HWID_CHANGE": "1", "CPU_NOTICE": "x"}, "no es esta VM")):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp, cpu="host"); k.ok("rtmig_send.sh", "base", "rtmig-a")
                r = k.run("rtmig_cutover.sh", "shutdown", "60", **env)
                self.assertEqual(r.returncode, 1); self.assertIn(needle, out(r))

    def test_ack_keeps_host_and_records_it_once_published(self):
        k, r = self.push("host", ACK_HWID_CHANGE=VMID, CPU_NOTICE="correo 20/11")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertIn("cpu: host", k.read(f"dst/etc/pve/qemu-server/{VMID}.conf"))
        rec = k.read("work/cpu-change.txt")
        self.assertEqual(rec.count("VM4242 cpu host sin cambiar"), 1)
        self.assertIn("AuthenticAMD|25|17 -> AuthenticAMD|26|2", rec)
        self.assertIn("correo 20/11", rec)

    def test_change_cpu_sets_v4_and_keeps_source_config(self):
        k, r = self.push("host", CHANGE_CPU="1", CPU_NOTICE="correo 20/11")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertIn("cpu: x86-64-v4", k.read(f"dst/etc/pve/qemu-server/{VMID}.conf"))
        self.assertIn("cpu: host", k.read(f"work/{VMID}.parked/conf"))
        self.assertIn("host -> x86-64-v4", k.read("work/cpu-change.txt"))

    def test_both_switches_are_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, cpu="host"); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60", CHANGE_CPU="1", ACK_HWID_CHANGE=VMID, CPU_NOTICE="x")
            self.assertEqual(r.returncode, 1); self.assertIn("excluyentes", out(r))

    def test_host_on_identical_silicon_needs_nothing(self):
        k, r = self.push("host", dst_cpu=GENOA)
        self.assertEqual(r.returncode, 0, out(r))
        self.assertFalse((k.work / "cpu-change.txt").exists())

    def test_named_or_missing_model_treated_like_host(self):
        for cpu in ("EPYC-Genoa", "kvm64"):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp, cpu=cpu); k.ok("rtmig_send.sh", "base", "rtmig-a")
                self.assertEqual(k.run("rtmig_cutover.sh", "shutdown", "60").returncode, 1, cpu)

    def test_v4_to_a_host_without_avx512(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, dst_cpu=INTEL_NO512); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("AVX-512", out(r))
        k, r = self.push("x86-64-v4", dst_cpu=INTEL_NO512, CHANGE_CPU="1", CPU_NOTICE="x")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertIn("cpu: x86-64-v3", k.read(f"dst/etc/pve/qemu-server/{VMID}.conf"))


class PullMode(unittest.TestCase):
    """Iván «B+» (25/11/2026): a node of another provider (0000266-KDM-9755) has NO fleet key. Fleet
    nodes may ssh into it, it may not ssh into any of ours. When it is the ORIGIN the kit runs on the
    Hetzner destination (RTMIG_MODE=pull) and opens every connection itself."""

    def kademar_origin(self, tmp, **kw):
        return Kit(tmp, mode="pull", src_host="0000266-KDM-9755", dst_host="0000238-AX162-2-LTD",
                   src_cpu=TURIN, dst_cpu=GENOA, **kw)

    def test_full_pull_move_from_kademar_never_connects_out_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            self.assertFalse((k.t / "src/neuravps_id").exists())          # the origin has no fleet key
            k.to_started()
            k.ok("rtmig_cutover.sh", "close")
            self.assertEqual(k.state()["MODE"], "pull")
            self.assertEqual((k.state()["SRC_NODE"], k.state()["DST_NODE"]), ("0000266-KDM-9755", "0000238-AX162-2-LTD"))
            self.assertTrue(k.dst_conf.exists()); self.assertFalse(k.src_conf.exists())
            self.assertEqual(k.read(f"dst/state-{VMID}").strip(), "running")
            ssh_lines = k.read("ssh.log").splitlines()
            self.assertTrue(ssh_lines)
            # every ssh was opened by the destination, towards the origin, and nothing nested
            self.assertTrue(all(l.startswith("dst> root@2001:db8:1::2 :: ") for l in ssh_lines), ssh_lines[:3])
            self.assertEqual(k.read("nested.log"), "")
            # the work dir (state, parked copy, history) lives on the destination that runs the kit
            self.assertTrue(list(k.work.glob(f"{VMID}.parked.done-*")))
            # zfs streams: send on the origin, receive on the destination
            z = k.read("zfs.log").splitlines()
            self.assertTrue(any(l.startswith("src send ") for l in z)); self.assertTrue(any(l.startswith("dst recv ") for l in z))
            self.assertFalse(any(l.startswith("dst send ") or l.startswith("src recv ") for l in z))

    def test_pull_rollback_restores_on_kademar_from_the_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertTrue(k.src_conf.exists()); self.assertFalse(k.dst_conf.exists())
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertTrue(all(l.startswith("dst> root@2001:db8:1::2 :: ") for l in k.read("ssh.log").splitlines()))

    def test_push_from_a_node_without_the_fleet_key_points_to_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, mode="push", src_host="0000266-KDM-9755", dst_host="0000238-AX162-2-LTD")
            r = k.run("rtmig_send.sh", "base", "rtmig-a", NV_FLEET_KEY=str(k.t / "src/no-key"))
            self.assertEqual(r.returncode, 1)
            self.assertIn("RTMIG_MODE=pull SRC=<IP de este nodo> SRC_NODE=0000266-KDM-9755", out(r))
            self.assertEqual(k.read("ssh.log"), "")                       # did not even try

    def test_pull_runs_on_the_destination_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-a", DST_NODE="0000999-OTRO")
            self.assertEqual(r.returncode, 2); self.assertIn("el kit corre en el DESTINO", out(r))
            env = dict(k.env); env.pop("DST_NODE")
            r = subprocess.run(["bash", str(KIT / "rtmig_send.sh"), "base", "rtmig-a"], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 2); self.assertIn("hace falta DST_NODE explícito", out(r))
            r = k.run("rtmig_send.sh", "base", "rtmig-a", NV_FLEET_KEY=str(k.t / "dst/no-key"))
            self.assertEqual(r.returncode, 1); self.assertIn("tiene que correr en un nodo de Hetzner", out(r))

    def test_state_of_one_mode_is_not_usable_from_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp); k.to_final()
            st = k.read(f"work/{VMID}.state").replace("MODE=pull", "MODE=push")
            (k.work / f"{VMID}.state").write_text(st)
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("en modo push, no pull", out(r))

    def test_ssh_to_kademar_dropping_during_the_restore_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_SSH_FAIL_MATCH="qemu-server/4242.conf.rtmig-tmp")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(k.state()["PHASE"], "rollingback")
            self.assertNotIn("src start", k.read("qm.log"))               # never started half restored
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")

    def test_ssh_to_kademar_dropping_is_indeterminate_never_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_SSH_FAIL_MATCH="PROBE-END")
            self.assertEqual(r.returncode, 1); self.assertIn("INDETERMINADO en origen 0000266-KDM-9755", out(r))
            self.assertTrue(k.dst_conf.exists()); self.assertFalse(k.src_conf.exists())
            r = k.run("rtmig_send.sh", "verify", FINAL, FAKE_SSH_FAIL_MATCH="zfs get")
            self.assertEqual(r.returncode, 1); self.assertIn("VERIFY FAIL", out(r))

    def test_park_cut_while_removing_the_config_on_kademar(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60")
            r = k.run("rtmig_cutover.sh", "park", FAKE_SSH_FAIL_MATCH='rm -f "${PVE_ETC')
            self.assertEqual(r.returncode, 1)
            self.assertEqual(k.state()["PHASE"], "parked"); self.assertTrue(k.src_conf.exists())
            r = k.run("rtmig_cutover.sh", "push", FINAL)                    # two bootable configs: refused
            self.assertEqual(r.returncode, 1); self.assertIn("dos configs arrancables", out(r))
            r = k.ok("rtmig_cutover.sh", "park")                           # park resumes
            self.assertIn("park retomado", r.stdout); self.assertFalse(k.src_conf.exists())

    def test_rollback_from_a_park_cut_half_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(k.run("rtmig_cutover.sh", "park", FAKE_SSH_FAIL_MATCH='rm -f "${PVE_ETC').returncode, 1)
            r = k.ok("rtmig_cutover.sh", "rollback")
            self.assertIn("park a medias", r.stdout)
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")

    def test_park_cut_while_reading_the_fw_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60")
            r = k.run("rtmig_cutover.sh", "park", FAKE_SSH_FAIL_MATCH='cat "${PVE_ETC:-/etc/pve}/firewall/')
            self.assertEqual(r.returncode, 1)
            self.assertFalse((k.work / f"{VMID}.parked").exists())           # nothing «parked» half way
            self.assertTrue(k.src_conf.exists()); self.assertEqual(k.state().get("PHASE"), "preparing")
            k.ok("rtmig_cutover.sh", "park")                              # simply repeat it
            self.assertEqual(k.state()["PHASE"], "parked")

    def test_nested_ssh_is_detected_by_the_fakes(self):
        # guard for the guard: a command run over ssh that tries to ssh again is caught
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kademar_origin(tmp)
            env = dict(k.env)
            r = subprocess.run(["ssh", "root@2001:db8:1::2", "ssh root@2001:db8:2::2 hostname"],
                               capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("src -> root@2001:db8:2::2", k.read("nested.log"))


class PullHostile(unittest.TestCase):
    """Coordinator's rule for #296: in pull mode the origin (Kademar) is NOT TRUSTED. Everything it
    returns is validated on the Hetzner node as hostile input; nothing of it is executed or evaluated;
    its config and .fw reach the destination only through a local allow-list profile."""

    def kit(self, tmp, **kw):
        return Kit(tmp, mode="pull", src_host="0000266-KDM-9755", dst_host="0000238-AX162-2-LTD",
                   src_cpu=TURIN, dst_cpu=GENOA, **kw)

    def test_pending_counter_is_never_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            for bad in ("a[$(touch /tmp/pwned-rtmig)]", "1e3", "-5", "99999999999999999999"):
                r = k.run("rtmig_send.sh", "pending", FAKE_HOSTILE_WRITTEN=bad)
                self.assertEqual(r.returncode, 1, bad); self.assertIn("formato raro", out(r))
            self.assertFalse(os.path.exists("/tmp/pwned-rtmig"))

    def test_hostile_guid_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-a", FAKE_HOSTILE_GUID="$(id)")
            self.assertEqual(r.returncode, 1); self.assertIn("guid con formato raro", out(r))

    def profile_fail(self, extra, needle):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp, conf=CONF.format(cpu="x86-64-v4") + extra)
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1, extra); self.assertIn("no cumple el perfil", out(r)); self.assertIn(needle, out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))                # checked BEFORE stopping it

    def test_profile_refuses_dangerous_keys(self):
        self.profile_fail("args: -device foo\n", "args")
        self.profile_fail("hostpci0: 0000:01:00.0\n", "hostpci0")
        self.profile_fail("usb0: host=1234:5678\n", "usb0")
        self.profile_fail("serial0: socket\n", "serial0")
        self.profile_fail("\n[PENDING]\ncpu: host\n", "[PENDING]")

    def test_profile_refuses_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = CONF.format(cpu="x86-64-v4").replace("bridge=vmbr0", "bridge=vmbr9")
            k = self.kit(tmp, conf=conf); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("net0=", out(r))

    def test_published_config_is_the_profile_with_our_hookscript(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = (CONF.format(cpu="x86-64-v4").replace("hookscript: shared:snippets/sync-dnat.py", "hookscript: local:snippets/evil.sh")
                    + "meta: creation-qemu=11.0.3\ndescription: hola\n\n[snap1]\nballoon: 1\nargs: -evil\n")
            k = self.kit(tmp, conf=conf.replace("description: hola\n", ""))
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            pub = k.dst_conf.read_text()
            self.assertIn("hookscript: shared:snippets/sync-dnat.py", pub)
            self.assertNotIn("evil", pub); self.assertNotIn("[snap1]", pub); self.assertNotIn("meta:", pub)
            self.assertIn("smbios1: uuid=11111111-2222-3333-4444-555555555555", pub)
            # the original stays parked for a rollback to Kademar
            self.assertIn("local:snippets/evil.sh", k.read(f"work/{VMID}.parked/conf"))

    def test_fw_is_rewritten_from_the_allow_list_or_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            (k.t / f"src/etc/pve/firewall/{VMID}.fw").write_text(
                "[OPTIONS]\n\nenable: 1\n\n[RULES]\n\n|GROUP vm-no-internet\nGROUP vm-default # x\n")
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(k.read(f"dst/etc/pve/firewall/{VMID}.fw"),
                             "[OPTIONS]\n\nenable: 1\n\n[RULES]\n\n|GROUP vm-no-internet\nGROUP vm-default\n")
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            (k.t / f"src/etc/pve/firewall/{VMID}.fw").write_text("[OPTIONS]\nenable: 1\npolicy_in: ACCEPT\n[RULES]\nIN ACCEPT -p tcp\n")
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("el .fw del origen no cumple el perfil", out(r))

    def test_balloon_history_from_an_untrusted_origin_is_not_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(k.read("ramguard.log"), "")
            self.assertFalse((k.work / f"{VMID}.parked/balloon-state").exists())

    def test_hostile_snapshot_names_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            p = k.t / "src/zfs.json"; st = json.loads(p.read_text())
            d0 = st[f"rpool/data/vm-{VMID}-disk-0"]
            d0["snaps"]["rtmig-x;id"] = "1"; d0["order"].append("rtmig-x;id")
            p.write_text(json.dumps(st))
            r = k.ok("rtmig_send.sh", "last")
            self.assertEqual(r.stdout.strip().splitlines()[-1], "rtmig-a")

    def test_agent_is_never_forwarded_to_the_peer(self):
        s = (KIT / "rtmig_common.sh").read_text()
        self.assertIn("SSHO=(-a -x -o ForwardAgent=no -o ClearAllForwardings=yes", s)
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")


class SecondHostileReview(unittest.TestCase):
    """2nd Codex review of #296."""

    def kit(self, tmp, **kw):
        return Kit(tmp, mode="pull", src_host="0000266-KDM-9755", dst_host="0000238-AX162-2-LTD",
                   src_cpu=TURIN, dst_cpu=GENOA, **kw)

    def dst_zfs(self, k):
        return json.loads(k.read("dst/zfs.json") or "{}")

    # 1. the .fw is fetched (sha-verified) and checked BEFORE stopping
    def test_cut_fw_read_during_shutdown_never_stops_the_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            (k.t / f"src/etc/pve/firewall/{VMID}.fw").write_text("[OPTIONS]\npolicy_in: DROP\nenable: 1\n")
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60", FAKE_SSH_FAIL_MATCH='cat "${PVE_ETC:-/etc/pve}/firewall/')
            self.assertEqual(r.returncode, 1)
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertNotIn("shutdown", k.read("qm.log"))

    def test_policy_in_drop_is_a_valid_fw(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            (k.t / f"src/etc/pve/firewall/{VMID}.fw").write_text("[OPTIONS]\npolicy_in: DROP\nenable: 1\n")
            k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            self.assertIn("policy_in: DROP", k.read(f"dst/etc/pve/firewall/{VMID}.fw"))

    # 2. preparing: everything between the shutdown and «parked» can be repeated or rolled back
    def promoted_but_not_committed(self, k):
        """park finished, then pretend its last state write and the rm on the origin never happened"""
        k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60"); k.ok("rtmig_cutover.sh", "park")
        k.src_conf.write_text(k.read(f"work/{VMID}.parked/conf"))
        (k.t / f"src/etc/pve/firewall/{VMID}.fw").write_text(k.read(f"work/{VMID}.parked/fw"))
        st = k.read(f"work/{VMID}.state").replace("PHASE=parked", "PHASE=preparing")
        (k.work / f"{VMID}.state").write_text(st)

    def test_park_resumes_a_promotion_that_was_not_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); self.promoted_but_not_committed(k)
            r = k.ok("rtmig_cutover.sh", "park")
            self.assertIn("ya estaba promovida", r.stdout)
            self.assertEqual(k.state()["PHASE"], "parked"); self.assertFalse(k.src_conf.exists())

    def test_rollback_from_preparing_restarts_the_untouched_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); self.promoted_but_not_committed(k)
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")
            self.assertTrue(list(k.work.glob(f"{VMID}.parked.rolledback-*")))

    def test_rollback_from_stopping_when_windows_did_not_stop(self):
        for fail in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
                if fail:
                    self.assertEqual(k.run("rtmig_cutover.sh", "shutdown", "5", FAKE_SHUTDOWN_FAIL="1").returncode, 1)
                else:
                    k.ok("rtmig_cutover.sh", "shutdown", "5")
                self.assertEqual(k.state()["PHASE"], "stopping")
                k.ok("rtmig_cutover.sh", "rollback")
                self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
                self.assertEqual(k.state()["PHASE"], "rolledback")

    def test_park_refuses_an_origin_config_changed_since_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60")
            k.src_conf.write_text(k.src_conf.read_text() + "args: -evil\n")
            r = k.run("rtmig_cutover.sh", "park")
            self.assertEqual(r.returncode, 1); self.assertIn("cambió desde el shutdown", out(r))

    # 3. receive container
    def test_receive_goes_to_a_bounded_container_then_is_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            z = self.dst_zfs(k)
            self.assertEqual(sorted(n for n in z if n.startswith("rpool/rtmig-recv/")),
                             [f"rpool/rtmig-recv/{VMID}"] + [f"rpool/rtmig-recv/{VMID}/disk-{n}" for n in (0, 1, 2)])
            self.assertFalse(any(n.startswith("rpool/data/") for n in z))
            q = int(z[f"rpool/rtmig-recv/{VMID}"]["props"]["quota"])
            want = (512 << 30) + (5 << 20)
            self.assertEqual(q, want + want // 10 + (1 << 30))
            self.assertEqual(z[f"rpool/rtmig-recv/{VMID}"]["props"]["reservation"], str(q))
            recv = [l for l in k.read("zfs.log").splitlines() if l.startswith("dst recv ")]
            self.assertTrue(recv and all(" -u " in l and "-x mountpoint" in l and "-x canmount" in l for l in recv))
            k.ok("rtmig_send.sh", "incr", "auto", "rtmig-b"); k.ok("rtmig_cutover.sh", "shutdown", "60")
            k.ok("rtmig_cutover.sh", "park"); k.ok("rtmig_send.sh", "incr", "auto", FINAL, RATE="0")
            k.ok("rtmig_cutover.sh", "push", FINAL)
            z = self.dst_zfs(k)
            self.assertFalse(any(n.startswith("rpool/rtmig-recv/") for n in z))
            self.assertEqual(sorted(n for n in z if n.startswith("rpool/data/")),
                             [f"rpool/data/vm-{VMID}-disk-{n}" for n in (0, 1, 2)])

    def test_hostile_streams_are_destroyed_not_promoted(self):
        for hs, needle in (("filesystem", "(filesystem)"), ("extra", "evil"), ("big", "volsize")):
            with tempfile.TemporaryDirectory() as tmp:
                k = self.kit(tmp)
                r = k.run("rtmig_send.sh", "base", "rtmig-a", FAKE_HOSTILE_STREAM=hs)
                self.assertEqual(r.returncode, 1, hs); self.assertIn("no es lo esperado", out(r)); self.assertIn(needle, out(r))
                z = self.dst_zfs(k)
                self.assertFalse(any(n.startswith(f"rpool/rtmig-recv/{VMID}") for n in z), hs)
                self.assertFalse(any(n.startswith("rpool/data/") for n in z), hs)

    def test_declared_disks_beyond_the_allocation_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-a", ALLOC_DISK_GB="100")
            self.assertEqual(r.returncode, 1); self.assertIn("pasan de la asignación", out(r))
            self.assertEqual(self.dst_zfs(k), {})

    # 4. resources from the trusted allocation; MAC checked before stopping
    def test_resources_come_from_the_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = CONF.format(cpu="x86-64-v4").replace("memory: 61440", "memory: 999999").replace("cores: 8", "cores: 64") + "balloon: 900000\n"
            k = self.kit(tmp, conf=conf)
            k.to_final(); r = k.ok("rtmig_cutover.sh", "push", FINAL)
            pub = k.dst_conf.read_text()
            self.assertIn("memory: 61440", pub); self.assertIn("cores: 8", pub); self.assertIn("sockets: 1", pub)
            self.assertIn("balloon: 61440", pub); self.assertNotIn("999999", pub)
            r = k.run("rtmig_cutover.sh", "status")
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-a", ALLOC_CORES="")
            self.assertEqual(r.returncode, 2); self.assertIn("ALLOC_CORES", out(r))

    def test_balloon_argument_cannot_exceed_the_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, "70000")
            self.assertEqual(r.returncode, 1); self.assertIn("> memoria asignada", out(r))

    def test_mac_in_use_on_the_destination_refuses_before_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            (k.t / "dst/etc/pve/qemu-server/555.conf").write_text("net0: virtio=bc:24:11:00:00:01,bridge=vmbr0\n")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("ya la usa otra VM", out(r)); self.assertIn("555.conf", out(r))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    def test_mac_prefix_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = self.kit(tmp, conf=CONF.format(cpu="x86-64-v4").replace("BC:24:11:00:00:01", "02:00:00:00:00:01"))
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("prefijo esperado", out(r))
            k.ok("rtmig_cutover.sh", "shutdown", "60", EXPECTED_MAC_PREFIX="02:00:00")

    def test_rtmig_alloc_reads_the_trusted_allocation(self):
        fb = textwrap.dedent("""\
            _apps = {}
            def initialize_app(c): _apps["x"] = c
            """)
        fs = textwrap.dedent("""\
            import json, os
            class _D:
                def __init__(self, i, d): self.id = i; self._d = d
                def to_dict(self): return self._d
            class _Q:
                def __init__(self, v): self.v = v
                def stream(self):
                    return [_D(i, d) for i, d in json.loads(os.environ["FAKE_SERVERS"]).items() if d.get("proxmoxId") == self.v]
            class _C:
                def where(self, f, op, v): return _Q(v)
            class _Cl:
                def collection(self, n): assert n == "servers"; return _C()
            def client(): return _Cl()
            """)
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d, "firebase_admin"); pkg.mkdir()
            (pkg / "__init__.py").write_text(fb); (pkg / "credentials.py").write_text("def Certificate(p): return p\n")
            (pkg / "firestore.py").write_text(fs)
            run = lambda servers, vmid: subprocess.run(["python3", str(KIT / "rtmig_alloc.py"), vmid], capture_output=True, text=True,
                                                       env=dict(os.environ, PYTHONPATH=d, FAKE_SERVERS=json.dumps(servers)))
            r = run({"a": {"proxmoxId": 1517, "memoryGb": 19, "cores": 5, "diskGb": 136}}, "1517")
            self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "ALLOC_MEMORY_MB=19456 ALLOC_CORES=5 ALLOC_DISK_GB=136")
            self.assertEqual(run({}, "1517").returncode, 2)
            self.assertEqual(run({"a": {"proxmoxId": 1, "memoryGb": 1, "cores": 1, "diskGb": 1},
                                  "b": {"proxmoxId": 1, "memoryGb": 1, "cores": 1, "diskGb": 1}}, "1").returncode, 2)
            self.assertEqual(run({"a": {"proxmoxId": 9, "memoryGb": 19, "cores": "x", "diskGb": 1}}, "9").returncode, 2)
            self.assertEqual(run({}, "01").returncode, 2)


class ShutdownNeedsPrecopy(ModeCase):
    def test_no_common_base_no_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("haz antes la precopia", out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")


class OldStateMigration(unittest.TestCase):
    def test_push_state_of_the_previous_version_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, mode="push"); k.to_final()
            st = "\n".join(l for l in k.read(f"work/{VMID}.state").splitlines()
                           if not l.startswith(("MODE=", "PEER="))) + "\nDST=2001:db8:2::2\n"
            (k.work / f"{VMID}.state").write_text(st)
            r = k.ok("rtmig_cutover.sh", "push", FINAL)
            self.assertIn("estado de la versión anterior del kit migrado", r.stdout)
            self.assertEqual((k.state()["MODE"], k.state()["PEER"]), ("push", "2001:db8:2::2"))

    def test_old_state_for_another_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, mode="push"); k.to_final()
            st = "\n".join(l for l in k.read(f"work/{VMID}.state").splitlines()
                           if not l.startswith(("MODE=", "PEER="))) + "\nDST=2001:db8:7::7\n"
            (k.work / f"{VMID}.state").write_text(st)
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("versión anterior del kit", out(r))


class ReturnToTheSameNode(unittest.TestCase):
    """(b) Hetzner A → Kademar (push) and back Kademar → A (pull): A kept its zvols and @rtmig-*
    snapshots, so the way back is `incr auto` (same guids); `base` refuses without overwriting."""

    def test_round_trip_uses_incr_auto_on_the_retained_zvols(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, mode="push", src_host="0000238-AX162-2-LTD", dst_host="0000266-KDM-9755",
                    src_cpu=GENOA, dst_cpu=TURIN)
            k.to_started(); k.ok("rtmig_cutover.sh", "close")             # the VM now lives on Kademar
            a_guids = {n: v["snaps"].get(FINAL) for n, v in json.loads(k.read("src/zfs.json")).items()}
            self.assertTrue(all(a_guids.values()))                         # A kept FINAL on its 3 zvols
            # back: the kit now runs on A (fake side "src") in pull mode, Kademar (fake "dst") is the origin
            back = dict(k.env, RTMIG_MODE="pull", SRC="2001:db8:2::2", SRC_NODE="0000266-KDM-9755",
                        DST_NODE="0000238-AX162-2-LTD", ALLOC_MEMORY_MB="61440", ALLOC_CORES="8", ALLOC_DISK_GB="512")
            back.pop("DST")
            (k.t / "dst/run").mkdir(); (k.t / "dst/run" / f"{VMID}.pid").write_text("777\n")
            (k.t / "dst/proc/777").mkdir(parents=True)
            (k.t / "dst/proc/777/cmdline").write_bytes(b"/usr/bin/kvm\0-cpu\0qemu64,+avx512f\0")
            run = lambda *a: subprocess.run(["bash", str(KIT / a[0]), *a[1:]], capture_output=True, text=True, env=back)
            r = run("rtmig_send.sh", "base", "rtmig-back-a")
            self.assertEqual(r.returncode, 1); self.assertIn("no piso nada", out(r))
            r = run("rtmig_send.sh", "incr", "auto", "rtmig-back-1")
            self.assertEqual(r.returncode, 0, out(r)); self.assertIn(f"PREV=auto -> @{FINAL}", r.stdout)
            self.assertEqual(r.stdout.count("VERIFY OK"), 3)
            for step in (("rtmig_cutover.sh", "shutdown", "60"), ("rtmig_cutover.sh", "park"),
                         ("rtmig_send.sh", "incr", "auto", "rtmig-back-final"),
                         ("rtmig_cutover.sh", "push", "rtmig-back-final"), ("rtmig_cutover.sh", "start")):
                r = run(*step); self.assertEqual(r.returncode, 0, (step, out(r)))
            self.assertTrue(k.src_conf.exists()); self.assertFalse(k.dst_conf.exists())
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.read("nested.log"), "")


for _cls in (HappyPath, ResumableSend, Inventory, Phases, Safety, Rollback, SecondReview, ThirdReview, CpuGuard,
             ShutdownNeedsPrecopy):
    globals()["Pull" + _cls.__name__] = type("Pull" + _cls.__name__, (_cls,), {"MODE": "pull"})
del _cls


class Deploy(unittest.TestCase):
    def deploy(self, **env):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        k = Kit(tmp.name, mode="push")
        e = dict(k.env, JUMP="b9", FAKE_JUMP="b9", SRC="2001:db8:1::2", FAKE_SRC_IP="2001:db8:1::2",
                 DST="2001:db8:2::2", WORK=str(k.t / "deploy"))
        e.update(env)
        return subprocess.run(["bash", str(KIT / "rtmig_deploy.sh")], capture_output=True, text=True,
                              timeout=60, env=e)

    def test_copies_and_compares_hashes(self):
        r = self.deploy()
        self.assertEqual(r.returncode, 0, out(r)); self.assertEqual(r.stdout.count("md5 iguales"), 2)

    def test_zone_or_non_canonical_ip_rejected_before_writing(self):
        for bad in ("fe80::1%eth0", "2001:db8:1:0::2"):
            r = self.deploy(SRC=bad)
            self.assertEqual(r.returncode, 2, bad); self.assertIn("canónica", out(r))

    def test_hash_mismatch_fails(self):
        r = self.deploy(FAKE_SSH_TRUNCATE_MATCH="cat > ")
        self.assertEqual(r.returncode, 1); self.assertIn("md5 distintos", out(r))


class Static(unittest.TestCase):
    FILES = ["rtmig_common.sh", "rtmig_send.sh", "rtmig_cutover.sh", "rtmig_deploy.sh"]

    def test_bash_n_and_executable(self):
        for f in self.FILES:
            r = subprocess.run(["bash", "-n", str(KIT / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (f, r.stderr))
        mode = subprocess.run(["git", "ls-files", "-s", "scripts/coldmove"], cwd=ROOT,
                              capture_output=True, text=True).stdout
        for f in ("rtmig_send.sh", "rtmig_cutover.sh", "rtmig_deploy.sh"):
            self.assertRegex(mode, r"100755 \w+ \d\tscripts/coldmove/" + re.escape(f))

    def test_no_customer_data_ips_or_vmids_in_code(self):
        for f in self.FILES:
            s = (KIT / f).read_text().replace("aes128-gcm@openssh.com", "")
            self.assertIsNone(re.search(r"\b2a0[0-9a-f]:[0-9a-f]{1,4}:", s), f)
            self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", s), f)
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", s), f)
            self.assertNotRegex(s, r"(?i)VMID=\$\{VMID:-\d|DST=\$\{DST:-")
            self.assertNotIn("BEGIN OPENSSH", s)

    def test_runners_forward_the_agent_only_to_hetzner_fleet_ips(self):
        files = subprocess.run(["git", "grep", "-l", "nv_agent_opt", "--", "run_remotes"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split()
        self.assertGreaterEqual(len(files), 10)
        leftovers = subprocess.run(["git", "grep", "-n", 'ForwardAgent=yes "root@', "--", ":!tests"], cwd=ROOT,
                                   capture_output=True, text=True).stdout
        self.assertEqual(leftovers, "")
        f = ROOT / "run_remotes/generic.sh"
        fn = re.search(r"(?ms)^nv_agent_opt\(\) \{\n.*?^\}\n", f.read_text()).group()
        for ip, want in (("2a01:4f9:3a:276b::2", "-o ForwardAgent=yes"), ("2a01:4f8:2b03:18a9::2", "-o ForwardAgent=yes"),
                         ("2a0c:4ac1:3:8::2", "-a -o ForwardAgent=no"), ("193.46.81.6", "-a -o ForwardAgent=no")):
            r = subprocess.run(["bash", "-c", fn + f'nv_agent_opt "{ip}"'], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), want, ip)

    def test_rate_default_fits_a_non_hetzner_link(self):
        self.assertIn('RATE="${RATE:-30m}"', (KIT / "rtmig_send.sh").read_text())

    def test_readme_covers_the_flow(self):
        s = (KIT / "README.md").read_text()
        for needle in ("rtmig_send.sh base", "incr auto", "shutdown", "park", "push", "start", "close",
                       "already on dest", "w32tm", "Rollback", "ACK_HWID_CHANGE", "ACK_DISCARD_DEST_WRITES",
                       "INDETERMINADO"):
            self.assertIn(needle, s)


if __name__ == "__main__":
    unittest.main()
