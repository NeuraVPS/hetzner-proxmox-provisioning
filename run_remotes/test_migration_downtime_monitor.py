"""Drive the real shell escalator through disk, RAM and terminal phases."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/migrate_vm.sh'
HELPER = re.search(r'(?ms)^  _downtime_escalator\(\) \{\n.*?^  \}', SCRIPT.read_text()).group()


def active(dirty):
    return f'Migration status: active\ntransferred ram: 100 kbytes\ndirty sync count: {dirty}\n'


class DowntimeMonitorTests(unittest.TestCase):
    def run_monitor(self, samples, *, clock_step=1, write_reply=''):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'samples').write_text(json.dumps(samples))
            mock = path / 'ssh'
            mock.write_text('''#!/usr/bin/env python3
import os,json,pathlib,sys
p=pathlib.Path(os.environ['STATE'])
command=sys.argv[1]
assert 'pvesh create' in command and '--output-format json' in command
assert '/nodes/source/qemu/123/monitor' in command
if "--command 'info migrate'" in command:
 n=int((p/'calls').read_text()) if (p/'calls').exists() else 0
 (p/'calls').write_text(str(n+1))
 samples=json.loads((p/'samples').read_text())
 print(json.dumps(samples[n] if n<len(samples) else 'Migration status: completed'))
else:
 assert "--command 'migrate_set_parameter downtime-limit " in command
 with (p/'writes').open('a') as f:f.write(command+'\\n')
 print(json.dumps(os.environ['WRITE_REPLY']))
''')
            mock.chmod(0o700)
            body = f'''set -euo pipefail
{HELPER}
export STATE={shlex.quote(temp)} WRITE_REPLY={shlex.quote(write_reply)}
VMID=123 SRC_NODE=source MIGRATE_DOWNTIME_INITIAL=15 MIGRATE_DOWNTIME=90 DOWNTIME_ESCALATE_HARD_S=600
src_ssh() {{ {shlex.quote(str(mock))} "$@"; }}
sleep() {{ :; }}
_info() {{ echo "$*"; }}
date() {{
 local n=0
 [[ ! -f "$STATE/clock" ]] || n=$(cat "$STATE/clock")
 n=$(( n+{clock_step} )); echo "$n" > "$STATE/clock"; echo "$n"
}}
_downtime_escalator
'''
            result = subprocess.run(['bash', '-c', body], capture_output=True, text=True, timeout=10)
            writes = (path/'writes').read_text().splitlines() if (path/'writes').exists() else []
            return result, int((path/'calls').read_text()), writes

    def test_disk_empty_and_stale_status_do_not_disarm_before_ram(self):
        result, calls, writes = self.run_monitor(['', 'Migration status: completed', '', active(1), active(3), 'Migration status: completed'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 6)
        self.assertEqual(len(writes), 1)
        self.assertIn('downtime-limit 30000', writes[0])

    def test_converging_guest_never_escalates(self):
        result, calls, writes = self.run_monitor(['', active(1), 'Migration status: completed'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 3)
        self.assertEqual(writes, [])

    def test_dirty_rounds_raise_budget_in_stages(self):
        result, _, writes = self.run_monitor([active(3), active(5), active(8)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(writes), 3)
        for command, ms in zip(writes, [30000, 60000, 90000]):
            self.assertIn(f'downtime-limit {ms}', command)

    def test_hard_timer_starts_with_ram_not_disk(self):
        result, calls, writes = self.run_monitor(['', '', active(1), active(1)], clock_step=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 4)
        self.assertEqual(len(writes), 1)
        self.assertIn('downtime-limit 90000', writes[0])

    def test_monitor_error_is_not_reported_as_changed_budget(self):
        result, _, writes = self.run_monitor([active(3), 'Migration status: completed'], write_reply='invalid parameter')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(writes), 1)
        self.assertNotIn('budget 15s', result.stdout)


if __name__ == '__main__':
    unittest.main()
