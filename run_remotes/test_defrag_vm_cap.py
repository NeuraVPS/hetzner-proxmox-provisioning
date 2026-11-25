"""A node with a hard VM cap is never a defrag/relief destination (25/11/2026).

proxmox_nodes.max_vm on an SQX node is a hard VM-count limit (26 vps-e on
Kademar 0000266). Operator decision after review: such a node is NEVER chosen
as a destination by pick_dest (defrag phases and relief alike) — its entries
are controlled manual moves. It may still be a source. Nodes without the field
(every SQX and MT5 node on 25/11) and VPS-E nodes (max_vm == 1, outside the
defrag) keep their behaviour.

The end-to-end cases run the real main() (and therefore the real pick_dest)
with the fake Firestore / fake SSH harness of test_defrag_mt_ram_gate.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_defrag_mt_ram_gate as h  # noqa: E402  (harness: run_main, df)

df = h.df


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
        self.assertIsNone(df.node_vm_cap({"nodeType": "MT5", "max_vm": 48}))
        self.assertIsNone(df.node_vm_cap({"nodeType": "VPS-E", "max_vm": 1}))
        self.assertIsNone(df.node_vm_cap({"max_vm": 1}))
        self.assertIsNone(df.node_vm_cap(None))

    def test_dest_capped(self):
        self.assertTrue(df.dest_capped({"vm_cap": 26}))
        self.assertFalse(df.dest_capped({"vm_cap": None}))
        self.assertFalse(df.dest_capped({}))


SRC, KDM, AX = "0000300-AX162", "0000266-KDM-9755", "0000301-AX162"


def sqx_store(kdm_cap=26, ax_frozen=False, src_over=True, kdm_over=False):
    """Three SQX nodes, base 100 GB (commit cap 150, op floor cap 90).
    SRC holds 10 vps-a (160 GB > 150: over the gate) when src_over.
    KDM (fuller, the best fit) and AX both have room for a vps-a.
    kdm_over: KDM itself over the gate, AX the only destination."""
    nodes = {
        SRC: {"nodeType": "SQX", "max_base_ram": 100.0, "ip": "n300",
              "location": "Falkenstein"},
        KDM: {"nodeType": "SQX", "max_base_ram": 100.0, "ip": "n266",
              "location": "Falkenstein"},
        AX: {"nodeType": "SQX", "max_base_ram": 100.0, "ip": "n301",
             "location": "Falkenstein", "frozen": ax_frozen},
    }
    if kdm_cap is not None:
        nodes[KDM]["max_vm"] = kdm_cap
    servers, vid = {}, 2000

    def add(nid, n, ram=16, fl=5.0, plan="vps-a"):
        nonlocal vid
        for _ in range(n):
            vid += 1
            servers[f"s{vid}"] = {"nodeId": nid, "proxmoxId": vid, "memoryGb": ram,
                                  "ramFloorGb": fl, "cores": 4, "status": "running",
                                  "serverType": plan}
    add(SRC, 10 if src_over else 5)                 # 160 GB (or 80)
    add(KDM, 10 if kdm_over else 6, ram=16.0)       # 160 (over) or 96 -> ch 54
    add(AX, 4, ram=16.0)                            # 64 -> ch 86
    return {"config": {"defrag": {"enabled": True, "dryRun": False,
                                  "maxMovesPerRun": 40}},
            "proxmox_nodes": nodes, "servers": servers}


def dests(doc):
    return [p.split()[1].split("->")[1] for p in (doc or {}).get("planned") or []]


def srcs(doc):
    return [p.split()[1].split("->")[0] for p in (doc or {}).get("planned") or []]


class PickDestEndToEnd(unittest.TestCase):
    def test_premise_without_cap_the_kademar_box_is_the_best_fit(self):
        _, doc, log = h.run_main(sqx_store(kdm_cap=None))
        self.assertTrue(dests(doc), log)
        self.assertEqual(set(dests(doc)), {"0000266"}, doc and doc["planned"])

    def test_capped_node_never_receives(self):
        _, doc, log = h.run_main(sqx_store())
        self.assertTrue(dests(doc), log)
        self.assertNotIn("0000266", dests(doc))
        self.assertEqual(set(dests(doc)), {"0000301"})

    def test_capped_node_is_not_a_destination_even_with_free_slots(self):
        # 6 VMs on a cap of 26: plenty of slots, still refused.
        _, doc, _ = h.run_main(sqx_store(kdm_cap=26))
        self.assertNotIn("0000266", dests(doc))

    def test_only_the_capped_node_left_means_no_move(self):
        _, doc, log = h.run_main(sqx_store(ax_frozen=True))
        self.assertEqual(dests(doc), [], log)
        self.assertIn("0000300-AX162", log)     # still reported as over-gate

    def test_capped_node_can_be_a_source(self):
        _, doc, log = h.run_main(sqx_store(src_over=False, kdm_over=True))
        self.assertIn("0000266", srcs(doc), log)
        self.assertTrue(dests(doc))
        self.assertNotIn("0000266", dests(doc))

    def test_invalid_cap_behaves_as_uncapped(self):
        _, doc, _ = h.run_main(sqx_store(kdm_cap="x"))
        self.assertEqual(set(dests(doc)), {"0000266"})


if __name__ == "__main__":
    unittest.main()
