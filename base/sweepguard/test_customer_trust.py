"""Tests sin nft de las IPs de confianza (24/11/2026): formato de entradas,
peticiones del script restringido y el Exempt de sweepguard.

    python3 base/sweepguard/test_customer_trust.py
La prueba con nft y paquetes de verdad es test_customer_trust_netns.sh."""
import importlib.machinery
import importlib.util
import ipaddress
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_l = importlib.machinery.SourceFileLoader("ub", os.path.join(HERE, "neuravps-unblock-ip"))
ub = importlib.util.module_from_spec(importlib.util.spec_from_loader("ub", _l))
_l.exec_module(ub)
_s = importlib.util.spec_from_file_location("sg", os.path.join(HERE, "sweepguard.py"))
sg = importlib.util.module_from_spec(_s)
_s.loader.exec_module(sg)


class Entries(unittest.TestCase):
    def ok(self, raw, want):
        self.assertEqual(ub.entry_text(ub.trust_entry(raw)), want)

    def bad(self, raw):
        with self.assertRaises(ub.BadRequest):
            ub.trust_entry(raw)

    def test_accepted(self):
        self.ok("149.56.212.201", "149.56.212.201")
        self.ok("149.56.212.201/32", "149.56.212.201")
        self.ok("2001:41d0:1:2::5", "2001:41d0:1:2::5")
        self.ok("2001:41d0:1:2::5/128", "2001:41d0:1:2::5")
        self.ok("2001:41d0:1:2::5/64", "2001:41d0:1:2::/64")      # bits de host fuera
        self.ok("2001:41D0:0001:0002::/64", "2001:41d0:1:2::/64")

    def test_rejected(self):
        for raw in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "100.64.0.1", "169.254.1.1",
                    "224.0.0.1", "203.0.113.9", "0.0.0.0", "255.255.255.255",
                    "1.2.3.0/24", "1.2.3.4/31", "1.2.3.4/032",
                    "::1", "fe80::1", "fc00::1", "ff02::1", "2001:db8::1",
                    "::ffff:1.2.3.4", "64:ff9b::102:304", "64:ff9b:1::1",
                    "2001:41d0::/48", "2001:41d0:1:2::/96", "2001:41d0:1:2::/0",
                    "4000::1", "fec0::1", "fe80::1%eth0", "2001:41d0:1:2::5%1",
                    "1.1.1.1 ", "１.1.1.1", None, 5,
                    "x", "", "1.2.3.4/", "1.2.3.4/a"):
            self.bad(raw)


