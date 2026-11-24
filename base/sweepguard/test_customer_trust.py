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
        self.assertEqual(ub.parse_command("trust-set 1302 - gen=7000000000000001")[3], (7000000000000001, False))
        self.assertEqual(ub.parse_command("trust-set 1302 1.1.1.1 closed gen=7000000000000001")[3],
                         (7000000000000001, True))
        self.assertEqual(ub.parse_command("trust-sync")[0], "trust-sync")
        self.assertEqual(ub.parse_command("trust-check 1.1.1.1")[0], "trust-check")

    def test_parse_rejects(self):
        for raw in ("trust-set 99 1.1.1.1", "trust-set 1302", "trust-set 1302 1.1.1.1 ",
                    "trust-set 1302 " + ",".join(f"1.1.1.{i}" for i in range(1, 12)),
                    "trust-set 1302 1.1.1.1;id", "trust-sync x", "trust-restore now",
                    "trust-check 10.0.0.1", "trust-set 01302 1.1.1.1",
                    "trust-set 1302 1.1.1.1 gen=700000000000000", "trust-set 1302 1.1.1.1 gen=9999999999999999"):
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
        d = P('{"gen": 7000000000000001, "vms": {"1302": ["1.1.1.1"]}}', "x")
        self.assertEqual(({k: [ub.entry_text(n) for n in v] for k, v in d["vms"].items()}, d["gen"]),
                         ({1302: ["1.1.1.1"]}, 7000000000000001))
        for raw in ('{"vms": {"1302": ["1.1.1.1"]}}', '{"gen": 9999999999999999, "vms": {}}',
                    '{"gen": true, "vms": {}}', '{"gen": "7000000000000001", "vms": {}}','{"vms": {"1302,1999": ["1.1.1.1"]}}', '{"vms": {"01302": []}}',
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
        st = ub.load_trust_state(fh.name)                          # ausente = vacio
        self.assertEqual((st.vms, st.gens, st.sync_gen), ({}, {}, 0))

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
    """Orden de las operaciones: revision = us*4 + fase; todo estricto."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.saved = (ub.TRUST_STATE, ub.set_exists, ub.run, ub.TrustChecker)
        ub.TRUST_STATE = os.path.join(self.dir, "cust_trust.json")
        ub.set_exists = lambda fam, name: True
        self.saved_pres = ub.set_presence
        ub.set_presence = lambda fam, name: True
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
        self.us = int(ub.time.time() * 1_000_000) - 60_000_000
        self.e = [ub.trust_entry("1.1.1.1")]

    def tearDown(self):
        ub.TRUST_STATE, ub.set_exists, ub.run, ub.TrustChecker = self.saved
        ub.set_presence = self.saved_pres

    def g(self, dus, phase):
        return (self.us + dus) * 4 + phase

    def live(self):
        return self.batches[-1]

    def test_set_then_late_revoke_keeps_new_list(self):
        self.assertEqual(ub.trust_set(1302, self.e, self.g(10, ub.GEN_SET))["result"], "set")
        self.assertEqual(ub.revoke_trust([1302], self.g(5, ub.GEN_REVOKE)), 0)     # revocacion atrasada
        self.assertIn("1.1.1.1", self.live())

    def test_revoke_then_late_set_is_stale(self):
        ub.trust_set(1302, self.e, self.g(0, ub.GEN_SET))
        ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))
        self.assertEqual(ub.trust_set(1302, self.e, self.g(5, ub.GEN_SET))["result"], "stale")
        self.assertNotIn("1.1.1.1", self.live())

    def test_same_microsecond_revoke_wins_and_reapply_follows(self):
        ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))
        self.assertEqual(ub.trust_set(1302, self.e, self.g(10, ub.GEN_SET))["result"], "stale")
        self.assertEqual(ub.trust_set(1302, self.e, self.g(10, ub.GEN_REAPPLY))["result"], "set")
        self.assertEqual(ub.trust_set(1302, self.e, self.g(10, ub.GEN_REAPPLY))["result"], "stale")  # empate = no

    def test_sync_is_a_floor_for_unknown_vms(self):
        ub.trust_sync({"gen": self.g(10, ub.GEN_SYNC), "vms": {}})                  # base nueva, foto vacia
        self.assertEqual(ub.trust_set(1999, self.e, self.g(5, ub.GEN_SET))["result"], "stale")
        self.assertEqual(ub.trust_set(1999, self.e, self.g(11, ub.GEN_SET))["result"], "set")

    def test_sync_does_not_undo_newer_remove(self):
        ub.trust_set(1302, self.e, self.g(0, ub.GEN_SET))
        ub.trust_set(1302, [], self.g(20, ub.GEN_SET))                              # el cliente la quita
        out = ub.trust_sync({"gen": self.g(10, ub.GEN_SYNC), "vms": {1302: self.e}})
        self.assertEqual(out["staleVms"], [1302])
        self.assertNotIn("1.1.1.1", self.live())
        self.assertTrue(ub.trust_sync({"gen": self.g(9, ub.GEN_SYNC), "vms": {1302: self.e}})["staleSnapshot"])

    def test_revoke_fails_on_unreadable_or_half_sets(self):
        ub.trust_set(1302, self.e, self.g(0, ub.GEN_SET))
        ub.set_presence = lambda fam, name: True if fam == "ip" else (_ for _ in ()).throw(RuntimeError("nft"))
        with self.assertRaises(RuntimeError):
            ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))
        ub.set_presence = lambda fam, name: fam == "ip"
        with self.assertRaises(RuntimeError):
            ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))
        ub.set_presence = lambda fam, name: False
        self.assertIsNone(ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE)))   # base sin desplegar

    def test_sync_fails_if_state_unreadable(self):
        with open(ub.TRUST_STATE, "w") as fh:
            fh.write("garbage")
        with self.assertRaises(RuntimeError):
            ub.trust_sync({"gen": self.g(10, ub.GEN_SYNC), "vms": {}})

    def test_revoke_retry_reapplies_live_set(self):
        ub.trust_set(1302, self.e, self.g(0, ub.GEN_SET))
        orig = ub.run

        def boom(cmd, stdin=None):
            if stdin is not None:
                raise ub.subprocess.TimeoutExpired(cmd, 20)
            return orig(cmd, stdin)
        ub.run = boom
        with self.assertRaises(RuntimeError):
            ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))
        ub.run = orig
        self.assertIn("1.1.1.1", open(ub.TRUST_STATE).read())               # el fichero volvio atras
        n = len(self.batches)
        ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))                  # mismo reintento
        self.assertEqual(len(self.batches), n + 1)
        self.assertNotIn("1.1.1.1", self.live())
        ub.revoke_trust([1302], self.g(10, ub.GEN_REVOKE))                  # otro: ya no cambia nada,
        self.assertEqual(len(self.batches), n + 2)                          # pero repara el set igual


class ClosedMode(Generations):
    def test_closed_needs_entries_and_follows_order(self):
        out = ub.trust_set(1302, self.e, self.g(1, ub.GEN_SET), closed=True)
        self.assertTrue(out["closed"])
        self.assertIn("add element ip rdpguard cust_closed { tcp . 11302, tcp . 21302, udp . 21302, tcp . 31302 }",
                      self.live())
        out = ub.trust_set(1302, [], self.g(2, ub.GEN_SET), closed=True)        # sin lista: queda abierta
        self.assertEqual((out["closed"], out["closedRefused"]), (False, "no_entries"))
        self.assertNotIn("add element ip rdpguard cust_closed", self.live())
        ub.trust_set(1302, self.e, self.g(3, ub.GEN_SET), closed=True)
        ub.revoke_trust([1302], self.g(4, ub.GEN_REVOKE))                      # VM reasignada: se abre
        self.assertNotIn("21302", self.live())
        self.assertEqual(ub.load_trust_state().closed, set())

    def test_sync_closed(self):
        ub.trust_sync({"gen": self.g(5, ub.GEN_SYNC), "vms": {1302: self.e, 1999: self.e}, "closed": {1999}})
        st = ub.load_trust_state()
        self.assertEqual(st.closed, {1999})
        closed = [l for l in self.live().splitlines() if "add element ip rdpguard cust_closed" in l][0]
        self.assertIn("tcp . 21999", closed)
        self.assertNotIn("21302", closed)

    def test_sync_reports_closed_missing(self):
        class Static:
            def reason(self, net):
                return "static" if str(net.network_address) == "45.155.205.9" else None
        ub.TrustChecker = Static
        out = ub.trust_sync({"gen": self.g(5, ub.GEN_SYNC), "closed": {1302, 1999},
                             "vms": {1302: [ub.trust_entry("45.155.205.9")], 1999: self.e}})
        self.assertEqual(out["closedMissing"], [1302])       # su única IP se rechaza aquí: queda abierta

    def test_pub6(self):
        import json as _j
        pol = os.path.join(self.dir, "pol.json")
        with open(pol, "w") as fh:
            _j.dump({"main": "2a01:4f9:2a:2d56::2"}, fh)
        saved, ub.IPV6_POLICY_STATE = ub.IPV6_POLICY_STATE, pol
        try:
            ub.trust_set(1517, [ub.trust_entry("2001:41d0:1:2::/64"), ub.trust_entry("1.1.1.1")],
                         self.g(1, ub.GEN_SET), closed=True)
            self.assertIn("2a01:4f9:2a:2d56::5ed . 2a01:4f9:c01f:e::5ed", self.live())   # (directa . su identidad)
            self.assertIn("2001:41d0:1:2::/64 . 2a01:4f9:2a:2d56::5ed", self.live())
        finally:
            ub.IPV6_POLICY_STATE = saved

    def _ct_run(self, rows, fail_list=False, fail_del=False):
        calls = []
        orig = ub.run

        class R:
            def __init__(self, out="", err="", rc=0):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def run(cmd, stdin=None):
            if cmd[0] == "conntrack" and cmd[1] == "-L":
                if fail_list:
                    return R(err="conntrack: Operation not permitted", rc=1)
                fam = cmd[cmd.index("-f") + 1]; proto = cmd[cmd.index("-p") + 1]
                key = cmd[cmd.index("--orig-port-dst") + 1] if "--orig-port-dst" in cmd else cmd[cmd.index("-d") + 1]
                return R("\n".join(rows.get((fam, proto, key), [])))
            if cmd[0] == "conntrack" and cmd[1] == "-D":
                calls.append(cmd)
                if fail_del:
                    return R(err="conntrack: Operation failed", rc=1)
                return R(err="conntrack v1.4.8: 1 flow entries have been deleted.")
            if cmd[:3] == ["nft", "-j", "list"] and cmd[-1] == "bf_allow":
                elem = '["95.216.102.179"]' if cmd[4] == "ip" else "[]"
                return R('{"nftables": [{"set": {"name": "bf_allow", "elem": %s}}]}' % elem)
            return orig(cmd, stdin)
        return run, calls

    def test_cut_flows_on_close(self):
        rows = {("ipv4", "tcp", "21302"): [
            "tcp 6 300 ESTABLISHED src=8.8.8.8 dst=77.42.49.79 sport=1 dport=21302 src=10.0.0.3 dst=77.42.49.79 sport=21302 dport=1",
            "tcp 6 300 ESTABLISHED src=1.1.1.1 dst=77.42.49.79 sport=2 dport=21302 src=10.0.0.3 dst=77.42.49.79 sport=21302 dport=2",
            "tcp 6 300 ESTABLISHED src=95.216.102.179 dst=77.42.49.79 sport=3 dport=21302 src=10.0.0.3 dst=77.42.49.79 sport=21302 dport=3",
            # salida de un invitado a un servidor externo :21302 (sin DNAT): no se toca
            "tcp 6 300 ESTABLISHED src=10.64.1.2 dst=5.6.7.8 sport=4 dport=21302 src=5.6.7.8 dst=91.98.53.130 sport=21302 dport=4"],
            ("ipv6", "tcp", "21302"): [
            # directa de OTRA VM en el mismo puerto (puerto sin traducir): no se toca
            "tcp 6 300 ESTABLISHED src=2001:db8:9::1 dst=2a01:4f9:2a:2d56::7cf sport=5 dport=21302 src=2a01:4f9:c01f:e::7cf dst=2001:db8:9::1 sport=21302 dport=5",
            # forward v6 de esta VM (puerto traducido a 3389): se corta
            "tcp 6 300 ESTABLISHED src=2001:db8:9::1 dst=2a01:4f9:2a:2d56::2 sport=6 dport=21302 src=2a01:4f9:c01f:e::51e dst=2001:db8:9::1 sport=3389 dport=6"]}
        run, calls = self._ct_run(rows)
        ub.run = run
        try:
            out = ub.trust_set(1302, self.e, self.g(1, ub.GEN_SET), closed=True)
        finally:
            ub.run = self.saved[2]
        self.assertEqual((out["flowsCut"], out["flowsCutOk"]), (2, True))
        self.assertEqual(sorted(c[c.index("-s") + 1] for c in calls), ["2001:db8:9::1", "8.8.8.8"])
        self.assertTrue(all("--sport" in c and "--dport" in c and "-d" in c for c in calls))   # tupla completa

    def test_cut_flows_failure_stays_pending_and_retries(self):
        rows = {("ipv4", "tcp", "21302"): [
            "tcp 6 300 ESTABLISHED src=8.8.8.8 dst=77.42.49.79 sport=1 dport=21302 src=10.0.0.3 dst=77.42.49.79 sport=21302 dport=1"]}
        run, calls = self._ct_run(rows, fail_list=True)
        ub.run = run
        try:
            out = ub.trust_set(1302, self.e, self.g(1, ub.GEN_SET), closed=True)
            self.assertEqual(out["flowsCutOk"], False)
            self.assertEqual(ub.load_trust_state().cut_pending, {1302})
            run2, calls2 = self._ct_run(rows, fail_del=True)
            ub.run = run2
            out = ub.trust_set(1302, self.e, self.g(2, ub.GEN_SET), closed=True)   # sin cambios: reintenta
            self.assertEqual((out["flowsCutOk"], len(calls2)), (False, 1))
            run3, calls3 = self._ct_run(rows)
            ub.run = run3
            out = ub.trust_restore()
            self.assertEqual((out["flowsCutOk"], len(calls3)), (True, 1))
            self.assertEqual(ub.load_trust_state().cut_pending, set())
        finally:
            ub.run = self.saved[2]

    def test_doc_closed_requires_list(self):
        with self.assertRaises(ub.BadRequest):
            ub.parse_trust_doc('{"gen": 7000000000000001, "vms": {}, "closed": ["1302"]}', "x")


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
