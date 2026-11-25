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
with open(os.path.join(root, "ssh.log"), "a") as f: f.write(host + " :: " + cmd + "\n")
fail = os.environ.get("FAKE_SSH_FAIL_MATCH")
dead = os.path.join(root, "ssh-dead")          # FAKE_SSH_DIE_AFTER: the link dies after that command
if os.path.exists(dead):
    sys.stderr.write("Connection timed out\n"); sys.exit(255)
if os.environ.get("FAKE_SSH_DIE_AFTER") and os.environ["FAKE_SSH_DIE_AFTER"] in cmd:
    open(dead, "w").close()
if fail and fail in cmd:
    sys.stderr.write("Connection closed by remote host\n"); sys.exit(255)
se_m, se_c = os.environ.get("FAKE_SSH_SIDE_EFFECT_MATCH"), os.environ.get("FAKE_SSH_SIDE_EFFECT")
if se_m and se_c and se_m in cmd and not os.path.exists(os.path.join(root, "side-effect-done")):
    open(os.path.join(root, "side-effect-done"), "w").close()
    subprocess.run(["bash", "-c", se_c])                  # something else edits a file meanwhile
if host == os.environ.get("FAKE_JUMP", "-"):
    env = dict(os.environ)
elif host == "root@" + os.environ.get("FAKE_SRC_IP", "-"):
    env = dict(os.environ, FAKE_SIDE="src")
elif host == "root@" + os.environ["FAKE_DST_IP"]:
    env = dict(os.environ, FAKE_SIDE="dst", PVE_ETC=os.path.join(root, "dst/etc/pve"),
               CPUINFO=os.path.join(root, "dst/cpuinfo"),
               NV_RAM_GUARD=os.path.join(root, "dst/ram-guard.py"),
               NV_RECONCILER=os.path.join(root, "dst/reconciler.sh"))
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
import os, re, sys
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
    if os.environ.get("FAKE_QM_ADD_PENDING_ON_STOP"):      # someone runs `qm set` while it stops
        open(conf, "a").write("\n[PENDING]\n" + os.environ["FAKE_QM_ADD_PENDING_ON_STOP"] + "\n")
    open(stf, "w").write("stopped")
    # like PVE (QemuServer vm_stop_cleanup): the pending changes are applied when the VM stops
    lines = open(conf).read().splitlines(); main, pend, rest, sec = [], {}, [], "main"
    for l in lines:
        if l.startswith("["): sec = l.strip()
        if sec == "main": main.append(l)
        elif sec == "[PENDING]":
            if not l.startswith("[") and ":" in l: pend[l.split(":", 1)[0]] = l
        else: rest.append(l)
    if pend:
        dels = [x for x in re.split(r"[ ,]+", pend.pop("delete", "delete:").split(":", 1)[1]) if x]
        main = [l for l in main if l.split(":", 1)[0] not in pend and l.split(":", 1)[0] not in dels]
        main = [l for l in main if l.strip()] + list(pend.values())
        open(conf, "w").write("\n".join(main + ([""] + rest if rest else [])) + "\n")
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
def new_ds(): return {"snaps": {}, "order": [], "written": 0, "token": "-"}
if a[0] == "list":
    if "-t" in a:
        ds = a[-1]
        for s in st.get(ds, {}).get("order", []): print(ds + "@" + s)
    elif "-r" in a:
        for ds in sorted(st): print(ds + "\t1G")
    else:
        ds, sn = split(a[-1])
        sys.exit(0 if ds in st and (sn is None or sn in st[ds]["snaps"]) else 1)
elif a[0] == "snapshot":
    for n in a[1:]:
        ds, sn = split(n)
        if ds not in st or sn in st[ds]["snaps"]: sys.stderr.write("cannot snapshot %s\n" % n); sys.exit(1)
        st[ds]["snaps"][sn] = str(random.getrandbits(60)); st[ds]["order"].append(sn)
        st[ds]["written"] = 0
    save()
elif a[0] == "set":
    pass
