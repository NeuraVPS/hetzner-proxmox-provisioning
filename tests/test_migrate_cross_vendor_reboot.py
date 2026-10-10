"""migrate_vm.sh: no LIVE migration across CPU vendors, and a guest reboot after
a live move is a failure of its own (exit 86), not rc=0 (10/10/2026).

On 2026-10-10 two live AMD→Intel moves of an x86-64-v4 Windows VM bugchecked
(0xA) ~20 s after resume, while the old guard only covered cpu=host and the
script still returned 0 ("reachable" — RDP came back after the reboot).
The shell functions are extracted from the real script and run for real;
`qm` is a fake that records what it is asked."""
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
SCRIPT = (ROOT / "scripts/migrate_vm.sh").read_text()
BATCH = (ROOT / "scripts/migrate_vms_batch.sh").read_text()


def _fn(name):
    m = re.search(r"(?ms)^%s\(\) \{\n.*?^\}\n" % re.escape(name), SCRIPT)
    assert m, name
    return m.group()


def _var_heredoc(name):
    m = re.search(r"(?ms)^%s=\$\(cat <<'PY'\n.*?^PY\n\)\n" % re.escape(name), SCRIPT)
    assert m, name
    return m.group()


PRELUDE = textwrap.dedent("""\
    set -euo pipefail
    _die()  { echo "DIE: $*" >&2; exit 1; }
    _warn() { echo "WARN: $*" >&2; }
    _info() { echo "INFO: $*" >&2; }
    _ok()   { echo "OK: $*" >&2; }
    _log_to_file() { echo "LOG[$1]: $2" >&2; }
    VMID=777 SRC_NODE=0000238-AX162-2-LTD DST_NODE=0000263-EX131-2-LTD
    GUEST_REBOOT_TOLERANCE_S=120
    """)


def run_bash(body, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", PRELUDE + body], capture_output=True,
                          text=True, timeout=60, env=e)


AMD = "AuthenticAMD|25|17"
AMD_ZEN5 = "AuthenticAMD|26|2"
INTEL = "GenuineIntel|6|173"


