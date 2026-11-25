"""scripts/coldmove (kit rtmig de traslado en frío, 25/11/2026): los scripts se ejecutan de verdad
contra `qm`, `zfs`, `ssh` y `hostname` falsos. «src» y «dst» son dos árboles de estado; el `ssh`
falso ejecuta la orden remota con FAKE_SIDE=dst y las rutas del destino (PVE_ETC, CPUINFO, ...)."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import textwrap
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
if host != "root@" + os.environ["FAKE_DST_IP"]:
    sys.stderr.write("ssh: connect to host %s: No route\n" % host); sys.exit(255)
env = dict(os.environ, FAKE_SIDE="dst", PVE_ETC=os.path.join(root, "dst/etc/pve"),
           CPUINFO=os.path.join(root, "dst/cpuinfo"),
           NV_RAM_GUARD=os.path.join(root, "dst/ram-guard.py"),
           NV_RECONCILER=os.path.join(root, "dst/reconciler.sh"))
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
    if not os.path.exists(conf): sys.stderr.write("Configuration file does not exist\n"); sys.exit(2)
    print("status: " + state())
elif a[0] == "config":
    if not os.path.exists(conf): sys.exit(2)
    sys.stdout.write(open(conf).read())
elif a[0] == "shutdown":
    if os.environ.get("FAKE_SHUTDOWN_FAIL"): sys.exit(255)
    open(stf, "w").write("stopped")
elif a[0] == "start":
    if not os.path.exists(conf): sys.exit(2)
    open(stf, "w").write("running")
elif a[0] == "set":
    key, val = a[2].lstrip("-"), a[3]
    lines = open(conf).read().splitlines(); out = []; done = False; top = True
    for l in lines:
        if l.startswith("["): top = False
        if top and l.startswith(key + ":"): l = "%s: %s" % (key, val); done = True
        out.append(l)
    if not done: out.insert(0, "%s: %s" % (key, val))
    open(conf, "w").write("\n".join(out) + "\n")
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
if a[0] == "list":
    if "-t" in a:                      # snapshots of a dataset, oldest first
        ds = a[-1]
        for s in st.get(ds, {}).get("order", []): print(ds + "@" + s)
    elif "-r" in a:
        for ds in sorted(st): print(ds + "\t1G")
    else:
        ds, sn = split(a[-1])
        ok = ds in st and (sn is None or sn in st[ds]["snaps"])
        sys.exit(0 if ok else 1)
elif a[0] == "snapshot":
    for n in a[1:]:
        ds, sn = split(n)
        if ds not in st: sys.stderr.write("no dataset %s\n" % ds); sys.exit(1)
        st[ds]["snaps"][sn] = str(random.getrandbits(60)); st[ds]["order"].append(sn)
        st[ds]["written"] = 0
    save()
elif a[0] == "set":
    pass
elif a[0] == "get":
    prop = a[a.index("value") + 1]; target = a[-1]
    ds, sn = split(target)
    if prop == "guid":
        try: print(st[ds]["snaps"][sn])
        except KeyError: sys.exit(1)
    elif prop.startswith("written@"):
        if ds not in st: sys.exit(1)
        print(st[ds].get("written", 0))
    elif prop == "receive_resume_token":
        print("-")
    else:
        print("1G"); print("2G")
elif a[0] == "send":
    target = a[-1]; ds, sn = split(target)
    prev = a[a.index("-i") + 1].lstrip("@") if "-i" in a else ""
    if "-nP" in a: print("size\t1000"); sys.exit()
    if sn not in st.get(ds, {}).get("snaps", {}): sys.exit(1)
    sys.stdout.write("STREAM %s %s %s %s\n" % (ds, sn, st[ds]["snaps"][sn], prev or "-"))
elif a[0] == "recv":
    ds = a[-1]; line = sys.stdin.read().split()
    _, sds, sn, guid, prev = line
    if prev == "-":
        if ds in st: sys.stderr.write("exists\n"); sys.exit(1)
        st[ds] = {"snaps": {}, "order": [], "written": 0}
    else:
        if ds not in st or prev not in st[ds]["snaps"]: sys.stderr.write("no base\n"); sys.exit(1)
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
    memory: 61440
    name: E-test
    net0: virtio=BC:24:11:00:00:01,bridge=vmbr0
    ostype: win11
    scsi0: local-zfs:vm-4242-disk-0,discard=on,size=512G
    smbios1: uuid=11111111-2222-3333-4444-555555555555
    tpmstate0: local-zfs:vm-4242-disk-2,size=4M,version=v2.0
    """)