elif a[0] == "get":
    prop = a[a.index("value") + 1]; ds, sn = split(a[-1])
    fail = os.environ.get("FAKE_ZFS_FAIL_MATCH")
    if fail and side == os.environ.get("FAKE_ZFS_FAIL_SIDE", "dst") and fail in " ".join(a):
        sys.stderr.write("internal error: Input/output error\n"); sys.exit(1)
    if ds not in st or (sn and sn not in st[ds]["snaps"]):
        sys.stderr.write("cannot open '%s': dataset does not exist\n" % a[-1]); sys.exit(1)
    if prop == "guid":
        if not sn: sys.exit(1)
        print(st[ds]["snaps"][sn])
    elif prop.startswith("written@"):
        print(st[ds].get("written", 0))
    elif prop == "receive_resume_token":
        print(st[ds].get("token", "-"))
    else:
        print("1G"); print("2G")
elif a[0] == "send":
    if "-t" in a:                       # resume from token: "1-" + hex("ds~snap~guid~prev")
        sds, sn, guid, prev = bytes.fromhex(a[a.index("-t") + 1][2:]).decode().split("~")
        sys.stdout.write("STREAM %s %s %s %s\n" % (sds, sn, guid, prev)); sys.exit()
    ds, sn = split(a[-1])
    prev = a[a.index("-i") + 1].lstrip("@") if "-i" in a else ""
    if "-nP" in a: print("size\t1000"); sys.exit()
    if sn not in st.get(ds, {}).get("snaps", {}): sys.exit(1)
    sys.stdout.write("STREAM %s %s %s %s\n" % (ds, sn, st[ds]["snaps"][sn], prev or "-"))
elif a[0] == "recv":
    ds = a[-1]; _, sds, sn, guid, prev = sys.stdin.read().split()
    cut = os.path.join(root, "recv-cut")
    if os.path.exists(cut) and open(cut).read().strip() == ds:   # cut half way: leave a token
        os.remove(cut)
        if ds not in st: st[ds] = new_ds()
        st[ds]["token"] = "1-" + "~".join([sds, sn, guid, prev]).encode().hex(); save()
        sys.stderr.write("connection reset\n"); sys.exit(1)
    if st.get(ds, {}).get("token", "-") != "-":
        if "-s" not in a: sys.exit(1)
        st[ds]["token"] = "-"
    elif prev == "-":
        if ds in st: sys.stderr.write("exists\n"); sys.exit(1)
        st[ds] = new_ds()
    elif ds not in st or prev not in st[ds]["snaps"]:
        sys.stderr.write("no base\n"); sys.exit(1)
    st[ds]["snaps"][sn] = guid; st[ds]["order"].append(sn); st[ds]["written"] = 0
    save()
elif a[0] == "destroy":
    ds, sn = split(a[-1]); del st[ds]["snaps"][sn]; st[ds]["order"].remove(sn); save()
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


class Kit:
    def __init__(self, tmp, cpu="x86-64-v4", src_cpu=GENOA, dst_cpu=TURIN,
                 src_reconciler=True, dst_reconciler=True, conf=None, run_cpu=None):
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
        (self.t / "src/zfs.json").write_text(json.dumps(
            {f"rpool/data/vm-{VMID}-disk-{n}": {"snaps": {}, "order": [], "written": 7, "token": "-"}
             for n in (0, 1, 2)}))
        # what QEMU runs with on the source (PVE runs x86-64-v* as qemu64,+flags)
        if run_cpu is None:
            run_cpu = "qemu64,+avx512f" if cpu.startswith("x86-64-v") else cpu + ",+kvm_pv_eoi"
        (self.t / "run").mkdir(); (self.t / "run" / f"{VMID}.pid").write_text("31337\n")
        (self.t / "proc/31337").mkdir(parents=True)
        (self.t / "proc/31337/cmdline").write_bytes(
            b"\0".join([b"/usr/bin/kvm", b"-id", VMID.encode(), b"-cpu", run_cpu.encode(), b"-m", b"61440"]) + b"\0")
        self.work = self.t / "work"
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{b}:{os.environ['PATH']}", "FAKE_ROOT": str(self.t),
            "FAKE_DST_IP": "2001:db8:2::2", "FAKE_HOST_SRC": "0000999-AX162-TEST",
            "FAKE_HOST_DST": "0000266-KDM-9755",
            "VMID": VMID, "DST": "2001:db8:2::2", "DST_NODE": "0000266-KDM-9755",
            "WORK": str(self.work), "PVE_ETC": str(self.t / "src/etc/pve"),
            "CPUINFO": str(self.t / "src/cpuinfo"),
            "NV_RAM_GUARD": str(self.t / "src/ram-guard.py"),
            "NV_RECONCILER": str(self.t / "src/reconciler.sh"),
            "QEMU_PIDDIR": str(self.t / "run"), "PROC_DIR": str(self.t / "proc"),
        })
        for k in ("CHANGE_CPU", "ACK_HWID_CHANGE", "CPU_NOTICE", "RATE", "DISKS", "ZSTD", "FAKE_SIDE",
                  "ACK_DISCARD_DEST_WRITES", "FAKE_SSH_FAIL_MATCH", "FAKE_SSH_TRUNCATE_MATCH",
                  "FAKE_SSH_LOSE_MATCH", "FAKE_SSH_DIE_AFTER", "FAKE_ZFS_FAIL_MATCH", "FAKE_ZFS_FAIL_SIDE"):
            self.env.pop(k, None)

    def run(self, script, *args, **env):
        e = dict(self.env); e.update(env)
        return subprocess.run(["bash", str(KIT / script), *args], capture_output=True, text=True,
                              timeout=60, env=e)

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