class CrossVendorGuard(unittest.TestCase):
    def guard(self, src, dst, model="x86-64-v4", env=None):
        return run_bash(_fn("_live_cross_vendor_guard")
                        + f'_live_cross_vendor_guard "{src}" "{dst}" "{model}"\necho PASSED\n',
                        env)

    def test_same_vendor_passes_for_any_model(self):
        for model in ("x86-64-v4", "host", "kvm64"):
            r = self.guard(AMD, AMD_ZEN5, model)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("PASSED", r.stdout)

    def test_amd_to_intel_refused_with_v4(self):
        r = self.guard(AMD, INTEL)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("PASSED", r.stdout)
        self.assertIn("refusing LIVE migration", r.stderr)
        self.assertIn("OFFLINE", r.stderr)
        self.assertNotIn("baseline cpu model", r.stderr)

    def test_intel_to_amd_refused_too(self):
        for model in ("x86-64-v4", "x86-64-v3", "host", "kvm64"):
            r = self.guard(INTEL, AMD, model)
            self.assertEqual(r.returncode, 1, model)
            self.assertIn("vendor mismatch", r.stderr)

    def test_unreadable_signature_refused(self):
        for src, dst in (("", AMD), (AMD, "garbage"), ("|25|17", AMD)):
            r = self.guard(src, dst)
            self.assertEqual(r.returncode, 1, (src, dst))

    def test_lab_override_only_for_its_own_vmid(self):
        r = self.guard(AMD, INTEL, env={"NV_LAB_ALLOW_CROSS_VENDOR_LIVE": "777"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LAB OVERRIDE", r.stderr)
        r = self.guard(AMD, INTEL, env={"NV_LAB_ALLOW_CROSS_VENDOR_LIVE": "1"})
        self.assertEqual(r.returncode, 1)
        r = self.guard(AMD, INTEL, env={"NV_LAB_ALLOW_CROSS_VENDOR_LIVE": ""})
        self.assertEqual(r.returncode, 1)

    def test_guard_runs_for_every_model_before_the_host_branch(self):
        online = SCRIPT.index('if [[ -n "$ONLINE_FLAG" ]]; then\n    CPU_RAW=')
        call = SCRIPT.index('_live_cross_vendor_guard "$SRC_CPU_SIG" "$DST_CPU_SIG"')
        host = SCRIPT.index('if [[ "$_cpu_lc" == "host" || "$_cpu_lc" == "max" ]]; then', online)
        self.assertLess(online, call)
        self.assertLess(call, host)
        # The old advice was exactly what failed on 10/10.
        self.assertNotIn("or use a baseline cpu model", SCRIPT)


FAKE_QM = r'''#!/usr/bin/env python3
"""Fake `qm guest`: exec answers a pid; exec-status replays SCENARIO."""
import json, os, re, sys
state = os.environ["FAKE_STATE"]
scen = json.load(open(os.path.join(state, "scenario.json")))
with open(os.path.join(state, "calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
assert args[0] == "guest", args
if args[1] == "exec":
    if scen.get("agent_down"):
        print("QEMU guest agent is not running"); sys.exit(255)
    assert args[2] == "777" and "--synchronous" in args and args[args.index("--synchronous") + 1] == "0"
    cmd = args[args.index("--") + 1:]
    assert cmd[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"], cmd
    assert "-EncodedCommand" not in cmd
    nonce = re.search(r"NV-RUN-([0-9a-f]{32})", cmd[4]).group(1)
    open(os.path.join(state, "nonce"), "w").write(nonce)
    print(json.dumps({"pid": 4242}, indent=3)); sys.exit()
assert args[1] == "exec-status" and args[2] == "777" and args[3] == "4242", args
nonce = open(os.path.join(state, "nonce")).read()
n_path = os.path.join(state, "n")
n = int(open(n_path).read()) if os.path.exists(n_path) else 0
open(n_path, "w").write(str(n + 1))
steps = scen["steps"]
step = steps[min(n, len(steps) - 1)]
if step == "running":
    print(json.dumps({"exited": 0})); sys.exit()
if step == "foreign":
    print(json.dumps({"exited": 1, "exitcode": 0,
                      "out-data": "Symlink & credential deployment completed successfully.\r\n"})); sys.exit()
boot, now = step
out = "﻿NV-RUN-%s\r\nBOOT:%d\r\nNOW:%d\r\nNV-END-%s\r\n" % (nonce, boot, now, nonce)
print(json.dumps({"exited": 1, "exitcode": 0, "out-data": out}, indent=3))
'''


class GuestBootRead(unittest.TestCase):
    def read(self, scenario):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "bin").mkdir()
            qm = t / "bin/qm"
            qm.write_text(FAKE_QM)
            qm.chmod(qm.stat().st_mode | stat.S_IEXEC)
            (t / "scenario.json").write_text(json.dumps(scenario))
            body = (_var_heredoc("_GUEST_BOOT_PY") + _fn("_guest_boot_read")
                    + 'fake_ssh() { bash -c "$1"; }\n_guest_boot_read fake_ssh 20\n')
            r = run_bash(body, {"PATH": f"{t / 'bin'}:{os.environ['PATH']}",
                                "FAKE_STATE": tmp})
            calls = (t / "calls").read_text().splitlines() if (t / "calls").exists() else []
            return r, calls

    def test_reads_boot_and_now_with_our_nonce(self):
        r, calls = self.read({"steps": ["running", [1791000000, 1791600000]]})
        self.assertEqual(r.returncode, 0, r.stderr)
        f = r.stdout.split()
        self.assertEqual(f[:3], ["OK", "1791000000", "1791600000"])
        self.assertTrue(f[3].isdigit())
        self.assertEqual(sum('"exec"' in c for c in calls), 1)

    def test_foreign_result_discarded_never_relaunched(self):
        r, calls = self.read({"steps": ["foreign", "foreign", [1791000000, 1791600000]]})
        self.assertTrue(r.stdout.startswith("OK 1791000000 "), r.stdout + r.stderr)
        self.assertEqual(sum('"exec"' in c for c in calls), 1)

    def test_three_foreign_results_give_up(self):
        r, calls = self.read({"steps": ["foreign"]})
        self.assertTrue(r.stdout.startswith("FAIL foreign-result"), r.stdout)
        self.assertEqual(sum('"exec"' in c for c in calls), 1)
        self.assertEqual(r.returncode, 0)

    def test_agent_down_is_a_fail_line_not_a_crash(self):
        r, _ = self.read({"agent_down": True, "steps": []})
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("FAIL exec-error"), r.stdout)


class BootChanged(unittest.TestCase):
    def changed(self, pre, post):
        return run_bash(_fn("_guest_boot_changed")
                        + f'_guest_boot_changed "{pre}" "{post}" && echo YES || echo NO\n').stdout.strip()

    def test_verdicts(self):
        self.assertEqual(self.changed(1000, 1000), "NO")
        self.assertEqual(self.changed(1000, 1003), "NO")       # clock nudged after the pause
        self.assertEqual(self.changed(1000, 1120), "NO")
        self.assertEqual(self.changed(1000, 999), "NO")
        self.assertEqual(self.changed(1000, 1121), "YES")
        self.assertEqual(self.changed(1000, 600000), "YES")
        self.assertEqual(self.changed("", 600000), "NO")
        self.assertEqual(self.changed(1000, "x"), "NO")


