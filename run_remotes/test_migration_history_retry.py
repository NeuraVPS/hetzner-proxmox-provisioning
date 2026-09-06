"""Exercise the shell retry helper with transient and permanent SSH failures."""
import re
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/migrate_vm.sh'
HELPER = re.search(r'(?ms)^_balloon_state_retry\(\) \{\n.*?^\}', SCRIPT.read_text()).group()


class MigrationHistoryRetryTests(unittest.TestCase):
    def run_retry(self, failures):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            mock = path / 'mock-ssh'
            mock.write_text('''#!/bin/bash
n=$(cat "$STATE/calls" 2>/dev/null || echo 0)
echo $((n+1)) > "$STATE/calls"
if (( n < FAILURES )); then
  echo '{"status":"error","error":"lock busy"}'
  exit 1
fi
echo 'valid-state-payload'
''')
            mock.chmod(0o700)
            body = f'''{HELPER}
export STATE={shlex.quote(temp)} FAILURES={failures}
sleep() {{ echo "$1" >> "$STATE/sleeps"; }}
_balloon_state_retry {shlex.quote(str(mock))} 'remote command'
'''
            result = subprocess.run(['bash', '-c', body], capture_output=True, text=True)
            calls = int((path / 'calls').read_text())
            delays = (path / 'sleeps').read_text().splitlines() if (path / 'sleeps').exists() else []
            return result, calls, delays

    def test_transient_busy_lock_recovers_without_leaking_error_payload(self):
        result, calls, delays = self.run_retry(2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'valid-state-payload\n')
        self.assertEqual(calls, 3)
        self.assertEqual(delays, ['5', '5'])

    def test_permanent_failure_is_bounded_and_returns_no_state(self):
        result, calls, delays = self.run_retry(99)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(calls, 12)
        self.assertEqual(delays, ['5'] * 11)

    def test_success_does_not_wait(self):
        result, calls, delays = self.run_retry(0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(delays, [])


if __name__ == '__main__':
    unittest.main()
