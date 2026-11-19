"""restore_vm_from_backup.sh: the config rewrite must map each disk token once.

19/11/2026, lab VM1517: an IN-PLACE restore wrote a config with efidisk0 AND
scsi0 on the same zvol. The usual layout is efidisk0=vm-X-disk-1,
scsi0=vm-X-disk-0, tpmstate0=vm-X-disk-2; streams are numbered in sorted-key
order, so the mapping is disk-1->disk-0, disk-0->disk-1, and chained
str.replace undid the first swap with the second. OVMF then used the Windows
disk as its EFI variable store.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'scripts' / 'restore_vm_from_backup.sh').read_text()
PY = re.search(r"<<'PY'\n(.*?)\nPY\n", SCRIPT, re.S).group(1)

STANDARD = """boot: order=scsi0
digest: 0123456789abcdef0123456789abcdef01234567
efidisk0: local-zfs:vm-1517-disk-1,efitype=4m,size=1M
name: B-lab
net0: virtio=52:54:00:96:eb:58,bridge=vmbr0
scsi0: local-zfs:vm-1517-disk-0,cache=writeback,discard=on,size=136G
smbios1: uuid=11111111-1111-1111-1111-111111111111
tpmstate0: local-zfs:vm-1517-disk-2,size=4M,version=v2.0
vmgenid: 22222222-2222-2222-2222-222222222222
"""


def _rewrite(conf: str, tokens, target: int, regen: str = '0') -> dict:
    with tempfile.TemporaryDirectory() as d:
        c = Path(d) / 'c.conf'
        t = Path(d) / 't.txt'
        p = Path(d) / 'rw.py'
        c.write_text(conf)
        t.write_text('\n'.join(tokens) + '\n')
        p.write_text(PY)
        subprocess.run([sys.executable, str(p), str(c), str(target), '', str(t), regen],
                       check=True)
        out = {}
        for line in c.read_text().splitlines():
            k, v = line.split(':', 1)
            out[k] = v.strip()
        return out


def _vol(value: str) -> str:
    return value.split(',', 1)[0].split(':', 1)[1]


class TestInPlaceStandardLayout(unittest.TestCase):
    # sorted keys: efidisk0, scsi0, tpmstate0 -> streams disk0..2
    TOKENS = ['vm-1517-disk-1', 'vm-1517-disk-0', 'vm-1517-disk-2']

    def test_each_disk_gets_its_own_stream_dataset(self):
        out = _rewrite(STANDARD, self.TOKENS, 1517)
        self.assertEqual(_vol(out['efidisk0']), 'vm-1517-disk-0')
        self.assertEqual(_vol(out['scsi0']), 'vm-1517-disk-1')
        self.assertEqual(_vol(out['tpmstate0']), 'vm-1517-disk-2')

    def test_no_two_disks_share_a_volume(self):
        out = _rewrite(STANDARD, self.TOKENS, 1517)
        vols = [_vol(out[k]) for k in ('efidisk0', 'scsi0', 'tpmstate0')]
        self.assertEqual(len(set(vols)), 3, vols)

    def test_digest_is_dropped_identity_kept(self):
        out = _rewrite(STANDARD, self.TOKENS, 1517)
        self.assertNotIn('digest', out)
        self.assertEqual(out['vmgenid'], '22222222-2222-2222-2222-222222222222')
        self.assertIn('52:54:00:96:eb:58', out['net0'])


class TestNewVm(unittest.TestCase):

    def test_new_vmid_mapping_and_regenerated_ids(self):
        out = _rewrite(STANDARD, ['vm-1517-disk-1', 'vm-1517-disk-0', 'vm-1517-disk-2'],
                       99517, regen='1')
        self.assertEqual(_vol(out['efidisk0']), 'vm-99517-disk-0')
        self.assertEqual(_vol(out['scsi0']), 'vm-99517-disk-1')
        self.assertEqual(_vol(out['tpmstate0']), 'vm-99517-disk-2')
        self.assertNotEqual(out['vmgenid'], '22222222-2222-2222-2222-222222222222')


class TestWholeTokensOnly(unittest.TestCase):

    def test_disk_1_does_not_match_inside_disk_10(self):
        conf = ("scsi0: local-zfs:vm-5-disk-1,size=10G\n"
                "scsi1: local-zfs:vm-5-disk-10,size=10G\n")
        out = _rewrite(conf, ['vm-5-disk-1', 'vm-5-disk-10'], 5)
        self.assertEqual(_vol(out['scsi0']), 'vm-5-disk-0')
        self.assertEqual(_vol(out['scsi1']), 'vm-5-disk-1')


if __name__ == '__main__':
    unittest.main()