class RebootPostcheck(unittest.TestCase):
    def post(self, answers, deadline=300, pre="1791000000"):
        """answers: lines _guest_boot_read returns, in order (last repeats)."""
        body = (_fn("_guest_boot_changed") + _fn("_guest_reboot_postcheck") + textwrap.dedent(f"""\
            ANS=$(mktemp); printf '%s\\n' {' '.join(repr(a) for a in answers)} > "$ANS"
            CNT=$(mktemp); echo 0 > "$CNT"
            _guest_boot_read() {{
              local n; n=$(cat "$CNT"); echo $((n+1)) > "$CNT"
              local total; total=$(wc -l < "$ANS")
              (( n < total )) || n=$((total-1))
              sed -n "$((n+1))p" "$ANS"
            }}
            sleep() {{ echo "SLEEP $1" >&2; }}
            dst_ssh() {{ :; }}
            PRE_GUEST_BOOT={pre} RESUMED_AT=$SECONDS RESUMED_EPOCH=1791600000
            GUEST_REBOOTED=0 GUEST_REBOOT_DETAIL=""
            GUEST_REBOOT_SETTLE_S=90 GUEST_REBOOT_DEADLINE_S={deadline}
            _guest_reboot_postcheck
            echo "REBOOTED=$GUEST_REBOOTED READS=$(cat "$CNT")"
            echo "DETAIL=$GUEST_REBOOT_DETAIL"
            """))
        return run_bash(body)

    def test_unchanged_boot_is_success_after_settle_wait(self):
        r = self.post(["OK 1791000002 1791600100 1791600100"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REBOOTED=0 READS=1", r.stdout)
        self.assertRegex(r.stderr, r"SLEEP (8\d|90)\b")      # waited >= 60 s after resume

    def test_reboot_detected_with_detail(self):
        r = self.post(["OK 1791600025 1791600100 1791600100"])
        self.assertIn("REBOOTED=1", r.stdout)
        self.assertIn("booted 25s after the VM was seen running", r.stdout)
        self.assertIn("LOG[ERROR]: GUEST_REBOOTED vm=777", r.stderr)

    def test_silent_agent_retried_then_read(self):
        r = self.post(["FAIL exec-error QEMU guest agent is not running",
                       "FAIL guest-timeout pid=8",
                       "OK 1791600030 1791600200 1791600200"])
        self.assertIn("REBOOTED=1 READS=3", r.stdout)

    def test_agent_never_answers_is_a_warning_only(self):
        r = self.post(["FAIL exec-error QEMU guest agent is not running"], deadline=0)
        self.assertEqual(r.returncode, 0)
        self.assertIn("REBOOTED=0", r.stdout)
        self.assertIn("WARN: Could not re-read Windows' boot time", r.stderr)
        self.assertIn("UNKNOWN", r.stderr)

    def test_no_pre_reading_skips_the_check(self):
        r = self.post(["OK 1791600025 1 1"], pre='""')
        self.assertIn("REBOOTED=0 READS=0", r.stdout)


class ExitCodeWiring(unittest.TestCase):
    def test_exit_86_after_routing_firestore_and_degraded(self):
        self.assertIn("EXIT_GUEST_REBOOTED=86", SCRIPT)
        fs = SCRIPT.index('_info "Updating Firestore servers/')
        check = SCRIPT.index("\n  _guest_reboot_postcheck\n")
        degraded = SCRIPT.index("if (( MIGRATION_DEGRADED == 1 )); then\n  # MIGRATION_DONE is already 1")
        exit86 = SCRIPT.index('exit "$EXIT_GUEST_REBOOTED"')
        done = SCRIPT.index('_ok "Migration complete:')
        self.assertLess(fs, check)
        self.assertLess(check, degraded)
        self.assertLess(degraded, exit86)
        self.assertLess(exit86, done)
        block = SCRIPT[SCRIPT.index("if (( GUEST_REBOOTED == 1 )); then\n  # Not _die"):exit86]
        self.assertIn("la VM ya está en el destino", block)
        self.assertIn("Windows se reinició", block)

    def test_pre_reading_is_live_windows_only_and_before_remote_migrate(self):
        pre = SCRIPT.index("_boot_res=$(_guest_boot_read src_ssh 45)")
        self.assertLess(pre, SCRIPT.index("'/nodes/${SRC_NODE}/qemu/${VMID}/remote_migrate'"))
        gate = SCRIPT[SCRIPT.rindex("if [[", 0, pre):pre]
        self.assertIn('-n "$ONLINE_FLAG"', SCRIPT[SCRIPT.rindex("# 4c)", 0, pre):pre])
        self.assertIn("win*", gate)
        self.assertIn('RESUMED_AT=$SECONDS', SCRIPT)

    def test_resume_mark_set_only_once_running_on_dest(self):
        i = SCRIPT.index('RESUMED_AT=$SECONDS; RESUMED_EPOCH=$(date +%s)')
        self.assertIn('if _ensure_running_dst "$POST_MIGRATE_RUN_TIMEOUT"; then',
                      SCRIPT[i - 120:i])

    def test_batch_labels_rc_86_without_breaking_the_fail_line(self):
        self.assertIn('(( jrc == 86 )) && jnote=" note=guest-rebooted-vm-on-dest"', BATCH)
        line = 'FAIL     vmid=12 src=a dst=b rc=86 log=/x note=guest-rebooted-vm-on-dest'
        tok = line.split()
        self.assertEqual(tok[0], "FAIL")
        self.assertEqual(re.findall(r"FAIL\s+vmid=(\d+)", line), ["12"])

    def test_script_parses(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "scripts/migrate_vm.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
