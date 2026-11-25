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

    def test_guard_runs_for_every_model_before_the_silicon_guard(self):
        online = SCRIPT.index('if [[ -n "$ONLINE_FLAG" ]]; then\n    CPU_RAW=')
        call = SCRIPT.index('_live_cross_vendor_guard "$SRC_CPU_SIG" "$DST_CPU_SIG"')
        silicon = SCRIPT.index('_live_silicon_guard "$SRC_CPU_SIG" "$DST_CPU_SIG"', online)
        self.assertLess(online, call)
        self.assertLess(call, silicon)
        # ...and both before anything is copied or upgraded.
        self.assertLess(silicon, SCRIPT.index('_apt_dist_upgrade src_ssh "source ${SRC_NODE}"'))
        self.assertLess(silicon, SCRIPT.index("'/nodes/${SRC_NODE}/qemu/${VMID}/remote_migrate'"))
        # The old advice was exactly what failed on 10/10.
        self.assertNotIn("or use a baseline cpu model", SCRIPT)


TURIN = "AuthenticAMD|26|2"
RAPHAEL = "AuthenticAMD|25|97"


class SiliconGuard(unittest.TestCase):
    """25/11/2026 (Kademar, EPYC 9755 Turin = AMD 26|2): nothing LIVE across
    silicon. Since the review of PR #291 every cpu model needs identical
    vendor|family|model (LIVE_SILICON_MATCH=model by default); =family relaxes
    generic models to the same family, by hand only."""

    DEFAULT = re.search(r'(?m)^LIVE_SILICON_MATCH="\$\{LIVE_SILICON_MATCH:-\w+\}"$', SCRIPT).group()

    def guard(self, src, dst, model="x86-64-v4", env=None):
        return run_bash(_fn("_live_silicon_guard") + self.DEFAULT + "\n"
                        + f'_live_silicon_guard "{src}" "{dst}" "{model}"\necho PASSED\n', env)

    def assertRefused(self, r, *needles):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("PASSED", r.stdout)
        for n in needles:
            self.assertIn(n, r.stderr)

    def test_genoa_to_turin_refused_for_v4_both_ways(self):
        for src, dst in ((AMD, TURIN), (TURIN, AMD)):
            self.assertRefused(self.guard(src, dst), "EN CALIENTE", "FRÍO", "silicio")

    def test_host_needs_identical_silicon(self):
        self.assertRefused(self.guard(AMD, TURIN, "host"), "silicios distintos", "FRÍO")
        self.assertRefused(self.guard(AMD, RAPHAEL, "host"), "silicios distintos")
        self.assertRefused(self.guard(AMD, RAPHAEL, "max"), "silicios distintos")
        # the old flag-superset "upgrade" path is gone: same family, newer model
        self.assertRefused(self.guard(RAPHAEL, AMD, "HOST"))
        r = self.guard(AMD, AMD, "host")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASSED", r.stdout)

    def test_default_is_model(self):
        self.assertIn('LIVE_SILICON_MATCH:-model', self.DEFAULT)

    def test_genoa_raphael_refused_by_default_even_generic(self):
        # behaviour change (PR #291 review): AX162 Genoa 25|17 <-> AX102 Raphael 25|97
        for model in ("x86-64-v4", "x86-64-v3", "kvm64", "<proxmox default>"):
            self.assertRefused(self.guard(AMD, RAPHAEL, model), "vendor|family|model")
            self.assertRefused(self.guard(RAPHAEL, AMD, model))
        for sig in (AMD, RAPHAEL, TURIN):
            r = self.guard(sig, sig)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_global_relax_switch(self):
        # 2nd review of PR #291: «family» was a global escape without a VMID; gone.
        for v in ("family", "nope"):
            self.assertRefused(self.guard(AMD, RAPHAEL, env={"LIVE_SILICON_MATCH": v}), "solo admite 'model'")
        self.assertNotIn("family)", SCRIPT[SCRIPT.index("_live_silicon_guard() {"):SCRIPT.index("_cold_hwid_guard() {")])

    def test_unreadable_signature_refused(self):
        for src, dst in (("", AMD), (AMD, "garbage"), ("AuthenticAMD||17", AMD),
                         (AMD, "AuthenticAMD|26|"), ("|25|17", AMD)):
            self.assertRefused(self.guard(src, dst), "ilegible")

    def test_lab_override_only_for_its_own_vmid(self):
        r = self.guard(AMD, TURIN, env={"NV_LAB_ALLOW_CROSS_SILICON_LIVE": "777"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LAB OVERRIDE", r.stderr)
        for other in ("1", "", "7777"):
            self.assertRefused(self.guard(AMD, TURIN,
                                          env={"NV_LAB_ALLOW_CROSS_SILICON_LIVE": other}))

    def test_flag_superset_rule_removed(self):
        self.assertNotIn("_node_cpu_flags", SCRIPT)
        self.assertNotIn("Cross-generation UPGRADE", SCRIPT)


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
    open(os.path.join(state, "cmd"), "w").write(cmd[4])
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
if isinstance(step, dict):          # w32tm /resync answer
    assert "w32tm /resync" in open(os.path.join(state, "cmd")).read()
    out = "﻿NV-RUN-%s\r\nRC:%d\r\nOUT:%s\r\nNV-END-%s\r\n" % (nonce, step["rc"], step["out"], nonce)
    print(json.dumps({"exited": 1, "exitcode": 0, "out-data": out})); sys.exit()
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


class GuestTimeResync(unittest.TestCase):
    """w32tm /resync after a move that leaves Windows running (25/11/2026):
    same nonce runner as the boot read, best-effort, never fails."""

    def resync(self, scenario, live=1, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "bin").mkdir()
            qm = t / "bin/qm"
            qm.write_text(FAKE_QM)
            qm.chmod(qm.stat().st_mode | stat.S_IEXEC)
            (t / "scenario.json").write_text(json.dumps(scenario))
            body = (_var_heredoc("_GUEST_BOOT_PY")
                    + 'GUEST_TIME_RESYNC="${GUEST_TIME_RESYNC:-1}" GUEST_TIME_RESYNC_BUDGET_S=20\n'
                    + _fn("_guest_time_resync")
                    + f'fake_ssh() {{ bash -c "$1"; }}\n_guest_time_resync fake_ssh {live}\necho RC=$?\n')
            e = {"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "FAKE_STATE": tmp}
            e.update(env or {})
            r = run_bash(body, e)
            calls = (t / "calls").read_text().splitlines() if (t / "calls").exists() else []
            return r, calls

    def test_success_runs_w32tm_once_with_nonce(self):
        r, calls = self.resync({"steps": ["running", {"rc": 0, "out": "The command completed successfully."}]})
        self.assertIn("RC=0", r.stdout, r.stderr)
        self.assertIn("OK: Reloj del invitado resincronizado", r.stderr)
        self.assertIn("completed successfully", r.stderr)
        self.assertEqual(sum('"exec"' in c for c in calls), 1)
        self.assertNotIn("WARN", r.stderr)

    def test_foreign_result_discarded(self):
        r, calls = self.resync({"steps": ["foreign", {"rc": 0, "out": "ok"}]})
        self.assertIn("OK: Reloj del invitado resincronizado", r.stderr)
        self.assertEqual(sum('"exec"' in c for c in calls), 1)

    def test_w32tm_error_is_a_warning_after_a_live_move(self):
        r, _ = self.resync({"steps": [{"rc": -2147023834, "out": "The service has not been started. (0x80070426)"}]})
        self.assertIn("RC=0", r.stdout)
        self.assertIn("WARN: w32tm /resync en la VM 777 devolvió rc=-2147023834", r.stderr)
        self.assertIn("0x80070426", r.stderr)

    def test_agent_down_after_cold_boot_is_info_only(self):
        r, _ = self.resync({"agent_down": True, "steps": []}, live=0)
        self.assertIn("RC=0", r.stdout)
        self.assertNotIn("WARN", r.stderr)
        self.assertIn("INFO: no se pudo lanzar w32tm /resync", r.stderr)
        self.assertIn("traslado en frío", r.stderr)

    def test_agent_down_after_live_move_warns_never_fails(self):
        r, _ = self.resync({"agent_down": True, "steps": []}, live=1)
        self.assertIn("RC=0", r.stdout)
        self.assertIn("WARN: no se pudo lanzar w32tm /resync", r.stderr)

    def test_switch_off(self):
        r, calls = self.resync({"steps": [{"rc": 0, "out": "x"}]}, env={"GUEST_TIME_RESYNC": "0"})
        self.assertIn("RC=0", r.stdout)
        self.assertEqual(calls, [])

    def test_boot_read_unaffected_by_resync_mode(self):
        self.assertIn('mode = sys.argv[3] if len(sys.argv) > 3 else "boot"', SCRIPT)

    def test_wired_for_running_windows_after_firestore_before_reboot_check(self):
        call = SCRIPT.index('  _guest_time_resync dst_ssh "$( [[ -n "$RESUMED_AT" ]] && echo 1 || echo 0 )"')
        gate = SCRIPT[SCRIPT.rindex("if [[", 0, call):call]
        self.assertIn('"$DST_STATUS" == "running"', gate)
        self.assertIn("win*", gate)
        self.assertLess(SCRIPT.index('_info "Updating Firestore servers/'), call)
        self.assertLess(call, SCRIPT.index("\n  _guest_reboot_postcheck\n"))


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

    def test_exit_86_files_the_event_before_exiting(self):
        exit86 = SCRIPT.index('exit "$EXIT_GUEST_REBOOTED"')
        block = SCRIPT[SCRIPT.index("if (( GUEST_REBOOTED == 1 )); then\n  # Not _die"):exit86]
        self.assertIn("\n  _report_guest_rebooted\n", block)


FAKE_FIREBASE = textwrap.dedent("""\
    import json, os
    _apps = {}
    def initialize_app(cred):
        _apps["x"] = cred
    """)
FAKE_CREDENTIALS = "def Certificate(path):\n    return path\n"
FAKE_FIRESTORE = textwrap.dedent("""\
    import json, os
    SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
    class _Doc:
        def __init__(self, col): self.col = col
        def set(self, data):
            if os.environ.get("FAKE_FS_FAIL"):
                raise RuntimeError("boom")
            with open(os.environ["FAKE_FS_OUT"], "a") as f:
                f.write(json.dumps({"col": self.col, "data": data}) + "\\n")
    class _Col:
        def __init__(self, name): self.name = name
        def document(self): return _Doc(self.name)
    class _Client:
        def collection(self, name): return _Col(name)
    def client(): return _Client()
    """)


class GuestRebootedEvent(unittest.TestCase):
    """The exit-86 path files ONE `migration_guest_rebooted` doc, whoever ran
    the script, so node_liveness can mail soporte@ (11/10/2026)."""

    def _run(self, fail=False):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d, "firebase_admin")
            pkg.mkdir()
            (pkg / "__init__.py").write_text(FAKE_FIREBASE)
            (pkg / "credentials.py").write_text(FAKE_CREDENTIALS)
            (pkg / "firestore.py").write_text(FAKE_FIRESTORE)
            out = Path(d, "out.jsonl")
            env = {"PYTHONPATH": d, "FAKE_FS_OUT": str(out)}
            if fail:
                env["FAKE_FS_FAIL"] = "1"
            r = run_bash(_fn("_report_guest_rebooted")
                         + 'GUEST_REBOOT_DETAIL="boot time A before, B after"\n'
                         + "_report_guest_rebooted\necho RC=$?\n", env)
            rows = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
        return r, rows

    def test_writes_one_doc_with_vmid_nodes_and_detail(self):
        r, rows = self._run()
        self.assertIn("RC=0", r.stdout, r.stderr)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["col"], "migration_guest_rebooted")
        self.assertEqual(rows[0]["data"]["vmid"], 777)
        self.assertEqual(rows[0]["data"]["src"], "0000238-AX162-2-LTD")
        self.assertEqual(rows[0]["data"]["dest"], "0000263-EX131-2-LTD")
        self.assertEqual(rows[0]["data"]["detail"], "boot time A before, B after")
        self.assertEqual(rows[0]["data"]["at"], "SERVER_TIMESTAMP")

    def test_firestore_failure_warns_and_never_aborts(self):
        r, rows = self._run(fail=True)
        self.assertEqual(rows, [])
        self.assertIn("RC=0", r.stdout, r.stderr)   # set -e did not kill the caller
        self.assertIn("WARN: Could not file migration_guest_rebooted", r.stderr)


FAKE_FIRESTORE_NODES = textwrap.dedent("""\
    import json, os
    class _Snap:
        def __init__(self, d): self.exists = d is not None; self._d = d
        def to_dict(self): return self._d
    class _Doc:
        def __init__(self, i): self.i = i
        def get(self):
            if os.environ.get("FAKE_FS_FAIL"):
                raise RuntimeError("boom")
            return _Snap(json.loads(os.environ["FAKE_NODES"]).get(self.i))
    class _Col:
        def __init__(self, name): assert name == "proxmox_nodes", name
        def document(self, i): return _Doc(i)
    class _Client:
        def collection(self, name): return _Col(name)
    def client(): return _Client()
    """)


V4_FLAGS = "fpu sse avx2 avx512f avx512bw avx512cd avx512dq avx512vl"


class ColdAvx512Guard(unittest.TestCase):
    """2nd review of PR #291: a stopped x86-64-v4 VM cannot be sent (remote_migrate --delete)
    to a node without AVX-512: it would not boot there."""

    def guard(self, model, flags):
        return run_bash(_fn("_cold_avx512_guard") + f'_cold_avx512_guard "{model}" "{flags}"\necho PASSED\n')

    def test_v4_needs_avx512(self):
        self.assertEqual(self.guard("x86-64-v4", V4_FLAGS).returncode, 0)
        r = self.guard("x86-64-v4", "fpu sse avx2")
        self.assertEqual(r.returncode, 1); self.assertIn("avx512f", r.stderr)
        r = self.guard("x86-64-v4", "fpu avx512f avx512bw avx512cd avx512dq")
        self.assertEqual(r.returncode, 1); self.assertIn("avx512vl", r.stderr)
        self.assertEqual(self.guard("x86-64-v4", "").returncode, 1)          # unreadable

    def test_other_models_unaffected(self):
        for m in ("x86-64-v3", "host", "kvm64"):
            self.assertEqual(self.guard(m, "fpu sse").returncode, 0, m)

    def test_wired_in_the_offline_branch(self):
        call = SCRIPT.index('_cold_avx512_guard "${CPU_MODEL:-kvm64}"')
        self.assertLess(SCRIPT.index('_cold_hwid_guard "$SRC_CPU_SIG" "$DST_CPU_SIG"'), call)
        self.assertLess(call, SCRIPT.index('_apt_dist_upgrade src_ssh "source ${SRC_NODE}"'))


class BalloonPending(unittest.TestCase):
    """2nd review of PR #291: a run cut between routes/Firestore and the VPS-E balloon fix
    leaves a marker; the retry (SRC_EQ_DST branch) finishes it."""

    def test_mark_unmark(self):
        with tempfile.TemporaryDirectory() as d:
            body = (f'BALLOON_PENDING_DIR="{d}/bp"\nSRC_NODE=0000181-EX44 DST_NODE=0000266-KDM-9755\n'
                    + _fn("_balloon_mark") + _fn("_balloon_unmark")
                    + '_balloon_mark; cat "$BALLOON_PENDING_DIR/777"; _balloon_unmark; ls "$BALLOON_PENDING_DIR"\n')
            r = run_bash(body)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "0000181-EX44 0000266-KDM-9755")

    def test_marker_before_nat_cleared_after_firestore(self):
        mark = SCRIPT.index('if [[ "$_vm_name" == E-* ]]; then _balloon_mark; fi')
        nat = SCRIPT.index('\nNAT_OK=0\n')
        fs = SCRIPT.index('_info "Updating Firestore servers/')
        adapt = SCRIPT.index('if [[ "${_vm_name:-}" == E-* ]]; then\n  if _vpse_balloon_adapt; then _balloon_unmark; fi')
        self.assertLess(mark, nat); self.assertLess(nat, fs); self.assertLess(fs, adapt)

    def test_src_eq_dst_branch_finishes_a_pending_marker(self):
        a = SCRIPT.index("if (( SRC_EQ_DST == 1 )); then")
        block = SCRIPT[a:SCRIPT.index("\n  exit 0\nfi", a)]
        self.assertIn('if [[ -f "$BALLOON_PENDING_DIR/${VMID}" ]]; then', block)
        self.assertIn('"${_bp_dst:-}" == "$RC_DST_NODE"', block)
        self.assertIn("if _vpse_balloon_adapt; then _balloon_unmark; fi", block)
        # the helpers it calls are defined before that branch runs
        for fn in ("_vpse_balloon_adapt() {", "_node_types_load() {", "_mem_class() {", "dst_ssh() {", "_ssh_close() {"):
            self.assertLess(SCRIPT.index(fn), a, fn)


