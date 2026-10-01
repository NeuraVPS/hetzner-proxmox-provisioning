"""Drive the real shell escalator through disk, RAM and terminal phases."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/migrate_vm.sh'
HELPER = '\n'.join(re.search(r'(?ms)^  %s\(\) \{[^\n]*\n.*?^  \}' % name, SCRIPT.read_text()).group()
                   for name in ('_downtime_ladder', '_downtime_escalator'))


def active(dirty):
    return f'Migration status: active\ntransferred ram: 100 kbytes\ndirty sync count: {dirty}\n'


class DowntimeMonitorTests(unittest.TestCase):
    def run_monitor(self, samples, *, clock_step=1, write_reply='', live=None):
        """`live`: {poll index: ms} — what Proxmox's own loop left in QEMU
        before that poll (it writes its own copy of the limit)."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'samples').write_text(json.dumps(samples))
            (path / 'live_over').write_text(json.dumps({str(k): v for k, v in (live or {}).items()}))
            (path / 'live').write_text('1000')
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
 over=json.loads((p/'live_over').read_text())
 if str(n) in over: (p/'live').write_text(str(over[str(n)]))
 samples=json.loads((p/'samples').read_text())
 print(json.dumps(samples[n] if n<len(samples) else 'Migration status: completed'))
elif "--command 'info migrate_parameters'" in command:
 print(json.dumps('announce-initial: 50 ms\\ndowntime-limit: %s ms\\n' % (p/'live').read_text()))
else:
 assert "--command 'migrate_set_parameter downtime-limit " in command
 with (p/'writes').open('a') as f:f.write(command+'\\n')
 if os.environ['WRITE_REPLY'] == '':
  (p/'live').write_text(command.split('downtime-limit ')[1].split("'")[0])
 print(json.dumps(os.environ['WRITE_REPLY']))
''')
            mock.chmod(0o700)
            body = f'''set -euo pipefail
{HELPER}
export STATE={shlex.quote(temp)} WRITE_REPLY={shlex.quote(write_reply)}
VMID=123 SRC_NODE=source MIGRATE_DOWNTIME_INITIAL=1 MIGRATE_DOWNTIME=90 DOWNTIME_ESCALATE_HARD_S=600 DOWNTIME_ESCALATE_POLL_S=10
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
        self.assertIn('downtime-limit 2000', writes[0])

    def test_converging_guest_never_escalates(self):
        result, calls, writes = self.run_monitor(['', active(1), 'Migration status: completed'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 3)
        self.assertEqual(writes, [])

    def test_dirty_rounds_raise_budget_in_stages(self):
        result, _, writes = self.run_monitor([active(3), active(4), active(5), active(6),
                                              active(8), active(10), active(12), active(14)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(writes), 7)
        for command, ms in zip(writes, [2000, 4000, 8000, 15000, 30000, 60000, 90000]):
            self.assertIn(f'downtime-limit {ms}', command)

    def test_budget_lowered_by_proxmox_is_reasserted(self):
        # Proxmox doubles ITS copy (1 s → 2 s) and writes it over our 8 s.
        result, _, writes = self.run_monitor([active(5), active(5), active(5)], live={1: 2000})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([w.split('downtime-limit ')[1].split("'")[0] for w in writes], ['8000', '8000'])
        self.assertIn('reafirmado a 8000 ms', result.stdout)

    def test_proxmox_raise_is_kept_but_capped_at_ceiling(self):
        # 16 s from Proxmox (above our 2 s step) is respected; 128 s is cut to 90.
        result, _, writes = self.run_monitor([active(3), active(3), active(3)],
                                             live={1: 16000, 2: 128000})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([w.split('downtime-limit ')[1].split("'")[0] for w in writes], ['2000', '16000', '90000'])

    def test_hard_timer_starts_with_ram_not_disk(self):
        result, calls, writes = self.run_monitor(['', '', active(1), active(1)], clock_step=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        # At the ceiling it keeps polling until the migration ends (Proxmox may
        # still try to raise the limit past it), so one more poll than before.
        self.assertEqual(calls, 5)
        self.assertEqual(len(writes), 1)
        self.assertIn('downtime-limit 90000', writes[0])

    def test_monitor_error_is_not_reported_as_changed_budget(self):
        result, _, writes = self.run_monitor([active(3), 'Migration status: completed'], write_reply='invalid parameter')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(writes), 1)
        self.assertNotIn('budget 1s', result.stdout)


if __name__ == '__main__':
    unittest.main()
