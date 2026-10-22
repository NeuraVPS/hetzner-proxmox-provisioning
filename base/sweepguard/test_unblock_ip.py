"""Tests de neuravps-unblock-ip (boton «Desbloquear mi IP», 18/10/2026).

    python3 -m pytest base/sweepguard/test_unblock_ip.py -q
    python3 base/sweepguard/test_unblock_ip.py           # sin pytest

`nft` y `conntrack` se simulan: un estado en memoria con los sets de las dos
familias y un registro de cada orden ejecutada.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("unblock", os.path.join(HERE, "neuravps-unblock-ip"))
spec = importlib.util.spec_from_loader("unblock", _loader)
ub = importlib.util.module_from_spec(spec)
_loader.exec_module(ub)


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeBase:
    """Sets de rdpguard: {(fam, set): {elementos}}; bf_seen guarda (addr, port)."""

    def __init__(self):
        self.sets = {("ip", "bf_allow"): set(), ("ip", "bf_static"): set(),
                     ("ip", "bf_auto"): set(), ("ip", "bf_seen"): set(),
                     ("ip6", "bf_allow"): set(), ("ip6", "bf_auto"): set(),
                     ("ip6", "bf_seen"): set(),
                     ("ip", "cust_allow"): set(), ("ip6", "cust_allow"): set()}
        self.expires = {}                  # (fam, addr, port) -> timeout de cust_allow
        self.conntrack = {}
        self.calls = []
        self.fail_batch = False

    def run(self, cmd, stdin=None):
        self.calls.append((cmd, stdin))
        if cmd[0] == "conntrack":
            n = self.conntrack.pop(cmd[-1], 0)
            if n:
                return Done(0, err=f"conntrack v1.4.8 (conntrack-tools): {n} flow entries have been deleted.\n")
            return Done(1, err="conntrack v1.4.8 (conntrack-tools): 0 flow entries have been deleted.\n")
        assert cmd[0] == "nft", cmd
        if cmd[1:3] == ["-t", "list"]:
            return Done(0 if (cmd[4], cmd[6]) in self.sets else 1)
        if cmd[1:3] == ["-j", "list"]:
            fam, name = cmd[4], cmd[6]
            if (fam, name) not in self.sets:
                return Done(1, err="Error: No such file or directory")
            elem = [{"elem": {"val": {"concat": [a, p]},
                              "expires": self.expires.get((fam, a, p), 1000)}}
                    for a, p in sorted(self.sets[(fam, name)])]
            return Done(0, out=json.dumps({"nftables": [{"metainfo": {}},
                                                        {"set": {"name": name, "elem": elem}}]}))
        if cmd[1:3] == ["-f", "-"]:
            if self.fail_batch:
                return Done(1, err="Error: Could not process rule: No such file or directory")
            staged = {k: set(v) for k, v in self.sets.items()}
            for ln in stdin.splitlines():
                m = re.fullmatch(r"(delete|add) element (ip6?) rdpguard (bf_seen|cust_allow) "
                                 r"\{ (\S+) \. (\d+)(?: timeout (\d+)s)? \}", ln)
                assert m, ln
                key, item = (m[2], m[3]), (m[4], int(m[5]))
                if m[1] == "delete":
                    if item not in staged[key]:      # la transaccion entera falla
                        return Done(1, err="Error: Could not process rule: No such file or directory")
                    staged[key].discard(item)
                else:
                    staged[key].add(item)
                    self.expires[(m[2], m[4], int(m[5]))] = int(m[6])
            self.sets = staged
            return Done(0)
        verb, fam, name = cmd[1], cmd[3], cmd[5]
        assert cmd[2] == "element" and cmd[4] == "rdpguard", cmd
        key = (fam, name)
        if key not in self.sets:
            return Done(1, err=f"Error: No such file or directory; did you mean set 'x' in table {fam} 'rdpguard'?")
        inner = cmd[6].strip("{} ")
        if name in ("bf_seen", "cust_allow"):
            a, _, p = inner.partition(" . ")
            p, _, tmo = p.partition(" timeout ")
            item = (a, int(p))
        else:
            item = inner
        if verb == "add":
            self.sets[key].add(item)
            self.expires[(fam, item[0], item[1])] = int(tmo.rstrip("s"))
            return Done(0)
        if verb == "get":
            ok = item in self.sets[key]
        elif verb == "delete":
            ok = item in self.sets[key]
            self.sets[key].discard(item)
        else:
            raise AssertionError(cmd)
        return Done(0) if ok else Done(1, err="Error: Could not process rule: No such file or directory")


def call(base, raw, env=True):
    buf = io.StringIO()
    environ = {"SSH_ORIGINAL_COMMAND": raw} if env else {}
    with mock.patch.object(ub, "run", base.run), \
         mock.patch.dict(os.environ, environ, clear=True), \
         mock.patch.object(ub, "LOCK", os.devnull), \
         mock.patch.object(ub.syslog, "syslog"), redirect_stdout(buf):
        rc = ub.main(["neuravps-unblock-ip"] + ([] if env else raw.split()))
    return rc, json.loads(buf.getvalue())


class ParseRequest(unittest.TestCase):
    def test_accepts_v4_and_v6(self):
        self.assertEqual(str(ub.parse_request("unblock 90.249.134.23")), "90.249.134.23")
        self.assertEqual(str(ub.parse_request("unblock 2A02:9130:0::1")), "2a02:9130::1")

    def test_v4_mapped_becomes_v4(self):
        self.assertEqual(str(ub.parse_request("unblock ::ffff:90.249.134.23")), "90.249.134.23")

    def test_rejects_everything_else(self):
        for raw in ["", "unblock", "unblock  1.2.3.4", "unblock 1.2.3.4 ", " unblock 1.2.3.4",
                    "unblock 1.2.3.4; rm -rf /", "unblock 1.2.3.4\n", "unblock $(id)",
                    "unblock 1.2.3.4/24", "UNBLOCK 1.2.3.4", "block 1.2.3.4", "unblock 999.1.1.1",
                    "unblock 1.2.3.4 1.2.3.5", "cat /etc/shadow", "unblock fe80::1%eth0",
                    # no globales: privadas, nuestras internas, loopback, documentacion
                    "unblock 10.64.1.2", "unblock 127.0.0.1", "unblock 192.168.1.1",
                    "unblock 64:ff9b:1::5fd8:66b3", "unblock fd00::1", "unblock ::1",
                    "unblock 0.0.0.0", "unblock 192.0.2.1"]:
            with self.assertRaises(ub.BadRequest, msg=raw):
                ub.parse_request(raw)


class Unblock(unittest.TestCase):
    def setUp(self):
        self.b = FakeBase()

    def test_unblocks_v4_and_cleans_seen_and_conntrack(self):
        ip = "90.249.134.23"
        self.b.sets[("ip", "bf_auto")] |= {ip, "8.8.4.4"}
        self.b.sets[("ip", "bf_seen")] |= {(ip, 20000 + i) for i in range(20)} | {("8.8.4.4", 20001)}
        self.b.conntrack[ip] = 7
        rc, out = call(self.b, f"unblock {ip}")
        self.assertEqual(rc, 0)
        self.assertEqual(out["result"], "unblocked")
        self.assertEqual(out["seenForgotten"], 20)
        self.assertEqual(out["conntrackDeleted"], 7)
        self.assertEqual(self.b.sets[("ip", "bf_auto")], {"8.8.4.4"})
        self.assertEqual(self.b.sets[("ip", "bf_seen")], {("8.8.4.4", 20001)})

    def test_conntrack_uses_the_right_family(self):
        ip = "2a02:9130::1"
        self.b.sets[("ip6", "bf_auto")].add(ip)
        rc, out = call(self.b, f"unblock {ip}")
        self.assertEqual((rc, out["result"], out["family"]), (0, "unblocked", "ip6"))
        ct = [c for c, _ in self.b.calls if c[0] == "conntrack"]
        self.assertEqual(ct, [["conntrack", "-D", "-f", "ipv6", "-s", ip]])

    def test_not_blocked_touches_nothing(self):
        ip = "90.249.134.23"
        self.b.sets[("ip", "bf_seen")].add((ip, 20001))
        rc, out = call(self.b, f"unblock {ip}")
        self.assertEqual((rc, out["result"]), (0, "not_blocked"))
        self.assertEqual(self.b.sets[("ip", "bf_seen")], {(ip, 20001)})
        self.assertFalse([c for c, _ in self.b.calls if c[0] == "conntrack" or c[1] in ("delete", "-f")])

    def test_static_is_never_touched(self):
        ip = "90.249.134.23"
        self.b.sets[("ip", "bf_static")].add(ip)
        self.b.sets[("ip", "bf_auto")].add(ip)
        rc, out = call(self.b, f"unblock {ip}")
        self.assertEqual((rc, out["result"]), (0, "static"))
        self.assertIn(ip, self.b.sets[("ip", "bf_static")])
        self.assertIn(ip, self.b.sets[("ip", "bf_auto")])
        self.assertFalse([c for c, _ in self.b.calls if "delete" in c])

    def test_ip6_has_no_bf_static(self):
        rc, out = call(self.b, "unblock 2a02:9130::1")
        self.assertEqual((rc, out["result"]), (0, "not_blocked"))

    def test_never_touches_bf_allow(self):
        ip = "188.40.215.246"
        self.b.sets[("ip", "bf_allow")].add(ip)
        self.b.sets[("ip", "bf_auto")].add(ip)
        call(self.b, f"unblock {ip}")
        self.assertIn(ip, self.b.sets[("ip", "bf_allow")])
        for cmd, stdin in self.b.calls:
            self.assertNotIn("bf_allow", cmd)
            self.assertNotIn("bf_allow", stdin or "")

    def test_seen_batch_falls_back_one_by_one(self):
        ip = "90.249.134.23"
        self.b.sets[("ip", "bf_auto")].add(ip)
        self.b.sets[("ip", "bf_seen")] |= {(ip, 20001), (ip, 20002)}
        self.b.fail_batch = True
        rc, out = call(self.b, f"unblock {ip}")
        self.assertEqual((rc, out["seenForgotten"]), (0, 2))
        self.assertEqual(self.b.sets[("ip", "bf_seen")], set())

    def test_bad_request_runs_nothing(self):
        rc, out = call(self.b, "unblock 1.2.3.4; nft flush ruleset")
        self.assertEqual((rc, out["error"]), (2, "bad_request"))
        self.assertEqual(self.b.calls, [])

    def test_nft_failure_is_an_error(self):
        def broken(cmd, stdin=None):
            return Done(1, err="Error: cache initialization failed: Operation not permitted")
        self.b.run = broken
        rc, out = call(self.b, "unblock 90.249.134.23")
        self.assertEqual((rc, out["ok"], out["error"]), (1, False, "internal"))

    def test_manual_invocation_without_ssh(self):
        self.b.sets[("ip", "bf_auto")].add("90.249.134.23")
        rc, out = call(self.b, "unblock 90.249.134.23", env=False)
        self.assertEqual((rc, out["result"]), (0, "unblocked"))


A4 = "47.194.157.202"
A6 = "2606:4d80:1300:22c5:54a3:613e:a786:5e30"


class ParseCommand(unittest.TestCase):
    def test_accepts_the_three_verbs(self):
        self.assertEqual(ub.parse_command(f"status {A4}")[:2], ("status", ub.ipaddress.ip_address(A4)))
        verb, ip, vmids, console = ub.parse_command(f"allow-vm {A6} 1671,1670,1671 console")
        self.assertEqual((verb, str(ip), vmids, console), ("allow-vm", A6, [1670, 1671], True))
        self.assertEqual(ub.parse_command(f"allow-vm {A4} 100")[2:], ([100], False))
        self.assertEqual(ub.parse_command(f"unblock {A4}")[0], "unblock")

    def test_rejects_bad_allow_vm(self):
        many = ",".join(str(1000 + i) for i in range(ub.MAX_VMIDS + 1))
        for raw in [f"allow-vm {A4}", f"allow-vm {A4} ", f"allow-vm {A4} 1671,", f"allow-vm {A4} ,1671",
                    f"allow-vm {A4} 99", f"allow-vm {A4} 10000", f"allow-vm {A4} 01671",
                    f"allow-vm {A4} 1671 consola", f"allow-vm {A4} 1671  console",
                    f"allow-vm {A4} 1671 console ", f"allow-vm {A4} 1671;id", f"allow-vm {A4} -1",
                    f"allow-vm 10.64.1.2 1671", f"allow-vm {A4} {many}", f"allow-vm {A4}/24 1671",
                    f"allow-vm 1671 {A4}", f"status {A4} 1671", f"status 192.168.1.1",
                    f"ALLOW-VM {A4} 1671", f"allow-vm {A4} 1671\n"]:
            with self.assertRaises(ub.BadRequest, msg=raw):
                ub.parse_command(raw)

    def test_ports_per_vm(self):
        self.assertEqual(ub.vm_ports([1670, 1671]), [11670, 11671, 21670, 21671, 31670, 31671])
        self.assertEqual(ub.vm_ports([100], console=True), [443, 10100, 20100, 30100])


class AllowVm(unittest.TestCase):
    def setUp(self):
        self.b = FakeBase()

    def test_allows_only_their_ports_and_keeps_bf_auto(self):
        self.b.sets[("ip", "bf_auto")].add(A4)
        self.b.sets[("ip", "bf_seen")] |= {(A4, 31671), (A4, 21999), ("8.8.4.4", 31671)}
        rc, out = call(self.b, f"allow-vm {A4} 1670,1671 console")
        self.assertEqual((rc, out["result"], out["ports"], out["renewed"]), (0, "allowed", 7, 0))
        self.assertEqual(self.b.sets[("ip", "cust_allow")],
                         {(A4, p) for p in (443, 11670, 11671, 21670, 21671, 31670, 31671)})
        self.assertIn(A4, self.b.sets[("ip", "bf_auto")])          # el resto de la base, cerrado
        self.assertEqual(out["seenForgotten"], 1)                   # solo sus puertos
        self.assertEqual(self.b.sets[("ip", "bf_seen")], {(A4, 21999), ("8.8.4.4", 31671)})
        self.assertEqual({v for (f, a, p), v in self.b.expires.items()}, {ub.ALLOW_SECONDS})
        self.assertFalse([c for c, _ in self.b.calls if c[0] == "conntrack"])

    def test_v6_goes_to_ip6_set(self):
        self.b.sets[("ip6", "bf_auto")].add(A6)
        rc, out = call(self.b, f"allow-vm {A6} 1671")
        self.assertEqual((rc, out["result"], out["family"]), (0, "allowed", "ip6"))
        self.assertEqual(self.b.sets[("ip6", "cust_allow")], {(A6, 11671), (A6, 21671), (A6, 31671)})
        self.assertEqual(self.b.sets[("ip", "cust_allow")], set())

    def test_renews_in_one_transaction(self):
        self.b.sets[("ip", "bf_auto")].add(A4)
        self.b.sets[("ip", "cust_allow")] |= {(A4, 21671), ("8.8.4.4", 21671), (A4, 21999)}
        self.b.expires[("ip", A4, 21671)] = 60
        rc, out = call(self.b, f"allow-vm {A4} 1671")
        self.assertEqual((rc, out["renewed"]), (0, 1))
        self.assertEqual(self.b.expires[("ip", A4, 21671)], ub.ALLOW_SECONDS)
        # nunca quita entradas: ni las de otra IP ni otras VMs de esta (IP compartida)
        self.assertIn(("8.8.4.4", 21671), self.b.sets[("ip", "cust_allow")])
        self.assertIn((A4, 21999), self.b.sets[("ip", "cust_allow")])
        batches = [i for c, i in self.b.calls if c[1:3] == ["-f", "-"] and "cust_allow" in (i or "")]
        self.assertEqual(len(batches), 1)
        self.assertIn(f"delete element ip rdpguard cust_allow {{ {A4} . 21671 }}", batches[0])

    def test_batch_failure_falls_back_one_by_one(self):
        self.b.sets[("ip", "bf_auto")].add(A4)
        self.b.fail_batch = True
        rc, out = call(self.b, f"allow-vm {A4} 1671")
        self.assertEqual((rc, out["result"]), (0, "allowed"))
        self.assertEqual(len(self.b.sets[("ip", "cust_allow")]), 3)

    def test_not_blocked_adds_nothing(self):
        rc, out = call(self.b, f"allow-vm {A4} 1671")
        self.assertEqual((rc, out["result"]), (0, "not_blocked"))
        self.assertEqual(self.b.sets[("ip", "cust_allow")], set())

    def test_static_adds_nothing(self):
        self.b.sets[("ip", "bf_static")].add(A4)
        self.b.sets[("ip", "bf_auto")].add(A4)
        rc, out = call(self.b, f"allow-vm {A4} 1671")
        self.assertEqual((rc, out["result"]), (0, "static"))
        self.assertEqual(self.b.sets[("ip", "cust_allow")], set())

    def test_missing_set_is_an_error(self):
        del self.b.sets[("ip", "cust_allow")]
        self.b.sets[("ip", "bf_auto")].add(A4)
        rc, out = call(self.b, f"allow-vm {A4} 1671")
        self.assertEqual((rc, out["error"]), (1, "internal"))
        self.assertIn("deploy_customer_allow.sh", out["detail"])

    def test_never_touches_bf_auto_bf_allow_or_static(self):
        self.b.sets[("ip", "bf_auto")].add(A4)
        call(self.b, f"allow-vm {A4} 1671 console")
        for cmd, stdin in self.b.calls:
            text = " ".join(cmd) + (stdin or "")
            for name in ("bf_allow", "bf_static"):
                if "delete" in text or "add" in text:
                    self.assertNotIn(name, text)
            self.assertFalse("bf_auto" in text and ("delete" in text or " add " in text), text)


class RevokeVm(unittest.TestCase):
    def setUp(self):
        self.b = FakeBase()

    def test_parse(self):
        self.assertEqual(ub.parse_command("revoke-vm 1671,483,1671"), ("revoke-vm", None, [483, 1671], False))
        for raw in ["revoke-vm", "revoke-vm ", f"revoke-vm {A4}", "revoke-vm 1671 console", "revoke-vm 99",
                    "revoke-vm 1671,", "revoke-vm 01671", "revoke-vm 1671;id", "REVOKE-VM 1671"]:
            with self.assertRaises(ub.BadRequest, msg=raw):
                ub.parse_command(raw)

    def test_removes_every_ip_for_those_vms_both_families(self):
        self.b.sets[("ip", "cust_allow")] |= {(A4, 10483), (A4, 20483), ("8.8.4.4", 30483), (A4, 443),
                                              (A4, 21671), ("8.8.4.4", 20484)}
        self.b.sets[("ip6", "cust_allow")] |= {(A6, 20483), (A6, 21671)}
        rc, out = call(self.b, "revoke-vm 483")
        self.assertEqual((rc, out["result"], out["removed"], out["vmids"]), (0, "revoked", 4, [483]))
        self.assertEqual(self.b.sets[("ip", "cust_allow")], {(A4, 443), (A4, 21671), ("8.8.4.4", 20484)})
        self.assertEqual(self.b.sets[("ip6", "cust_allow")], {(A6, 21671)})
        self.assertNotIn("ip", out)

    def test_nothing_to_remove(self):
        rc, out = call(self.b, "revoke-vm 483")
        self.assertEqual((rc, out["result"], out["removed"]), (0, "revoked", 0))
        self.assertFalse([c for c, _ in self.b.calls if c[1:3] == ["-f", "-"]])

    def test_without_set_is_no_set(self):
        del self.b.sets[("ip", "cust_allow")], self.b.sets[("ip6", "cust_allow")]
        rc, out = call(self.b, "revoke-vm 483")
        self.assertEqual((rc, out["result"]), (0, "no_set"))

    def test_batch_failure_falls_back(self):
        self.b.sets[("ip", "cust_allow")] |= {(A4, 20483), ("8.8.4.4", 30483)}
        self.b.fail_batch = True
        rc, out = call(self.b, "revoke-vm 483")
        self.assertEqual((rc, out["removed"]), (0, 2))
        self.assertEqual(self.b.sets[("ip", "cust_allow")], set())

    def test_never_touches_other_sets(self):
        self.b.sets[("ip", "bf_auto")].add(A4)
        self.b.sets[("ip", "bf_seen")].add((A4, 20483))
        self.b.sets[("ip", "cust_allow")].add((A4, 20483))
        call(self.b, "revoke-vm 483")
        self.assertIn(A4, self.b.sets[("ip", "bf_auto")])
        self.assertIn((A4, 20483), self.b.sets[("ip", "bf_seen")])


class Status(unittest.TestCase):
    def setUp(self):
        self.b = FakeBase()

    def test_reports_blocks_and_allowed_vms_without_writing(self):
        self.b.sets[("ip", "bf_auto")].add(A4)
        for p, exp in ((11671, 500), (21671, 400), (31671, 900), (21670, 800), (443, 300)):
            self.b.sets[("ip", "cust_allow")].add((A4, p))
            self.b.expires[("ip", A4, p)] = exp
        rc, out = call(self.b, f"status {A4}")
        self.assertEqual(rc, 0)
        self.assertEqual((out["blocked"], out["static"], out["customerSet"]), (True, False, True))
        self.assertEqual(out["allowedVmids"], {"1671": 400})       # 1670 no tiene sus 3 puertos
        self.assertEqual(out["consoleExpires"], 300)
        for cmd, stdin in self.b.calls:
            self.assertNotIn(cmd[1], ("delete", "add", "-f"))
            self.assertNotEqual(cmd[0], "conntrack")

    def test_v6_without_static_or_customer_set(self):
        del self.b.sets[("ip6", "cust_allow")]
        rc, out = call(self.b, f"status {A6}")
        self.assertEqual((rc, out["blocked"], out["static"], out["customerSet"]), (0, False, False, False))
        self.assertNotIn("allowedVmids", out)


if __name__ == "__main__":
    unittest.main()
