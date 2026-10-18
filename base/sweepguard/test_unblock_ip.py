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
                     ("ip6", "bf_seen"): set()}
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
            fam = cmd[4]
            elem = [{"elem": {"val": {"concat": [a, p]}, "expires": 1000}}
                    for a, p in sorted(self.sets[(fam, "bf_seen")])]
            return Done(0, out=json.dumps({"nftables": [{"metainfo": {}},
                                                        {"set": {"name": "bf_seen", "elem": elem}}]}))
        if cmd[1:3] == ["-f", "-"]:
            if self.fail_batch:
                return Done(1, err="Error: Could not process rule: No such file or directory")
            for ln in stdin.splitlines():
                m = re.fullmatch(r"delete element (ip6?) rdpguard bf_seen \{ (\S+) \. (\d+) \}", ln)
                assert m, ln
                self.sets[(m[1], "bf_seen")].discard((m[2], int(m[3])))
            return Done(0)
        verb, fam, name = cmd[1], cmd[3], cmd[5]
        assert cmd[2] == "element" and cmd[4] == "rdpguard", cmd
        key = (fam, name)
        if key not in self.sets:
            return Done(1, err=f"Error: No such file or directory; did you mean set 'x' in table {fam} 'rdpguard'?")
        inner = cmd[6].strip("{} ")
        if name == "bf_seen":
            a, _, p = inner.partition(" . ")
            item = (a, int(p))
        else:
            item = inner
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


if __name__ == "__main__":
    unittest.main()