class HappyPath(unittest.TestCase):
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
            self.assertIn("historial del globo exportado", r.stdout)
            self.assertFalse(k.src_conf.exists())
            st = k.state()
            self.assertEqual((st["PHASE"], st["UUID"], st["SRC_NODE"], st["DST_NODE"], st["DISKS"]),
                             ("parked", "11111111-2222-3333-4444-555555555555", "0000999-AX162-TEST",
                              "0000266-KDM-9755", "0 1 2"))
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
            self.assertEqual(imp[0].split(), ["dst", "import-state", VMID, "0000266-KDM-9755",
                                              k.read(f"work/{VMID}.balloon-state").strip()])
            k.ok("rtmig_cutover.sh", "start")
            self.assertEqual(k.read(f"dst/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "started")
            k.ok("rtmig_cutover.sh", "close")
            self.assertEqual(k.state()["PHASE"], "done")
            self.assertFalse((k.work / f"{VMID}.conf.src").exists())
            self.assertEqual(len(json.loads(k.read("src/zfs.json"))), 3)   # source zvols kept

    def test_no_reconciler_on_source_means_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, src_reconciler=False, src_cpu=INTEL_NO512, cpu="x86-64-v3")
            k.to_final()
            k.ok("rtmig_cutover.sh", "push", FINAL, "47104")
            self.assertNotIn("import-state", k.read("ramguard.log"))
            self.assertFalse((k.work / f"{VMID}.balloon-state").exists())

    def test_dest_without_reconciler_skips_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, dst_reconciler=False)
            k.to_final()
            r = k.ok("rtmig_cutover.sh", "push", FINAL)
            self.assertIn("no tiene reconciliador de globo: el historial no se importa", r.stdout)
            self.assertNotIn("import-state", k.read("ramguard.log"))


class ResumableSend(unittest.TestCase):
    def test_base_cut_half_way_resumes_with_the_same_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            (k.t / "recv-cut").write_text(f"rpool/data/vm-{VMID}-disk-1")
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
            (k.t / "recv-cut").write_text(f"rpool/data/vm-{VMID}-disk-2")
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


class Inventory(unittest.TestCase):
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


class Phases(unittest.TestCase):
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
            k.src_conf.write_text((k.work / f"{VMID}.conf.src").read_text())
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("dos configs arrancables", out(r))
            self.assertFalse(k.dst_conf.exists())

    def test_state_bound_to_destination_and_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, DST="2001:db8:2::3", FAKE_DST_IP="2001:db8:2::3")
            self.assertEqual(r.returncode, 1); self.assertIn("no hacia", out(r))
            p = k.work / f"{VMID}.conf.src"
            p.write_text(p.read_text().replace("uuid=1111", "uuid=9999"))
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertIn("no es la de este traslado", out(r))

    def test_fw_write_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final()
            r = k.run("rtmig_cutover.sh", "push", FINAL, FAKE_SSH_FAIL_MATCH="firewall/4242.fw.rtmig-tmp")
            self.assertEqual(r.returncode, 1); self.assertIn("la config NO se ha publicado", out(r))
            self.assertFalse(k.dst_conf.exists()); self.assertEqual(k.state()["PHASE"], "publishing")
            k.ok("rtmig_cutover.sh", "push", FINAL)                     # retried from «publishing»
            self.assertEqual(k.state()["PHASE"], "pushed")

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


