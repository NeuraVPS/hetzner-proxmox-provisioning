"""Tests for the pure parsing logic in neuravps-explorer-sweep.py.

The one worth reading first: test_cross_talk_from_concurrent_probe_is_not_trusted.
It is not a hypothetical — it is the literal `qm guest exec` response captured
live on 2026-09-12 while manually validating this sweep against vm1485: a
concurrently-running neuravps-egresscheck.py probe against the SAME VM caused
qemu-ga's exec-status to hand back ITS output instead of ours, on 1 of 3
consecutive calls. Trusting "exitcode 0 + valid JSON" alone would have read
that as "0 orphans found" and silently hidden the 26 that were actually there
(confirmed on immediate retry). Our own `EXPLORERSWEEP:` marker is the only
thing standing between that and a false "clean" VM.

    python3 run_remotes/test_explorer_sweep.py
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "neuravps_explorer_sweep", Path(__file__).with_name("neuravps-explorer-sweep.py"))
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)

# Captured verbatim from vm1048 (real fleet VM) during manual dry-run
# validation, 2026-09-12.
REAL_COUNT_ONLY = (
    '{"exitcode":0,"exited":1,'
    '"out-data":"EXPLORERSWEEP:{\\"ids\\":[3692,4980,6160],\\"found\\":3,'
    '\\"mode\\":\\"count-only\\",\\"killed\\":0}\\r\\n"}'
)
REAL_KILL = (
    '{"exitcode":0,"exited":1,'
    '"out-data":"EXPLORERSWEEP:{\\"mode\\":\\"kill\\",\\"found\\":22,'
    '\\"errors\\":[],\\"killed\\":22}\\r\\n"}'
)
# Verbatim cross-talk: egresscheck's own probe output surfacing on our
# exec-status poll for a DIFFERENT concurrent guest-exec call, same VM.
REAL_CROSS_TALK = (
    '{"exitcode":0,"exited":1,'
    '"out-data":"v4g=200/32768/0.139954/0 v4p=200/1024/0.104349/0 '
    'v6g=200/32768/0.104811/0 v6p=200/1024/0.151716/0 dns=172.66.0.218 '
    'cpu=96\\r\\n"}'
)
GUEST_TIMEOUT_PID_ONLY = '{"pid": 4821}'
NO_AGENT = "ERROR: QEMU guest agent is not running"


class ParseAgentOutput(unittest.TestCase):
    def test_count_only_result_is_trusted(self):
        r = es.parse_agent_output(REAL_COUNT_ONLY)
        self.assertEqual(r, {"ok": True, "found": 3, "killed": 0,
                             "errors": [], "mode": "count-only"})

    def test_kill_result_is_trusted(self):
        r = es.parse_agent_output(REAL_KILL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["found"], 22)
        self.assertEqual(r["killed"], 22)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["mode"], "kill")

    def test_cross_talk_from_concurrent_probe_is_not_trusted(self):
        # THE case this whole marker scheme exists for.
        r = es.parse_agent_output(REAL_CROSS_TALK)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no-marker")

    def test_bare_pid_is_guest_timeout_not_success(self):
        # qm returned only a pid: the guest-side execution status is unknown,
        # so this must never be read as "0 found".
        r = es.parse_agent_output(GUEST_TIMEOUT_PID_ONLY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "guest-timeout")

    def test_agent_not_running_is_a_named_reason(self):
        r = es.parse_agent_output(NO_AGENT)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no-agent")

    def test_garbage_never_raises(self):
        for garbage in ("", "not json at all", "{}", '{"exitcode":1}',
                        '{"exitcode":0,"exited":1,"out-data":"nothing here"}'):
            r = es.parse_agent_output(garbage)
            self.assertFalse(r["ok"])
            self.assertIn("reason", r)

    def test_nonzero_powershell_exitcode_is_not_trusted(self):
        raw = ('{"exitcode":1,"exited":1,'
               '"out-data":"EXPLORERSWEEP:{\\"found\\":0,\\"killed\\":0,'
               '\\"errors\\":[],\\"mode\\":\\"count-only\\"}"}')
        r = es.parse_agent_output(raw)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "ps-exitcode-1")

    def test_errors_from_guest_are_surfaced_and_trusted_result_can_still_be_ok(self):
        # A per-process Stop-Process failure inside the guest (e.g. already
        # exited) is reported, not swallowed, but does not itself invalidate
        # the whole VM's result.
        raw = ('{"exitcode":0,"exited":1,'
               '"out-data":"EXPLORERSWEEP:{\\"found\\":2,\\"killed\\":1,'
               '\\"errors\\":[\\"1234:No process...\\"],'
               '\\"mode\\":\\"kill\\"}"}')
        r = es.parse_agent_output(raw)
        self.assertTrue(r["ok"])
        self.assertEqual(r["found"], 2)
        self.assertEqual(r["killed"], 1)
        self.assertEqual(len(r["errors"]), 1)


class PsPayload(unittest.TestCase):
    def test_dry_and_kill_variants_differ_only_in_doKill(self):
        dry = es._ps_payload(False)
        kill = es._ps_payload(True)
        self.assertNotIn("__DO_KILL__", dry)
        self.assertNotIn("__CLSID__", dry)
        dl, kl = dry.splitlines(), kill.splitlines()
        diffs = [(a, b) for a, b in zip(dl, kl) if a != b]
        self.assertEqual(diffs, [("$doKill = $false", "$doKill = $true")])

    def test_the_three_validated_conditions_are_present_verbatim(self):
        # The filter this sweep is built on (memory/neuravps-explorer-
        # factory-fuga-ram.md) is not to be generalized. Pin its three
        # conditions literally so an "improvement" fails this test loudly.
        ps = es._ps_payload(True)
        self.assertIn("Name='explorer.exe'", ps)
        self.assertIn('/factory,$clsid*', ps)
        self.assertIn("-Embedding*", ps)
        self.assertIn("AddHours(-24)", ps)
        self.assertIn(es.CLSID, ps)

    def test_clsid_is_the_validated_literal(self):
        self.assertEqual(es.CLSID, "{75dff2b7-6936-4c06-a8bb-676a7b00b24b}")


class ResolveIp(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(es.resolve_ip({"n": "2a01::2"}, "n"), "2a01::2")

    def test_dict_with_ip_key(self):
        self.assertEqual(es.resolve_ip({"n": {"ip": "2a01::2"}}, "n"), "2a01::2")

    def test_missing_node(self):
        self.assertIsNone(es.resolve_ip({}, "n"))

    def test_empty_string_is_none(self):
        self.assertIsNone(es.resolve_ip({"n": ""}, "n"))


if __name__ == "__main__":
    unittest.main()
