"""Node role, plan allow-list and CPU in the defrag (2026-11-04).

Operator rules (Iván, 04/11):
  * proxmox_nodes.nodeType (set by hand) is the ONLY source of truth for SQX
    vs MT — the defrag used to classify by NAME (-AX162 = SQX, -AX102 = MT5),
    which would have treated an AX102 assigned to SQX as an MT node.
  * never mix: MT VMs only on MT nodes, SQX VMs only on SQX nodes.
  * proxmox_nodes.allowedServerTypes (hand-set) narrows the plans a node takes
    (the SQX AX102s: vps-a/vps-b only), also as a defrag destination.
  * a `cpu: host` VM never moves to a node with another CPU; generic
    x86-64-v* VMs may move within the vendor. Anything unknown -> no move.

Earlier (10/10/2026) the vendor came from the product in the node id; it now
comes from the node's /proc/cpuinfo, probed every run."""
import importlib.util
from pathlib import Path
import re
import unittest

PATH = Path(__file__).with_name('neuravps-defrag.py')
spec = importlib.util.spec_from_file_location('defrag_cpu_role', PATH)
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)

EPYC_P = {"vendor": "AuthenticAMD", "family": "25", "model": "17", "stepping": "1",
          "name": "AMD EPYC 9454P 48-Core Processor"}
EPYC = dict(EPYC_P, name="AMD EPYC 9454 48-Core Processor")
RYZEN = {"vendor": "AuthenticAMD", "family": "25", "model": "97", "stepping": "2",
         "name": "AMD Ryzen 9 7950X3D 16-Core Processor"}
INTEL = {"vendor": "GenuineIntel", "family": "6", "model": "183", "stepping": "1",
         "name": "13th Gen Intel(R) Core(TM) i5-13500"}


class NodeRole(unittest.TestCase):
    def test_role_comes_from_node_type_not_name(self):
        self.assertEqual(df.node_model({"nodeType": "SQX"}), "SQX")
        self.assertEqual(df.node_model({"nodeType": " MT5 "}), "MT5")
        for other in ({"nodeType": "VPS-E"}, {"nodeType": "Otro"}, {}, None):
            self.assertIsNone(df.node_model(other))

    def test_main_no_longer_reads_the_name(self):
        src = PATH.read_text()
        main = src[src.index('def main():'):]
        self.assertNotIn('"-AX162" in nd.id', main)
        self.assertNotIn('"-AX102" in nd.id', main)
        self.assertIn('model = node_model(d)', main)


class AllowList(unittest.TestCase):
    def test_parsing(self):
        self.assertIsNone(df.node_allowed({}))
        self.assertEqual(df.node_allowed({"allowedServerTypes": ["vps-a", "vps-b"]}),
                         frozenset({"vps-a", "vps-b"}))
        self.assertEqual(df.node_allowed({"allowedServerTypes": "vps-a,vps-b"}), frozenset())

    def test_dest_allows(self):
        ab = {"allowed": frozenset({"vps-a", "vps-b"})}
        self.assertTrue(df.dest_allows(ab, {"type": "vps-b"}))
        self.assertFalse(df.dest_allows(ab, {"type": "vps-c"}))
        self.assertFalse(df.dest_allows(ab, {"type": ""}))
        self.assertTrue(df.dest_allows({"allowed": None}, {"type": "vps-e"}))

    def test_plan_sizes_follow_the_list(self):
        sizes, plans = df.plan_sizes(frozenset({"vps-a", "vps-b"}))
        self.assertEqual(sizes, [16, 19])
        self.assertEqual([p[0] for p in plans], ["vps-a", "vps-b"])
        self.assertEqual(df.plan_sizes(None), (df.PLAN_SIZES, df.PLANS))

    def test_a_vps_c_shaped_hole_is_a_crumb_on_an_ab_node(self):
        _, ab = df.plan_sizes(frozenset({"vps-a", "vps-b"}))
        # 14 GB commit / 30 GB floor: nothing fits on either kind of node
        self.assertFalse(df.fits_any_plan(14, 30, ab))
        # a hole of 31 GB/16 GB fits a vps-a/b on both kinds...
        self.assertTrue(df.fits_any_plan(31, 16, ab))
        # leftover_clean uses the node's own plan sizes
        self.assertTrue(df.leftover_clean(35, [16, 19]))   # 16+19
        self.assertFalse(df.leftover_clean(28, [16, 19]))  # 16->12 left, 19->9 left
        self.assertTrue(df.leftover_clean(31, None))       # a vps-c on an AX162