class Safety(unittest.TestCase):
    def test_wrong_destination_name_stops_before_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-x", DST_NODE="0000234-AX162-2-LTD")
            self.assertEqual(r.returncode, 1); self.assertIn("dice llamarse '0000266-KDM-9755'", out(r))
            self.assertNotIn(" snapshot ", k.read("zfs.log"))

    def test_parameters_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            for env, needle in (({"DST": "nodo-kademar"}, "IP literal"), ({"WORK": "/root/a b"}, "WORK no válido"),
                                ({"WORK": "/root/../etc"}, "WORK no válido"), ({"POOLDS": "rpool/data;rm"}, "POOLDS no válido"),
                                ({"VMID": "42x"}, "VMID no válida"), ({"DST_NODE": "a b"}, "DST_NODE no válido")):
                r = k.run("rtmig_send.sh", "base", "rtmig-x", **env)
                self.assertEqual(r.returncode, 2, env); self.assertIn(needle, out(r))
            r = k.run("rtmig_send.sh", "base", "rtmig-x';rm -rf /;'")
            self.assertEqual(r.returncode, 1); self.assertIn("debe empezar por rtmig-", out(r))
            r = k.run("rtmig_cutover.sh", "push", "x';id;'")
            self.assertEqual(r.returncode, 1); self.assertIn("debe empezar por rtmig-", out(r))

    def test_shutdown_never_forces(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
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
            k = Kit(tmp); k.work.mkdir()
            (k.work / f"{VMID}.fw.src").write_text("[OPTIONS]\nenable: 0\n")   # left over from an old move
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            k.ok("rtmig_cutover.sh", "shutdown", "60")
            r = k.run("rtmig_cutover.sh", "park")
            self.assertEqual(r.returncode, 1); self.assertIn("queda una copia anterior", out(r))
            self.assertTrue(k.src_conf.exists())


class Rollback(unittest.TestCase):
    def test_rollback_after_push_without_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertTrue(k.src_conf.exists()); self.assertFalse(k.dst_conf.exists())
            self.assertFalse((k.t / f"dst/etc/pve/firewall/{VMID}.fw").exists())
            self.assertTrue(list(k.work.glob(f"{VMID}.conf.dst-*")))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")
            self.assertFalse((k.work / f"{VMID}.conf.src").exists())          # archived, never reused
            self.assertTrue(list(k.work.glob(f"{VMID}.conf.src.rolledback-*")))

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

    def test_written_query_failing_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_SSH_FAIL_MATCH="written@")
            self.assertEqual(r.returncode, 1); self.assertIn("INDETERMINADO", out(r))
            self.assertFalse(k.src_conf.exists())

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
            k.ok("rtmig_cutover.sh", "shutdown", "60")
            k.ok("rtmig_cutover.sh", "park")
            self.assertEqual(k.state()["PHASE"], "parked")


