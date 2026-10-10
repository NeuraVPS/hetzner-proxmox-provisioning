"""The defrag never proposes a destination of another CPU vendor (10/10/2026).

migrate_vm.sh refuses every LIVE AMD<->Intel move (an x86-64-v4 Windows guest
bugchecked ~20 s after resuming on Intel), and a cold one changes the SQX
Hardware ID. Today SQX/MT5 are AX* (AMD) only; this keeps an EX* node out even
if it is ever classified into one of those pools."""
import importlib.util
from pathlib import Path
import re
import unittest

PATH = Path(__file__).with_name('neuravps-defrag.py')
spec = importlib.util.spec_from_file_location('defrag_cpu_vendor', PATH)
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)


class CpuVendor(unittest.TestCase):
    def test_vendor_from_product(self):
        self.assertEqual(df.cpu_vendor('0000238-AX162-2-LTD'), 'AMD')
        self.assertEqual(df.cpu_vendor('0000198-AX102-1'), 'AMD')
        self.assertEqual(df.cpu_vendor('0000263-EX131-2-LTD'), 'Intel')
        self.assertEqual(df.cpu_vendor('0000066-EX44'), 'Intel')
        self.assertIsNone(df.cpu_vendor('0000999-SB'))
        self.assertIsNone(df.cpu_vendor('weird'))

    def test_same_vendor_only(self):
        self.assertTrue(df.same_cpu_vendor('0000240-AX162-R', {'0000238-AX162-2-LTD'}))
        self.assertFalse(df.same_cpu_vendor('0000263-EX131-2-LTD', {'0000238-AX162-2-LTD'}))
        self.assertFalse(df.same_cpu_vendor('0000238-AX162-2-LTD', {'0000263-EX131-2-LTD'}))
        self.assertFalse(df.same_cpu_vendor('0000999-SB', {'0000238-AX162-2-LTD'}))
        # relief grows `excluded` with tried AMD destinations: still AMD-only
        self.assertTrue(df.same_cpu_vendor('0000241-AX162-R',
                                           {'0000238-AX162-2-LTD', '0000240-AX162-R'}))

    def test_pick_dest_applies_it(self):
        src = PATH.read_text()
        body = src[src.index('    def pick_dest('):src.index('    _rates_cache = {}')]
        self.assertRegex(body, re.compile(r'if not same_cpu_vendor\(did, exclude\):\n\s+continue'))


if __name__ == '__main__':
    unittest.main()