class Commands(unittest.TestCase):
    def test_parse(self):
        v, ents, vmids, _ = ub.parse_command("trust-set 1302 149.56.212.201,2001:41d0:1:2::/64")
        self.assertEqual((v, vmids, [ub.entry_text(n) for n in ents]),
                         ("trust-set", [1302], ["149.56.212.201", "2001:41d0:1:2::/64"]))
        self.assertEqual(ub.parse_command("trust-set 1302 -")[1], [])
        self.assertEqual(ub.parse_command("trust-set 1302 - gen=1764000000000")[3], 1764000000000)
        self.assertEqual(ub.parse_command("trust-sync")[0], "trust-sync")
        self.assertEqual(ub.parse_command("trust-check 1.1.1.1")[0], "trust-check")

    def test_parse_rejects(self):
        for raw in ("trust-set 99 1.1.1.1", "trust-set 1302", "trust-set 1302 1.1.1.1 ",
                    "trust-set 1302 " + ",".join(f"1.1.1.{i}" for i in range(1, 12)),
                    "trust-set 1302 1.1.1.1;id", "trust-sync x", "trust-restore now",
                    "trust-check 10.0.0.1", "trust-set 01302 1.1.1.1",
                    "trust-set 1302 1.1.1.1 gen=176400000000", "trust-set 1302 1.1.1.1 gen=9999999999999"):
            with self.assertRaises(ub.BadRequest, msg=raw):
                ub.parse_command(raw)

    def test_clean_list(self):
        class NoBase:
            def reason(self, net):
                return "static" if str(net.network_address) == "45.155.205.9" else None
        nets = [ub.trust_entry(x) for x in ("1.1.1.1", "1.1.1.1", "45.155.205.9",
                                            "2001:41d0:1:2::/64", "2001:41d0:1:2::7")]
        ok, rej = ub.clean_list(nets, NoBase())
        self.assertEqual([ub.entry_text(n) for n in ok], ["1.1.1.1", "2001:41d0:1:2::/64"])
        self.assertEqual(rej, {"45.155.205.9": "static", "2001:41d0:1:2::7": "covered"})

    def test_elements(self):
        st = {1302: [ub.trust_entry("1.1.1.1"), ub.trust_entry("2001:41d0:1:2::/64")]}
        el = ub.trust_elements(st)
        self.assertEqual(el["ip"], ["1.1.1.1 . tcp . 11302", "1.1.1.1 . tcp . 21302",
                                    "1.1.1.1 . udp . 21302", "1.1.1.1 . tcp . 31302"])
        self.assertEqual(el["ip6"][0], "2001:41d0:1:2::/64 . tcp . 11302")

    def test_doc_all_or_nothing(self):
        P = ub.parse_trust_doc
        d = P('{"gen": 1764000000000, "vms": {"1302": ["1.1.1.1"]}}', "x")
        self.assertEqual(({k: [ub.entry_text(n) for n in v] for k, v in d["vms"].items()}, d["gen"]),
                         ({1302: ["1.1.1.1"]}, 1764000000000))
        for raw in ('{"vms": {"1302": ["1.1.1.1"]}}', '{"gen": 9999999999999, "vms": {}}',
                    '{"gen": true, "vms": {}}', '{"gen": "1764000000000", "vms": {}}','{"vms": {"1302,1999": ["1.1.1.1"]}}', '{"vms": {"01302": []}}',
                    '{"vms": {"99": []}}', '{"vms": {"1302": ["1.1.1.1"], "1302": []}}',
                    '{"vms": {"1302": ["1.1.1.1", "10.0.0.1"]}}', '{"vms": {"1302": ["fe80::1%eth0"]}}',
                    '{"vms": {"1302": [1]}}', '{"vms": {"1302": "1.1.1.1"}}', '[]', 'x'):
            with self.assertRaises(ub.BadRequest, msg=raw):
                P(raw if raw.startswith(('{"vms"', '{"gen"')) or not raw.startswith("{")
                  else raw, "x")

    def test_state_file_fails_closed(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"vms": {"1302": ["1.1.1.1"], "1999": ["10.0.0.1"]}}')
        with self.assertRaises(RuntimeError):
            ub.load_trust_state(fh.name)
        os.unlink(fh.name)
        self.assertEqual(ub.load_trust_state(fh.name), ({}, {}))   # ausente = vacio

    def test_guarded_read_failure_fails_closed(self):
        class R:
            returncode, stdout, stderr = 1, "", "Error: No such file"
        orig = ub.run
        ub.run = lambda *a, **k: R()
        try:
            with self.assertRaises(RuntimeError):
                ub.TrustChecker().reason(ub.trust_entry("1.1.1.1"))
        finally:
            ub.run = orig


