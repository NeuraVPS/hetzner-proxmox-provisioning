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
        self.assertEqual(ub.parse_command("trust-sync")[0], "trust-sync")
        self.assertEqual(ub.parse_command("trust-check 1.1.1.1")[0], "trust-check")

    def test_parse_rejects(self):
        for raw in ("trust-set 99 1.1.1.1", "trust-set 1302", "trust-set 1302 1.1.1.1 ",
                    "trust-set 1302 " + ",".join(f"1.1.1.{i}" for i in range(1, 12)),
                    "trust-set 1302 1.1.1.1;id", "trust-sync x", "trust-restore now",
                    "trust-check 10.0.0.1", "trust-set 01302 1.1.1.1"):
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
        self.assertEqual({k: [ub.entry_text(n) for n in v] for k, v in
                          P('{"vms": {"1302": ["1.1.1.1"]}}', "x").items()}, {1302: ["1.1.1.1"]})
        for raw in ('{"vms": {"1302,1999": ["1.1.1.1"]}}', '{"vms": {"01302": []}}',
                    '{"vms": {"99": []}}', '{"vms": {"1302": ["1.1.1.1"], "1302": []}}',
                    '{"vms": {"1302": ["1.1.1.1", "10.0.0.1"]}}', '{"vms": {"1302": ["fe80::1%eth0"]}}',
                    '{"vms": {"1302": [1]}}', '{"vms": {"1302": "1.1.1.1"}}', '[]', 'x'):
            with self.assertRaises(ub.BadRequest, msg=raw):
                P(raw, "x")

    def test_state_file_fails_closed(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"vms": {"1302": ["1.1.1.1"], "1999": ["10.0.0.1"]}}')
        with self.assertRaises(RuntimeError):
            ub.load_trust_state(fh.name)
        os.unlink(fh.name)
        self.assertEqual(ub.load_trust_state(fh.name), {})         # ausente = vacio

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


class SweepguardExempt(unittest.TestCase):
    def test_exempt(self):
        ex = sg.Exempt({("1.1.1.1", 21302)}, [(ipaddress.ip_network("2001:41d0:1:2::/64"), 31302),
                                               (ipaddress.ip_network("9.9.9.9/32"), 21302)])
        self.assertIn(("1.1.1.1", 21302), ex)
        self.assertIn(("9.9.9.9", 21302), ex)
        self.assertIn(("2001:41d0:1:2::abcd", 31302), ex)
        self.assertNotIn(("2001:41d0:1:2::abcd", 21302), ex)
        self.assertNotIn(("2001:41d0:1:3::1", 31302), ex)
        self.assertNotIn(("9.9.9.9", 21999), ex)
        self.assertTrue(ex)
        self.assertFalse(sg.Exempt())

    def test_hot_port_ignores_trusted(self):
        cfg = dict(sg.DEFAULTS)
        entries = [{"proto": "tcp", "src": "149.56.212.201", "dport": 31302, "state": "SYN_SENT",
                    "offload": False, "timeout": 100, "assured": False, "unreplied": True}] * 10
        ab, _ = sg.hot_port_scan(entries, cfg, 432000, {}, now=0)
        self.assertIn("149.56.212.201", ab)
        ex = sg.Exempt(set(), [(ipaddress.ip_network("149.56.212.201/32"), 31302)])
        ab, _ = sg.hot_port_scan(entries, cfg, 432000, {}, now=0, exempt=ex)
        self.assertEqual(ab, {})
        fl, _ = sg.flood_scan(entries * 11, cfg, 432000, {}, ex, now=0)
        self.assertEqual(fl, {})


if __name__ == "__main__":
    unittest.main()