class CpuRule(unittest.TestCase):
    def test_generic_models(self):
        for cpu in ("x86-64-v4", "x86-64-v3", "x86-64-v2-AES", "x86-64-v4,flags=+aes"):
            self.assertTrue(df.vm_cpu_generic(cpu), cpu)
        for cpu in ("host", "kvm64", "EPYC-Genoa", "max", "", None):
            self.assertFalse(df.vm_cpu_generic(cpu), cpu)

    def test_generic_vm_moves_within_the_vendor(self):
        self.assertTrue(df.cpu_move_ok("x86-64-v4", EPYC, RYZEN))
        self.assertTrue(df.cpu_move_ok("x86-64-v4", RYZEN, EPYC_P))
        self.assertFalse(df.cpu_move_ok("x86-64-v4", EPYC, INTEL))   # never live AMD<->Intel
        self.assertFalse(df.cpu_move_ok("x86-64-v3", INTEL, EPYC))

    def test_host_vm_needs_the_same_silicon(self):
        self.assertFalse(df.cpu_move_ok("host", EPYC, RYZEN))    # AX162 -> AX102
        self.assertFalse(df.cpu_move_ok("host", RYZEN, EPYC))    # AX102 -> AX162
        self.assertTrue(df.cpu_move_ok("host", RYZEN, dict(RYZEN)))
        # 9454 and 9454P are the same Genoa die: unchanged from before
        self.assertTrue(df.cpu_move_ok("host", EPYC, EPYC_P))
        self.assertFalse(df.cpu_move_ok("kvm64", EPYC, RYZEN))

    def test_unknown_never_moves(self):
        self.assertFalse(df.cpu_move_ok(None, EPYC, EPYC))       # config unreadable
        self.assertFalse(df.cpu_move_ok("x86-64-v4", None, EPYC))
        self.assertFalse(df.cpu_move_ok("x86-64-v4", EPYC, None))
        self.assertFalse(df.cpu_move_ok("host", dict(EPYC, stepping=""), EPYC))

    def test_probe_parsing(self):
        out = ("SIG|AuthenticAMD|25|97|2|AMD Ryzen 9 7950X3D 16-Core Processor\n"
               "VM|1751|x86-64-v3\nVM|936|host\nVM|12|kvm64\nVM|bad|host\n")
        sig, cpus = df.parse_node_probe(out)
        self.assertEqual(sig, RYZEN)
        self.assertEqual(cpus, {1751: "x86-64-v3", 936: "host", 12: "kvm64"})
        self.assertEqual(df.parse_node_probe("SIG|||||\n"), (None, {}))
        self.assertEqual(df.parse_node_probe(""), (None, {}))

    def test_probe_runs_where_it_is_sent(self):
        # The probe is plain python3 shipped with shlex: execute it here too.
        import subprocess
        out = subprocess.run(["bash", "-c", df.node_probe_cmd()],
                             capture_output=True, text=True, timeout=20).stdout
        sig, _ = df.parse_node_probe(out)
        self.assertIsNotNone(sig, out)


class PickDestWiring(unittest.TestCase):
    """main() is one big Firestore-bound function; these pin its wiring."""

    def setUp(self):
        src = PATH.read_text()
        self.body = src[src.index('    def pick_dest('):src.index('    _rates_cache = {}')]
        self.main = src[src.index('def main():'):]

    def test_pick_dest_applies_every_rule(self):
        for rule in (r'if not vm or vm\.get\("node"\) not in nodes:\n\s+return None',
                     r'if dn\["model"\] != src\["model"\]:\n\s+continue',
                     r'if not dest_allows\(dn, vm\):\n\s+continue',
                     r'if not cpu_move_ok\(vm_cpu, src\.get\("sig"\), dn\.get\("sig"\)\):\n\s+continue'):
            self.assertRegex(self.body, re.compile(rule))

    def test_every_caller_passes_the_vm(self):
        calls = re.findall(r'pick_dest\((?:[^()]|\([^()]*\))*\)', self.main)
        calls = [c for c in calls if not c.startswith('pick_dest(vm, excluded)')
                 and not c.startswith('pick_dest(model')]
        self.assertGreaterEqual(len(calls), 7)
        for c in calls:
            self.assertIn('vm=vm', c, c)

    def test_restricted_empties_are_not_the_reserve(self):
        self.assertRegex(self.main, re.compile(
            r"empty = sum\(1 for x in nodes\.values\(\) if x\['model'\] == n\['model'\]\n"
            r"\s+and x\.get\('allowed'\) is None"))


if __name__ == '__main__':
    unittest.main()
