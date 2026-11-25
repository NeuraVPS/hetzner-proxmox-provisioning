"""Hard VM cap of an SQX node as a defrag destination (Kademar 0000266, 25/11).

proxmox_nodes.max_vm on an SQX node is a hard VM-count limit (26 vps-e on
the Kademar box, raised by editing the field). Placement honours it in
NeuraVPS auto_provision.sqx_vm_cap; the defrag must not push a node past it
either. Nodes without the field (every SQX and MT5 node on 25/11) and VPS-E
nodes (max_vm == 1, never part of the defrag) keep their behaviour."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).with_name('neuravps-defrag.py')
spec = importlib.util.spec_from_file_location('defrag_vm_cap', PATH)
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)


class NodeVmCap(unittest.TestCase):
    def test_sqx_with_max_vm(self):
        self.assertEqual(df.node_vm_cap({"nodeType": "SQX", "max_vm": 26}), 26)
        self.assertEqual(df.node_vm_cap({"nodeType": "SQX", "max_vm": "28"}), 28)

    def test_no_cap_when_absent_or_invalid(self):
        for d in ({"nodeType": "SQX"}, {"nodeType": "SQX", "max_vm": 0},
                  {"nodeType": "SQX", "max_vm": None},
                  {"nodeType": "SQX", "max_vm": "x"},
                  {"nodeType": "SQX", "max_vm": -3}):
            self.assertIsNone(df.node_vm_cap(d), d)

    def test_only_sqx_nodes(self):
        # MT5 legacy max_vm and dedicated VPS-E (max_vm 1) are not touched.
        self.assertIsNone(df.node_vm_cap({"nodeType": "MT5", "max_vm": 48}))
        self.assertIsNone(df.node_vm_cap({"nodeType": "VPS-E", "max_vm": 1}))
        self.assertIsNone(df.node_vm_cap({"max_vm": 1}))
        self.assertIsNone(df.node_vm_cap(None))


class DestVmSlot(unittest.TestCase):
    def test_uncapped_always_ok(self):
        self.assertTrue(df.dest_vm_slot_ok({"vm_cap": None, "n_cap": 500}))
        self.assertTrue(df.dest_vm_slot_ok({}))

    def test_capped(self):
        self.assertTrue(df.dest_vm_slot_ok({"vm_cap": 26, "n_cap": 25}))
        self.assertFalse(df.dest_vm_slot_ok({"vm_cap": 26, "n_cap": 26}))
        self.assertFalse(df.dest_vm_slot_ok({"vm_cap": 26, "n_cap": 27}))


class Wiring(unittest.TestCase):
    """pick_dest and book live inside main(); check the wiring in the source."""

    def setUp(self):
        src = PATH.read_text()
        self.main = src[src.index('def main():'):]

    def test_pick_dest_checks_the_slot(self):
        body = self.main[self.main.index('def pick_dest('):]
        body = body[:body.index('\n    def ', 10)]
        self.assertIn('if not dest_vm_slot_ok(dn):', body)

    def test_node_state_and_booking_track_the_count(self):
        self.assertIn('"vm_cap": node_vm_cap(d), "n_cap": 0', self.main)
        self.assertIn('if d.get("provisioningStatus") != "failed":\n'
                      '            n["n_cap"] += 1', self.main)
        book = self.main[self.main.index('def book('):]
        book = book[:book.index('\n    # ')]
        self.assertIn('sn["n_cap"] -= 1', book)
        self.assertIn('dn["n_cap"] += 1', book)

    def test_consolidation_rollback_restores_the_count(self):
        self.assertIn('v["n_cap"]) for k, v in nodes.items()}', self.main)
        self.assertIn('nodes[k]["n_cap"] = nc', self.main)


if __name__ == '__main__':
    unittest.main()
