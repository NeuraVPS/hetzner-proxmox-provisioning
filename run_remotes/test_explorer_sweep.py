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
        # REAL_COUNT_ONLY predates the commit sample (captured 2026-09-12) —
        # pinning that an old-shape payload still parses cleanly, with the
        # three new fields defaulting to None, is the backward-compat
        # guarantee: a rollout that mixes old/new guest responses for a
        # moment must never look like a parse failure.
        r = es.parse_agent_output(REAL_COUNT_ONLY)
        self.assertEqual(r, {"ok": True, "found": 3, "killed": 0,
                             "errors": [], "mode": "count-only",
                             "commitPct": None, "commitChargeMb": None,
                             "commitLimitMb": None})

    def test_commit_sample_is_parsed_when_present(self):
        raw = ('{"exitcode":0,"exited":1,'
               '"out-data":"EXPLORERSWEEP:{\\"found\\":0,\\"killed\\":0,'
               '\\"errors\\":[],\\"mode\\":\\"count-only\\",'
               '\\"commitPct\\":96.4,\\"commitChargeMb\\":3948,'
               '\\"commitLimitMb\\":4096}\\r\\n"}')
        r = es.parse_agent_output(raw)
        self.assertTrue(r["ok"])
        self.assertEqual(r["commitPct"], 96.4)
        self.assertEqual(r["commitChargeMb"], 3948)
        self.assertEqual(r["commitLimitMb"], 4096)

    def test_commit_sample_null_from_guest_side_wmi_failure(self):
        # The guest-side try/catch sets these to $null on a WMI failure —
        # must still be a fully trusted, ok=True result.
        raw = ('{"exitcode":0,"exited":1,'
               '"out-data":"EXPLORERSWEEP:{\\"found\\":1,\\"killed\\":0,'
               '\\"errors\\":[],\\"mode\\":\\"count-only\\",'
               '\\"commitPct\\":null,\\"commitChargeMb\\":null,'
               '\\"commitLimitMb\\":null}\\r\\n"}')
        r = es.parse_agent_output(raw)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["commitPct"])

    def test_garbage_commit_fields_never_raise_or_pass_through(self):
        raw = ('{"exitcode":0,"exited":1,'
               '"out-data":"EXPLORERSWEEP:{\\"found\\":0,\\"killed\\":0,'
               '\\"errors\\":[],\\"mode\\":\\"count-only\\",'
               '\\"commitPct\\":\\"not-a-number\\"}\\r\\n"}')
        r = es.parse_agent_output(raw)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["commitPct"])

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


class CommitSampleHelpers(unittest.TestCase):
    def test_as_number_passes_through_numbers(self):
        self.assertEqual(es._as_number(96.4), 96.4)
        self.assertEqual(es._as_number(4096), 4096)

    def test_as_number_rejects_non_numbers_and_bools(self):
        # bool is a subclass of int in Python — explicitly excluded so a
        # stray True/False in the JSON never gets treated as commitPct=1/0.
        for v in (None, "x", [], {}, True, False):
            with self.subTest(v=v):
                self.assertIsNone(es._as_number(v))

    def test_no_process_reason_matches_the_three_named_reasons(self):
        self.assertTrue(es._is_no_process_reason("guest-timeout"))
        self.assertTrue(es._is_no_process_reason("no-marker"))
        self.assertTrue(es._is_no_process_reason("ps-exitcode-1"))
        self.assertTrue(es._is_no_process_reason("ps-exitcode-3221225620"))

    def test_no_process_reason_excludes_connectivity_failures(self):
        # These are a DIFFERENT failure class (SSH/agent down, not commit
        # exhaustion) and must not pollute the /admin/salud card.
        for reason in ("no-agent", "unresolved-node", "ssh/TimeoutExpired",
                       "bad-json:garbage", "unparseable-payload"):
            with self.subTest(reason=reason):
                self.assertFalse(es._is_no_process_reason(reason))

    def test_high_commit_threshold_is_90_percent(self):
        self.assertEqual(es.HIGH_COMMIT_PCT, 90.0)


class ParseArgs(unittest.TestCase):
    def test_defaults(self):
        args = es.parse_args([])
        self.assertIsNone(args.only_vmids)
        self.assertFalse(args.force_dry_run)
        self.assertFalse(args.no_journal)

    def test_manual_test_flags(self):
        args = es.parse_args(
            ["--only-vmids", "215,701", "--force-dry-run", "--no-journal"])
        self.assertEqual(args.only_vmids, "215,701")
        self.assertTrue(args.force_dry_run)
        self.assertTrue(args.no_journal)


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

    def test_commit_sample_is_present_and_wrapped_in_its_own_try_catch(self):
        # The commit sample must never be able to stop the marker line from
        # being written — a guest-side WMI failure has to degrade to nulls,
        # not to a missing EXPLORERSWEEP: marker (which would silently
        # cancel this VM's kill for the day, per the module docstring).
        ps = es._ps_payload(True)
        self.assertIn("Win32_OperatingSystem", ps)
        self.assertIn("TotalVirtualMemorySize", ps)
        self.assertIn("FreeVirtualMemory", ps)
        self.assertIn("commitPct = $commitPct", ps)
        self.assertIn("try {", ps)
        self.assertIn("} catch { }", ps)


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