class SecondReview(unittest.TestCase):
    """2nd review of PR #291."""

    def test_zfs_error_on_dest_is_indeterminate_not_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            r = k.run("rtmig_cutover.sh", "rollback", FAKE_ZFS_FAIL_MATCH="written@")
            self.assertEqual(r.returncode, 1); self.assertIn("zfs falló en el destino", out(r))
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
            fw = k.read(f"src/etc/pve/firewall/{VMID}.fw") or k.read(f"work/{VMID}.fw.src")
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

    def test_pending_changes_are_refused_before_shutdown(self):
        # PVE applies [PENDING] when the VM stops (QemuServer vm_stop_cleanup / _do_vm_stop): the config
        # validated before the shutdown would not be the one parked and published. Refused up front.
        for pend in ("cores: 4", "cpu: host", "delete: cpu", "balloon: 2048", "memory: 4096\ndelete: balloon"):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[PENDING]\n" + pend + "\n")
                k.ok("rtmig_send.sh", "base", "rtmig-a")
                r = k.run("rtmig_cutover.sh", "shutdown", "60", ACK_HWID_CHANGE=VMID, CPU_NOTICE="x")
                self.assertEqual(r.returncode, 1, pend); self.assertIn("cambios pendientes ([PENDING])", out(r))
                self.assertIn("--revert", out(r))
                self.assertNotIn("shutdown", k.read("qm.log"))
                self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    def test_pending_header_is_matched_like_pve(self):
        # QemuServer: /^\[PENDING\]\s*$/i
        for hdr in ("[pending]", "[Pending]  ", "[PENDING]\t", "[PeNdInG]"):
            with tempfile.TemporaryDirectory() as tmp:
                k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n" + hdr + "\ncores: 4\n")
                k.ok("rtmig_send.sh", "base", "rtmig-a")
                r = k.run("rtmig_cutover.sh", "shutdown", "60")
                self.assertEqual(r.returncode, 1, repr(hdr)); self.assertIn("cambios pendientes", out(r))
                self.assertNotIn("shutdown", k.read("qm.log"))
        with tempfile.TemporaryDirectory() as tmp:                    # a snapshot is not a pending change
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[pending-snap]\ncores: 4\n")
            k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60")

    def test_a_failed_read_never_means_clean(self):
        # only the pending check's awk fails (rc 2, transient): the VM must NOT be stopped
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[PENDING]\ncores: 4\n")
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            shim = k.t / "shim"; shim.mkdir()
            real = subprocess.run(["bash", "-c", "command -v awk"], capture_output=True, text=True).stdout.strip()
            (shim / "awk").write_text('#!/bin/bash\ncase "$*" in *"[pending]"*) echo "awk: read error" >&2; exit 2;; esac\nexec %s "$@"\n' % real)
            (shim / "awk").chmod(0o755)
            r = k.run("rtmig_cutover.sh", "shutdown", "60", PATH=f"{shim}:{k.env['PATH']}")
            self.assertEqual(r.returncode, 1); self.assertIn("no pude leer la config", out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertIn("[PENDING]", k.src_conf.read_text())             # nothing applied

    def test_an_edit_during_the_remote_checks_aborts_before_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60", FAKE_SSH_SIDE_EFFECT_MATCH="PROBE-END",
                      FAKE_SSH_SIDE_EFFECT=f"printf '\\n[PENDING]\\ncores: 4\\n' >> {k.src_conf}")
            self.assertEqual(r.returncode, 1)
            self.assertTrue("cambios pendientes" in out(r) or "cambió mientras" in out(r), out(r))
            self.assertNotIn("shutdown", k.read("qm.log"))
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    def test_a_pending_change_applied_during_the_stop_is_detected_and_park_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a")
            r = k.run("rtmig_cutover.sh", "shutdown", "60", FAKE_QM_ADD_PENDING_ON_STOP="cores: 2")
            self.assertEqual(r.returncode, 1); self.assertIn("cambió durante el apagado", out(r))
            self.assertIn("cores: 2", k.src_conf.read_text())             # PVE applied it on stop
            r = k.run("rtmig_cutover.sh", "park")
            self.assertEqual(r.returncode, 1); self.assertIn("no es la que comprobó shutdown", out(r))
            self.assertTrue(k.src_conf.exists())

    def test_an_edit_during_parks_remote_checks_keeps_the_config_on_the_origin(self):
        # cores 8→4 while park probes the destination: nothing is parked, the origin keeps its config
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.ok("rtmig_send.sh", "base", "rtmig-a"); k.ok("rtmig_cutover.sh", "shutdown", "60")
            r = k.run("rtmig_cutover.sh", "park", FAKE_SSH_SIDE_EFFECT_MATCH="PROBE-END",
                      FAKE_SSH_SIDE_EFFECT=f"sed -i 's/^cores: 8$/cores: 4/' {k.src_conf}")
            self.assertEqual(r.returncode, 1); self.assertIn("cambió desde que la comprobó shutdown", out(r))
            self.assertTrue(k.src_conf.exists()); self.assertIn("cores: 4", k.src_conf.read_text())
            self.assertFalse((k.work / f"{VMID}.conf.src").exists())
            self.assertFalse((k.work / f"{VMID}.conf.staging").exists())
            self.assertEqual(k.read(f"work/{VMID}.state"), "")              # no «parked» state
            r = k.run("rtmig_cutover.sh", "push", FINAL)
            self.assertEqual(r.returncode, 1); self.assertFalse(k.dst_conf.exists())
            # the source has a balloon reconciler: the discarded park left no balloon-state behind,
            # so the retry (start → shutdown → park) goes through
            self.assertFalse((k.work / f"{VMID}.balloon-state").exists())
            self.assertFalse((k.work / f"{VMID}.balloon-state.staging").exists())
            subprocess.run(["qm", "start", VMID], env=k.env, check=True)
            k.ok("rtmig_cutover.sh", "shutdown", "60")
            r = k.ok("rtmig_cutover.sh", "park")
            self.assertIn("historial del globo exportado", r.stdout)
            self.assertTrue((k.work / f"{VMID}.balloon-state").exists())
            self.assertIn("cores: 4", k.read(f"work/{VMID}.conf.src"))
            self.assertEqual(k.state()["PHASE"], "parked"); self.assertFalse(k.src_conf.exists())

    def test_after_reverting_the_pending_change_the_move_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[PENDING]\ncores: 4\n")
            k.ok("rtmig_send.sh", "base", "rtmig-a")
            self.assertEqual(k.run("rtmig_cutover.sh", "shutdown", "60").returncode, 1)
            k.src_conf.write_text(CONF.format(cpu="x86-64-v4"))           # qm set --revert cores
            k.ok("rtmig_cutover.sh", "shutdown", "60"); k.ok("rtmig_cutover.sh", "park")
            self.assertIn("cores: 8", k.read(f"work/{VMID}.conf.src"))
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    def test_the_fake_applies_pending_on_stop_like_pve(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, conf=CONF.format(cpu="x86-64-v4") + "\n[PENDING]\ncores: 4\ndelete: tablet\n")
            subprocess.run(["qm", "shutdown", VMID], env=k.env, check=True)
            c = k.src_conf.read_text()
            self.assertIn("cores: 4", c); self.assertNotIn("[PENDING]", c); self.assertNotIn("cores: 8", c)

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
                r = k.run("rtmig_send.sh", "base", "rtmig-a", DST=bad)
                self.assertEqual(r.returncode, 2, bad); self.assertIn("canónica", out(r))