class Generations(unittest.TestCase):
    """Orden de las operaciones: una orden atrasada no deshace una posterior."""

    def setUp(self):
        import tempfile
        global T0
        T0 = ub.now_ms() - 60000
        self.dir = tempfile.mkdtemp()
        self.saved = (ub.TRUST_STATE, ub.set_exists, ub.run, ub.TrustChecker)
        ub.TRUST_STATE = os.path.join(self.dir, "cust_trust.json")
        ub.set_exists = lambda fam, name: True
        self.batches = []

        class R:
            returncode, stdout, stderr = 0, "", ""

        def run(cmd, stdin=None):
            if stdin is not None:
                self.batches.append(stdin)
            return R()
        ub.run = run

        class NoBase:
            def reason(self, net):
                return None
        ub.TrustChecker = NoBase

    def tearDown(self):
        ub.TRUST_STATE, ub.set_exists, ub.run, ub.TrustChecker = self.saved

    def live(self):
        return self.batches[-1]

    def test_stale_set_after_revoke(self):
        e = [ub.trust_entry("1.1.1.1")]
        self.assertEqual(ub.trust_set(1302, e, (T0 + 0))["result"], "set")
        ub.revoke_trust([1302], (T0 + 5000))                 # VM reasignada
        self.assertNotIn("1.1.1.1", self.live())
        out = ub.trust_set(1302, e, (T0 + 3000))             # alta del dueño anterior, atrasada
        self.assertEqual(out["result"], "stale")
        self.assertNotIn("1.1.1.1", self.live())
        self.assertEqual(ub.trust_set(1302, e, (T0 + 6000))["result"], "set")   # posterior: entra

    def test_sync_does_not_undo_newer_remove(self):
        ub.trust_set(1302, [ub.trust_entry("1.1.1.1")], (T0 + 0))
        ub.trust_set(1302, [], (T0 + 9000))                  # el cliente la quita
        out = ub.trust_sync({"gen": (T0 + 5000), "vms": {1302: [ub.trust_entry("1.1.1.1")]}})
        self.assertEqual(out["staleVms"], [1302])
        self.assertNotIn("1.1.1.1", self.live())

    def test_revoke_retry_reapplies_live_set(self):
        ub.trust_set(1302, [ub.trust_entry("1.1.1.1")], (T0 + 0))
        orig = ub.run

        def boom(cmd, stdin=None):
            if stdin is not None:
                raise ub.subprocess.TimeoutExpired(cmd, 20)
            return orig(cmd, stdin)
        ub.run = boom
        with self.assertRaises(RuntimeError):
            ub.revoke_trust([1302], (T0 + 5000))
        ub.run = orig
        n = len(self.batches)
        ub.revoke_trust([1302], (T0 + 6000))                 # reintento: VM ya fuera del fichero
        self.assertEqual(len(self.batches), n + 1)             # pero el set se reescribe igual
        self.assertNotIn("1.1.1.1", self.live())


class SweepguardExempt(unittest.TestCase):
    def test_exempt(self):
        L4, L6 = "95.216.102.179", "2a01:4f9:2a:2d56::2"
        ex = sg.Exempt({("1.1.1.1", 21302)}, [(ipaddress.ip_network("2001:41d0:1:2::/64"), 31302),
                                               (ipaddress.ip_network("9.9.9.9/32"), 21302)],
                       local={L4, L6})
        self.assertIn(("1.1.1.1", 21302), ex)                       # cust_allow: sin destino
        self.assertIn(("1.1.1.1", 21302, "8.8.8.8"), ex)
        self.assertIn(("9.9.9.9", 21302, L4), ex)
        self.assertNotIn(("9.9.9.9", 21302), ex)                    # bf_seen: la confianza no exime
        self.assertNotIn(("9.9.9.9", 21302, "95.216.102.180"), ex)  # otro destino (no local)
        self.assertIn(("2001:41d0:1:2::abcd", 31302, "2a01:04f9:002a:2d56:0000:0000:0000:0002"), ex)
        self.assertNotIn(("2001:41d0:1:2::abcd", 31302, "2a01:4f9:2a:2d56::c9"), ex)   # IPv6 directa de otra VM
        self.assertNotIn(("2001:41d0:1:2::abcd", 21302, L6), ex)
        self.assertNotIn(("2001:41d0:1:3::1", 31302, L6), ex)
        self.assertNotIn(("9.9.9.9", 21999, L4), ex)
        self.assertNotIn(("9.9.9.9", 21302, L4), sg.Exempt(set(), [(ipaddress.ip_network("9.9.9.9/32"), 21302)]))
        self.assertTrue(ex)
        self.assertFalse(sg.Exempt())

    def test_hot_port_ignores_trusted(self):
        cfg = dict(sg.DEFAULTS)
        entries = [{"proto": "tcp", "src": "149.56.212.201", "dport": 31302, "dst": "95.216.102.179", "state": "SYN_SENT",
                    "offload": False, "timeout": 100, "assured": False, "unreplied": True}] * 10
        ab, _ = sg.hot_port_scan(entries, cfg, 432000, {}, now=0)
        self.assertIn("149.56.212.201", ab)
        ex = sg.Exempt(set(), [(ipaddress.ip_network("149.56.212.201/32"), 31302)], local={"95.216.102.179"})
        ab, _ = sg.hot_port_scan(entries, cfg, 432000, {}, now=0, exempt=ex)
        self.assertEqual(ab, {})
        fl, _ = sg.flood_scan(entries * 11, cfg, 432000, {}, ex, now=0)
        self.assertEqual(fl, {})


if __name__ == "__main__":
    unittest.main()