class ColdHwidGuard(unittest.TestCase):
    """PR #291 review: the direct cold path (stopped VM, offline remote_migrate)
    asks for the same HWID-change acknowledgement as the coldmove kit."""

    def guard(self, src, dst, model, env=None):
        return run_bash(_fn("_cold_hwid_guard")
                        + f'_cold_hwid_guard "{src}" "{dst}" "{model}"\necho PASSED\n', env)

    def test_generic_models_need_nothing(self):
        for m in ("x86-64-v4", "x86-64-v3", "X86-64-V4"):
            r = self.guard(AMD, TURIN, m)
            self.assertEqual(r.returncode, 0, (m, r.stderr))
        self.assertEqual(self.guard("", "", "x86-64-v4").returncode, 0)

    def test_host_same_silicon_passes(self):
        self.assertEqual(self.guard(AMD, AMD, "host").returncode, 0)

    def test_host_kvm64_named_to_other_silicon_need_ack_and_notice(self):
        for m in ("host", "kvm64", "EPYC-Genoa", "max"):
            r = self.guard(AMD, TURIN, m)
            self.assertEqual(r.returncode, 1, m)
            self.assertIn("ACK_HWID_CHANGE=777", r.stderr)
        r = self.guard(AMD, TURIN, "host", {"ACK_HWID_CHANGE": "777"})
        self.assertEqual(r.returncode, 1)                     # notice missing
        r = self.guard(AMD, TURIN, "host", {"ACK_HWID_CHANGE": "1", "CPU_NOTICE": "x"})
        self.assertEqual(r.returncode, 1)                     # other vmid
        r = self.guard(AMD, TURIN, "host", {"ACK_HWID_CHANGE": "777", "CPU_NOTICE": "correo 20/11"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("correo 20/11", r.stderr)

    def test_unreadable_signature_refused_for_host(self):
        self.assertEqual(self.guard("", TURIN, "host").returncode, 1)

    def test_wired_in_the_offline_branch_before_anything_is_copied(self):
        call = SCRIPT.index('_cold_hwid_guard "$SRC_CPU_SIG" "$DST_CPU_SIG"')
        live = SCRIPT.index('_live_silicon_guard "$SRC_CPU_SIG" "$DST_CPU_SIG"')
        self.assertLess(live, call)
        self.assertIn("  else\n", SCRIPT[live:call])
        self.assertLess(call, SCRIPT.index('_apt_dist_upgrade src_ssh "source ${SRC_NODE}"'))
        self.assertLess(call, SCRIPT.index("'/nodes/${SRC_NODE}/qemu/${VMID}/remote_migrate'"))


class NodeMemClass(unittest.TestCase):
    """25/11/2026: the VPS-E balloon and the balloon-history copy follow
    proxmox_nodes.nodeType, not the node NAME (0000266-KDM-9755 fell into
    "other": an EX44 E would land with balloon = 60 GB)."""

    def cls(self, node, nt=""):
        return run_bash(_fn("_mem_class") + f'_mem_class "{node}" "{nt}"\n').stdout.strip()

    def test_node_type_decides(self):
        self.assertEqual(self.cls("0000266-KDM-9755", "SQX"), "shared")
        self.assertEqual(self.cls("0000234-AX162-2-LTD", "SQX"), "shared")
        self.assertEqual(self.cls("0000260-AX102-3-LTD", "SQX"), "shared")
        self.assertEqual(self.cls("0000181-EX44", "VPS-E"), "dedicated")
        self.assertEqual(self.cls("0000240-AX102-3-LTD", "MT5"), "other")
        self.assertEqual(self.cls("0000234-AX162-2-LTD", "MT5"), "other")   # nodeType wins over name
        self.assertEqual(self.cls("0000266-KDM-9755", "sqx"), "shared")

    def test_name_fallback_without_node_type(self):
        for nt in ("", "-"):
            self.assertEqual(self.cls("0000266-KDM-9755", nt), "shared")
            self.assertEqual(self.cls("0000235-AX162-2-LTD", nt), "shared")
            self.assertEqual(self.cls("0000091-EX44", nt), "dedicated")
            self.assertEqual(self.cls("0000240-AX102-3-LTD", nt), "other")

    def load(self, nodes, fail=False):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d, "firebase_admin")
            pkg.mkdir()
            (pkg / "__init__.py").write_text(FAKE_FIREBASE)
            (pkg / "credentials.py").write_text(FAKE_CREDENTIALS)
            (pkg / "firestore.py").write_text(FAKE_FIRESTORE_NODES)
            env = {"PYTHONPATH": d, "FAKE_NODES": json.dumps(nodes)}
            if fail:
                env["FAKE_FS_FAIL"] = "1"
            body = ('SRC_NODE=0000181-EX44 DST_NODE=0000266-KDM-9755\n'
                    'NODE_TYPE_SRC=""; NODE_TYPE_DST=""; _NODE_TYPES_LOADED=0\n'
                    + _fn("_node_types_load") + _fn("_mem_class")
                    + '_node_types_load; _node_types_load\n'
                    'echo "SRC=$NODE_TYPE_SRC DST=$NODE_TYPE_DST"\n'
                    'echo "CLS=$(_mem_class "$SRC_NODE" "$NODE_TYPE_SRC")>$(_mem_class "$DST_NODE" "$NODE_TYPE_DST")"\n')
            return run_bash(body, env)

    def test_reads_node_type_from_firestore(self):
        r = self.load({"0000181-EX44": {"nodeType": "VPS-E"},
                       "0000266-KDM-9755": {"nodeType": "SQX", "allowedServerTypes": ["vps-e"]}})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SRC=VPS-E DST=SQX", r.stdout)
        self.assertIn("CLS=dedicated>shared", r.stdout)
        self.assertNotIn("WARN", r.stderr)

    def test_missing_doc_or_field_falls_back_to_the_name_and_warns(self):
        r = self.load({"0000181-EX44": {}})
        self.assertIn("SRC=NO-FIELD DST=NO-DOC", r.stdout)
        self.assertIn("CLS=dedicated>shared", r.stdout)
        self.assertIn("WARN: proxmox_nodes/0000181-EX44 no tiene nodeType", r.stderr)
        self.assertIn("WARN: proxmox_nodes/0000266-KDM-9755 no existe en Firestore", r.stderr)

    def test_firestore_down_warns_once_and_falls_back(self):
        r = self.load({}, fail=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CLS=dedicated>shared", r.stdout)
        self.assertEqual(r.stderr.count("WARN: No pude leer proxmox_nodes.nodeType"), 1)

    def test_vpse_balloon_after_nat_and_firestore(self):
        # PR #291 review: the Firestore read (up to 60 s) must not delay the
        # customer's routes in the "already on dest" branch.
        e = SCRIPT.index('if [[ "${_vm_name:-}" == E-* ]]; then')
        self.assertLess(SCRIPT.index('_info "Updating Firestore servers/'), e)
        self.assertLess(SCRIPT.index('"$SYNC_BASE_NAT" sync "$VMID" "$EXPECTED_VM_IPV6" "node=${DST_NODE}"'), e)
        self.assertLess(e, SCRIPT.index("\n  _guest_reboot_postcheck\n"))

    def test_wiring_no_name_patterns_left(self):
        self.assertNotIn('"$SRC_NODE" == *AX162* && "$DST_NODE" == *AX162*', SCRIPT)
        self.assertNotIn("_fam() {", SCRIPT)
        hist = SCRIPT.index("neuravps-ram-guard.py export-state")
        self.assertIn('_mem_class "$SRC_NODE" "$NODE_TYPE_SRC")" == shared && "$(_mem_class "$DST_NODE" "$NODE_TYPE_DST")" == shared',
                      SCRIPT[hist - 400:hist])
        block = _fn("_vpse_balloon_adapt")
        self.assertIn("_node_types_load", block)
        self.assertIn('if [[ "$dst_fam" == "shared" ]]; then target="$VPSE_BALLOON_MIN_MB"', block)
        self.assertNotIn("--memory", block)          # RAM untouched (Iván 11/11)


class Parses(unittest.TestCase):
    def test_script_parses(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "scripts/migrate_vm.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
