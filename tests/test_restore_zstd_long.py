"""Every Storage Box reader must decompress with a long-range window.

Since 19/11/2026 backups are written with `zstd -3 --long=30|31`. A frame like
that declares a 1-2 GiB window and plain `zstd -d` refuses it ("Frame requires
too much memory for decoding", rc 1), so a restore script without `--long=31`
cannot restore any new backup. The flag is harmless for older streams.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestZstdReadersUseLongWindow(unittest.TestCase):

    def test_no_bare_zstd_decompress_in_scripts(self):
        offenders = []
        for path in sorted(ROOT.rglob('*.sh')):
            if 'worktrees' in path.parts or '.git' in path.parts:
                continue
            for n, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                if re.search(r'\bzstd\b[^|]*\s-d\b', line) and '--long=31' not in line:
                    offenders.append(f'{path.relative_to(ROOT)}:{n}: {line.strip()}')
        self.assertEqual(offenders, [])

    def test_backup_restore_script_decompresses_with_long_window(self):
        text = (ROOT / 'scripts' / 'restore_vm_from_backup.sh').read_text()
        self.assertIn('| zstd -d --long=31 \\', text)


if __name__ == '__main__':
    unittest.main()