class Kit:
    def __init__(self, tmp, cpu="x86-64-v4", src_cpu=GENOA, dst_cpu=TURIN,
                 src_reconciler=True, dst_reconciler=True):
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
            (d / "cpuinfo").write_text(cpuinfo)
            (d / "ram-guard.py").write_text(FAKE_RAM_GUARD)
            if rec:
                r = d / "reconciler.sh"; r.write_text("#!/bin/sh\n"); r.chmod(0o755)
        (self.t / "src/etc/pve/qemu-server" / f"{VMID}.conf").write_text(CONF.format(cpu=cpu))
        (self.t / "src/etc/pve/firewall" / f"{VMID}.fw").write_text("[OPTIONS]\nenable: 1\n")
        (self.t / f"src/state-{VMID}").write_text("running")
        (self.t / "src/zfs.json").write_text(json.dumps(
            {f"rpool/data/vm-{VMID}-disk-{n}": {"snaps": {}, "order": [], "written": 7}
             for n in (0, 1, 2)}))
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
        })
        for k in ("CHANGE_CPU", "ACK_HWID_CHANGE", "CPU_NOTICE", "RATE", "DISKS", "ZSTD", "FAKE_SIDE"):
            self.env.pop(k, None)

    def run(self, script, *args, **env):
        e = dict(self.env); e.update(env)
        return subprocess.run(["bash", str(KIT / script), *args], capture_output=True, text=True,
                              timeout=60, env=e)

    def read(self, rel):
        p = self.t / rel
        return p.read_text() if p.exists() else ""

    def written_after(self, n):   # the guest writes on source after the last snapshot
        p = self.t / "src/zfs.json"; st = json.loads(p.read_text())
        for d in st.values(): d["written"] = n
        p.write_text(json.dumps(st))

    def to_final(self):
        """precopy, hot incr, shutdown, park, final incr -> returns final snapshot name"""
        steps = [("rtmig_send.sh", "base", "rtmig-20990101T0000Z"),
                 ("rtmig_send.sh", "incr", "auto", "rtmig-20990101T0100Z"),
                 ("rtmig_cutover.sh", "shutdown", "60"),
                 ("rtmig_cutover.sh", "park"),
                 ("rtmig_send.sh", "incr", "auto", "rtmig-final-20990101T0200Z")]
        for s in steps:
            r = self.run(*s, **({"RATE": "0"} if "rtmig-final" in s[-1] else {}))
            assert r.returncode == 0, (s, r.stdout, r.stderr)
        return "rtmig-final-20990101T0200Z"


class HappyPath(unittest.TestCase):
    def test_full_cold_move_v4_genoa_to_turin(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-20990101T0000Z")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout.count("VERIFY OK"), 3)
            self.assertIn("discos: 0 1 2", r.stdout)                  # from the config, not hard-coded
            self.assertIn("rate=30m", r.stdout)                       # non-Hetzner default
            final = None
            r = k.run("rtmig_send.sh", "incr", "auto", "rtmig-20990101T0100Z", DROP_PREV="1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("PREV=auto -> @rtmig-20990101T0000Z", r.stdout)
            self.assertEqual(k.run("rtmig_cutover.sh", "shutdown", "60").returncode, 0)
            r = k.run("rtmig_cutover.sh", "park")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("historial del globo exportado", r.stdout)
            self.assertFalse((k.t / f"src/etc/pve/qemu-server/{VMID}.conf").exists())
            self.assertTrue((k.work / f"{VMID}.conf.src").exists())
            # export-state ran BEFORE the config was parked (it needs the live config)
            log = k.read("ramguard.log").splitlines()
            self.assertEqual(log[0].split()[:4], ["src", "export-state", VMID, "0000999-AX162-TEST"])
            r = k.run("rtmig_send.sh", "incr", "auto", "rtmig-final-20990101T0200Z", RATE="0")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)    # disks found in the parked conf
            final = "rtmig-final-20990101T0200Z"
            r = k.run("rtmig_cutover.sh", "push", final, "47104")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout.count("disco OK"), 3)
            dconf = k.read(f"dst/etc/pve/qemu-server/{VMID}.conf")
            self.assertIn("balloon: 47104", dconf)
            self.assertIn("cpu: x86-64-v4", dconf)                    # generic: unchanged, no ack needed
            self.assertIn("memory: 61440", dconf)                     # RAM untouched
            self.assertTrue(k.read(f"dst/etc/pve/firewall/{VMID}.fw"))
            imp = [l for l in k.read("ramguard.log").splitlines() if "import-state" in l]
            self.assertEqual(len(imp), 1)
            exp_b64 = k.read(f"work/{VMID}.balloon-state").strip()
            self.assertEqual(imp[0].split(), ["dst", "import-state", VMID, "0000266-KDM-9755", exp_b64])
            r = k.run("rtmig_cutover.sh", "start")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(k.read(f"dst/state-{VMID}").strip(), "running")
            self.assertIn("migrate_vm.sh", r.stdout)
            # source zvols kept for the rollback
            self.assertEqual(len(json.loads(k.read("src/zfs.json"))), 3)

    def test_no_reconciler_on_source_means_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, src_reconciler=False, src_cpu=INTEL_NO512, cpu="x86-64-v3")
            final = k.to_final()
            r = k.run("rtmig_cutover.sh", "push", final, "47104")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("import-state", k.read("ramguard.log"))
            self.assertFalse((k.work / f"{VMID}.balloon-state").exists())

    def test_dest_without_reconciler_skips_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp, dst_reconciler=False)
            final = k.to_final()
            r = k.run("rtmig_cutover.sh", "push", final)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("no tiene reconciliador de globo: el historial no se importa", r.stdout)
            self.assertNotIn("import-state", k.read("ramguard.log"))