class ThirdReview(unittest.TestCase):
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
            self.assertEqual(fw.read_text(), k.read(f"work/{VMID}.fw.src") or "[OPTIONS]\nenable: 1\n")
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")
            self.assertEqual(k.state()["PHASE"], "rolledback")

    def test_rollback_keeps_a_copy_of_the_dest_fw(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp); k.to_final(); k.ok("rtmig_cutover.sh", "push", FINAL)
            k.ok("rtmig_cutover.sh", "rollback")
            self.assertTrue(list(k.work.glob(f"{VMID}.fw.dst-*")))
            self.assertTrue(list(k.work.glob(f"{VMID}.conf.dst-*")))

    def test_running_host_with_a_different_configured_model_needs_the_ack(self):
        # the config already says x86-64-v4 (edited without [PENDING]) but QEMU still runs `host`:
        # the next boot changes the Hardware ID even on the same silicon
        conf = CONF.format(cpu="x86-64-v4")
        with tempfile.TemporaryDirectory() as tmp:
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
            k = Kit(tmp); (k.t / "run" / f"{VMID}.pid").unlink()
            r = k.run("rtmig_cutover.sh", "shutdown", "60")
            self.assertEqual(r.returncode, 1); self.assertIn("no pude leer con qué CPU corre", out(r))

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
            self.assertTrue((k.work / f"{VMID}.conf.src").exists())
            k.ok("rtmig_cutover.sh", "status")
            self.assertFalse((k.work / f"{VMID}.conf.src").exists())
            self.assertEqual(k.state()["ARCHIVE_PENDING"], "")
            k.ok("rtmig_cutover.sh", "shutdown", "60"); k.ok("rtmig_cutover.sh", "park")


class CpuGuard(unittest.TestCase):
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
        self.assertIn("cpu: host", k.read(f"work/{VMID}.conf.src"))
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


class Deploy(unittest.TestCase):
    def deploy(self, **env):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        k = Kit(tmp.name)
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