class Pending(unittest.TestCase):
    def test_pending_estimates_with_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            self.assertEqual(k.run("rtmig_send.sh", "base", "rtmig-20990101T0000Z").returncode, 0)
            k.written_after(31457280 * 60)                       # 60 s at 30m
            r = k.run("rtmig_send.sh", "pending")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("base común: @rtmig-20990101T0000Z", r.stdout)
            self.assertIn("~60s a RATE=30m", r.stdout)


class Safety(unittest.TestCase):
    def test_wrong_destination_name_stops_before_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-x", DST_NODE="0000234-AX162-2-LTD")
            self.assertEqual(r.returncode, 1)
            self.assertIn("dice llamarse '0000266-KDM-9755'", r.stdout)
            self.assertNotIn(" snapshot ", k.read("zfs.log"))

    def test_dst_must_be_an_ip_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_send.sh", "base", "rtmig-x", DST="nodo-kademar")
            self.assertEqual(r.returncode, 2)
            self.assertIn("IP literal", r.stderr)

    def test_snapshot_names_must_start_with_rtmig(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Kit(tmp).run("rtmig_send.sh", "base", "pre-move")
            self.assertEqual(r.returncode, 1)
            self.assertIn("rtmig-", r.stdout)

    def test_shutdown_never_forces(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            r = k.run("rtmig_cutover.sh", "shutdown", "5", FAKE_SHUTDOWN_FAIL="1")
            self.assertEqual(r.returncode, 1)
            self.assertIn("NO forzar", r.stdout)
            self.assertNotIn("stop", [l.split()[1] for l in k.read("qm.log").splitlines()])

    def test_push_refuses_if_written_after_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            final = k.to_final()
            k.written_after(4096)
            r = k.run("rtmig_cutover.sh", "push", final)
            self.assertEqual(r.returncode, 1)
            self.assertIn("la final no es la última", r.stdout)
            self.assertFalse((k.t / f"dst/etc/pve/qemu-server/{VMID}.conf").exists())

    def test_rollback_after_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            final = k.to_final()
            self.assertEqual(k.run("rtmig_cutover.sh", "push", final).returncode, 0)
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((k.t / f"src/etc/pve/qemu-server/{VMID}.conf").exists())
            self.assertFalse((k.t / f"dst/etc/pve/qemu-server/{VMID}.conf").exists())
            self.assertTrue((k.work / f"{VMID}.conf.dst").exists())
            self.assertEqual(k.read(f"src/state-{VMID}").strip(), "running")

    def test_rollback_refuses_while_running_on_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = Kit(tmp)
            final = k.to_final()
            k.run("rtmig_cutover.sh", "push", final); k.run("rtmig_cutover.sh", "start")
            r = k.run("rtmig_cutover.sh", "rollback")
            self.assertEqual(r.returncode, 1)
            self.assertIn("ORDENADAMENTE", r.stdout)


class CpuGuard(unittest.TestCase):
    def push(self, cpu, src_cpu=GENOA, dst_cpu=TURIN, **env):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        k = Kit(tmp.name, cpu=cpu, src_cpu=src_cpu, dst_cpu=dst_cpu)
        final = k.to_final()
        return k, k.run("rtmig_cutover.sh", "push", final, **env)

    def assertNothingWritten(self, k):
        self.assertFalse((k.t / f"dst/etc/pve/qemu-server/{VMID}.conf").exists())

    def test_host_to_other_silicon_needs_an_explicit_ack(self):
        k, r = self.push("host")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Hardware ID", r.stdout)
        self.assertIn("ACK_HWID_CHANGE=4242", r.stdout)
        self.assertNothingWritten(k)

    def test_ack_needs_the_notice_and_the_right_vmid(self):
        k, r = self.push("host", ACK_HWID_CHANGE=VMID)
        self.assertEqual(r.returncode, 1); self.assertIn("CPU_NOTICE", r.stdout); self.assertNothingWritten(k)
        k, r = self.push("host", ACK_HWID_CHANGE="1", CPU_NOTICE="correo 20/11")
        self.assertEqual(r.returncode, 1); self.assertIn("no es esta VM", r.stdout); self.assertNothingWritten(k)

    def test_ack_keeps_host_and_records_it(self):
        k, r = self.push("host", ACK_HWID_CHANGE=VMID, CPU_NOTICE="correo 20/11")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cpu: host", k.read(f"dst/etc/pve/qemu-server/{VMID}.conf"))
        rec = k.read("work/cpu-change.txt")
        self.assertIn("VM4242 cpu host sin cambiar", rec)
        self.assertIn("AuthenticAMD|25|17 -> AuthenticAMD|26|2", rec)
        self.assertIn("correo 20/11", rec)

    def test_change_cpu_sets_v4_and_keeps_source_config(self):
        k, r = self.push("host", CHANGE_CPU="1", CPU_NOTICE="correo 20/11")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cpu: x86-64-v4", k.read(f"dst/etc/pve/qemu-server/{VMID}.conf"))
        self.assertIn("cpu: host", k.read(f"work/{VMID}.conf.src"))
        self.assertIn("host -> x86-64-v4", k.read("work/cpu-change.txt"))

    def test_both_switches_are_exclusive(self):
        k, r = self.push("host", CHANGE_CPU="1", ACK_HWID_CHANGE=VMID, CPU_NOTICE="x")
        self.assertEqual(r.returncode, 1); self.assertIn("excluyentes", r.stdout)

    def test_host_on_identical_silicon_needs_nothing(self):
        k, r = self.push("host", dst_cpu=GENOA)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("cpu-change", " ".join(os.listdir(k.work)))

    def test_named_or_missing_model_treated_like_host(self):
        for cpu in ("EPYC-Genoa", "kvm64"):
            k, r = self.push(cpu)
            self.assertEqual(r.returncode, 1, cpu); self.assertNothingWritten(k)

    def test_v4_to_a_host_without_avx512(self):
        k, r = self.push("x86-64-v4", dst_cpu=INTEL_NO512)
        self.assertEqual(r.returncode, 1); self.assertIn("AVX-512", r.stdout); self.assertNothingWritten(k)
        k, r = self.push("x86-64-v4", dst_cpu=INTEL_NO512, CHANGE_CPU="1", CPU_NOTICE="x")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cpu: x86-64-v3", k.read(f"dst/etc/pve/qemu-server/{VMID}.conf"))


class Static(unittest.TestCase):
    FILES = ["rtmig_common.sh", "rtmig_send.sh", "rtmig_cutover.sh", "rtmig_deploy.sh"]

    def test_bash_n(self):
        for f in self.FILES:
            r = subprocess.run(["bash", "-n", str(KIT / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (f, r.stderr))

    def test_no_customer_data_ips_or_vmids_in_code(self):
        for f in self.FILES:
            s = (KIT / f).read_text().replace("aes128-gcm@openssh.com", "")   # SSH cipher name
            self.assertIsNone(re.search(r"\b2a0[0-9a-f]:[0-9a-f]{1,4}:", s), f)       # fleet/Kademar IPv6
            self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", s), f)
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", s), f)            # e-mails
            self.assertNotRegex(s, r"(?i)VMID=\$\{VMID:-\d|DST=\$\{DST:-")              # no baked defaults
            self.assertNotIn("BEGIN OPENSSH", s)

    def test_rate_default_fits_a_non_hetzner_link(self):
        self.assertIn('RATE="${RATE:-30m}"', (KIT / "rtmig_send.sh").read_text())

    def test_readme_covers_the_flow(self):
        s = (KIT / "README.md").read_text()
        for needle in ("rtmig_send.sh base", "incr auto", "shutdown", "park", "push", "start",
                       "already on dest", "w32tm", "Rollback", "ACK_HWID_CHANGE"):
            self.assertIn(needle, s)


if __name__ == "__main__":
    unittest.main()
