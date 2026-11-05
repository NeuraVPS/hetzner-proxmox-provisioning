"""MT RAM gate in the defrag (v14, approved by Iván 2026-11-05).

The MT5 correction only looked at cores: an AX102 MT at 96/96 cores with 123
of its 125 GB reserved as balloon floors was never touched, while other AX102
MT nodes sat half empty. Now:
  * origin when floors > mtRamGatePct (0.85) x gbRam (physical RAM);
  * destination only if it stays <= mtRamDestMaxPct (0.80) x gbRam and
    <= max_cores after the move, plus every existing rule (nodeType, CPU...);
  * the smallest RUNNING VM that gets the node under the gate on its own,
    else the largest; worst node first; the run's move cap still rules;
  * every other MT move refuses to push a destination over the gate;
  * SQX and the MT cores gate are unchanged.
"""
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PATH = Path(__file__).with_name('neuravps-defrag.py')
spec = importlib.util.spec_from_file_location('defrag_mt_ram', PATH)
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)

RYZEN = "SIG|AuthenticAMD|25|97|2|AMD Ryzen 9 7950X3D 16-Core Processor|1"


def node(fl, phys=125.0, cap=96, cores=0, frozen=False, status="", model="MT5"):
    return {"model": model, "fl": fl, "phys": phys, "cap": cap, "cores": cores,
            "frozen": frozen, "status": status, "vms": []}


class Settings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(df.mt_ram_settings({}), (0.85, 0.80))
        self.assertEqual(df.mt_ram_settings(None), (0.85, 0.80))

    def test_configurable(self):
        self.assertEqual(df.mt_ram_settings({"mtRamGatePct": 0.9, "mtRamDestMaxPct": 0.82}),
                         (0.9, 0.82))

    def test_disabled_or_malformed_turns_it_off(self):
        for cfg in ({"mtRamGateEnabled": False}, {"mtRamGateEnabled": "no"},
                    {"mtRamGatePct": "x"}, {"mtRamGatePct": 0.8, "mtRamDestMaxPct": 0.85},
                    {"mtRamGatePct": 1.2}, {"mtRamDestMaxPct": 0}):
            self.assertIsNone(df.mt_ram_settings(cfg), cfg)


class Arithmetic(unittest.TestCase):
    def test_gate_uses_physical_ram(self):
        n = node(fl=110.0, phys=125.0)
        self.assertAlmostEqual(df.mt_ram_excess(n, 0.85), 110 - 106.25)
        self.assertLessEqual(df.mt_ram_excess(node(fl=106.0), 0.85), 0)

    def test_unknown_physical_ram_is_never_origin_nor_destination(self):
        n = node(fl=500.0, phys=0)
        self.assertEqual(df.mt_ram_excess(n, 0.85), 0.0)
        self.assertIsNone(df.mt_ram_pct(n))
        self.assertFalse(df.mt_ram_dest_ok(node(fl=0, phys=0), 2.0, 0.80))

    def test_dest_cap(self):
        self.assertTrue(df.mt_ram_dest_ok(node(fl=96.0), 4.0, 0.80))    # 100 == 0.80*125
        self.assertFalse(df.mt_ram_dest_ok(node(fl=96.5), 4.0, 0.80))
        self.assertTrue(df.mt_ram_dest_ok(node(fl=124.0), 4.0, None))   # gate off

    def test_victim_order_smallest_sufficient_then_largest(self):
        vms = [{"vmid": 1, "fl": 2.0}, {"vmid": 2, "fl": 4.0}, {"vmid": 3, "fl": 6.0},
               {"vmid": 4, "fl": 3.0}]
        key = df.mt_ram_victim_key(3.5)
        self.assertEqual([v["vmid"] for v in sorted(vms, key=key)], [2, 3, 4, 1])
        key = df.mt_ram_victim_key(10.0)       # nobody suffices alone: largest first
        self.assertEqual([v["vmid"] for v in sorted(vms, key=key)], [3, 2, 4, 1])


def run_plan(nodes, room=8, dest_cap=0.80, gate=0.85):
    """plan_mt_ram_gate with a pick_dest that applies the same RAM + core rule
    as main(): best-fit by RAM headroom under dest_cap."""
    moves = []

    def book(vm, src, dst, reason):
        for k in ("fl", "cores"):
            nodes[src][k] -= vm[k]
            nodes[dst][k] += vm[k]
        nodes[src]["vms"] = [v for v in nodes[src]["vms"] if v["vmid"] != vm["vmid"]]
        moves.append((vm["vmid"], src, dst))

    def victims_of(nid, excess):
        return sorted([v for v in nodes[nid]["vms"] if v.get("st", "running") == "running"],
                      key=df.mt_ram_victim_key(excess))

    def pick_dest(vm):
        best = None
        for did, dn in nodes.items():
            if did == vm["node"] or dn["model"] != "MT5" or dn["frozen"] or dn["status"]:
                continue
            if dn["cap"] - dn["cores"] - vm["cores"] < 0:
                continue
            if not df.mt_ram_dest_ok(dn, vm["fl"], dest_cap):
                continue
            key = dest_cap * dn["phys"] - dn["fl"] - vm["fl"]
            if best is None or key < best[1]:
                best = (did, key)
        return best[0] if best else None

    report = df.plan_mt_ram_gate(nodes, gate, victims_of, pick_dest, book,
                                 lambda: room - len(moves))
    return moves, report


def vm(vmid, fl, nid, cores=2, st="running"):
    return {"vmid": vmid, "fl": fl, "ram": fl * 2, "cores": cores, "node": nid, "st": st}


class Planner(unittest.TestCase):
    def test_full_node_drains_to_below_gate_into_nodes_that_stay_under_80(self):
        nodes = {"hot": node(fl=110.0, cores=96), "a": node(fl=60.0, cores=40),
                 "b": node(fl=98.0, cores=50)}
        nodes["hot"]["vms"] = [vm(1, 2.0, "hot"), vm(2, 4.0, "hot"), vm(3, 2.0, "hot")]
        moves, report = run_plan(nodes)
        # excess 3.75 GB -> the smallest VM that suffices alone is the 4 GB one
        self.assertEqual(moves, [(2, "hot", "a")])          # b would end at 102 > 100
        self.assertEqual(report["hot"]["outcome"], "under")
        self.assertLessEqual(nodes["hot"]["fl"], 0.85 * 125)
        self.assertLessEqual(nodes["a"]["fl"], 0.80 * 125)

    def test_needs_several_moves_largest_first(self):
        nodes = {"hot": node(fl=120.0, cores=96), "a": node(fl=40.0, cores=40)}
        nodes["hot"]["vms"] = [vm(i, 4.0 if i < 4 else 2.0, "hot") for i in range(1, 9)]
        moves, report = run_plan(nodes)
        self.assertEqual([m[0] for m in moves[:3]], [1, 2, 3])   # the 4 GB ones first
        self.assertEqual(report["hot"]["outcome"], "under")
        self.assertLessEqual(nodes["hot"]["fl"], 0.85 * 125)

    def test_move_cap_is_respected_and_worst_node_goes_first(self):
        nodes = {"n1": node(fl=110.0, cores=96), "n2": node(fl=124.0, cores=96),
                 "dst": node(fl=10.0, cores=10)}
        for nid in ("n1", "n2"):
            nodes[nid]["vms"] = [vm(100 * len(nid) + i + (50 if nid == "n2" else 0), 2.0, nid)
                                 for i in range(10)]
        moves, report = run_plan(nodes, room=3)
        self.assertEqual(len(moves), 3)
        self.assertTrue(all(m[1] == "n2" for m in moves))
        self.assertEqual(report["n2"]["outcome"], "partial")
        self.assertEqual(report["n1"]["outcome"], "rate-limited")

    def test_no_destination_under_80_means_no_move(self):
        nodes = {"hot": node(fl=110.0, cores=96), "a": node(fl=99.0, cores=10)}
        nodes["hot"]["vms"] = [vm(1, 4.0, "hot")]
        moves, report = run_plan(nodes)
        self.assertEqual(moves, [])
        self.assertEqual(report["hot"]["outcome"], "no-dest")

    def test_destination_must_keep_cores_within_max_cores(self):
        nodes = {"hot": node(fl=110.0, cores=96), "a": node(fl=40.0, cores=95)}
        nodes["hot"]["vms"] = [vm(1, 4.0, "hot", cores=2)]
        moves, _ = run_plan(nodes)
        self.assertEqual(moves, [])

    def test_stopped_vms_and_frozen_nodes_are_left_alone(self):
        nodes = {"hot": node(fl=110.0, cores=96), "cold": node(fl=120.0, frozen=True),
                 "a": node(fl=40.0, cores=40)}
        nodes["hot"]["vms"] = [vm(1, 4.0, "hot", st="stopped")]
        moves, report = run_plan(nodes)
        self.assertEqual(moves, [])
        self.assertEqual(report["hot"]["outcome"], "no-victim")
        self.assertEqual(report["cold"]["outcome"], "not-placeable")

    def test_under_gate_and_sqx_nodes_are_not_origins(self):
        nodes = {"ok": node(fl=106.0), "sqx": node(fl=500.0, model="SQX"),
                 "a": node(fl=10.0)}
        moves, report = run_plan(nodes)
        self.assertEqual((moves, report), ([], {}))


# ---------------------------------------------------------------------------
# main() end to end, with a fake Firestore and fake SSH: proves the wiring
# (phase order, pick_dest RAM cap, region, move cap, run doc, dry-run).
# ---------------------------------------------------------------------------
class _Snap:
    def __init__(self, id_, data):
        self.id, self._d = id_, data
        self.exists = data is not None

    def to_dict(self):
        return None if self._d is None else dict(self._d)


class _Doc:
    def __init__(self, store, coll, id_):
        self.store, self.coll, self.id = store, coll, id_

    def get(self):
        return _Snap(self.id, self.store.setdefault(self.coll, {}).get(self.id))

    def set(self, data, merge=False):
        c = self.store.setdefault(self.coll, {})
        c[self.id] = {**(c.get(self.id) or {}), **data} if merge else dict(data)

    def update(self, data):
        self.store.setdefault(self.coll, {}).setdefault(self.id, {}).update(data)


class _Coll:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def document(self, id_):
        return _Doc(self.store, self.name, id_)

    def stream(self):
        return [_Snap(k, v) for k, v in list(self.store.get(self.name, {}).items())]


class FakeDb:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return _Coll(self.store, name)


def fake_run_factory(vm_cpus):
    """subprocess.run stand-in: no manual migration, CPU probe = 7950X3D with
    the given per-node VM cpu lines, every guest agent alive, no rates."""
    def fake_run(cmd, *a, **kw):
        out = ""
        if cmd[0] == "ps":
            out = "  PID ARGS\n"
        elif cmd[0] == "ssh":
            remote = cmd[-1]
            host = next((c for c in cmd if c.startswith("root@")), "root@")[5:]
            if "python3 -c" in remote:
                out = RYZEN + "\n" + "".join(
                    f"VM|{v}|x86-64-v4|x86-64-v4\n" for v in vm_cpus.get(host, ()))
            elif "qm agent" in remote:
                out = "RC=0\n"
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)
    return fake_run


def run_main(store, argv=("--dry-run",)):
    tmp = tempfile.mkdtemp()
    real_open = open

    def fake_open(path, *a, **kw):
        if isinstance(path, str) and path.startswith(("/run/", "/var/lib/", "/root/")):
            path = os.path.join(tmp, path.strip("/").replace("/", "_"))
        return real_open(path, *a, **kw)

    vm_cpus = {}
    for s in store["servers"].values():
        ip = store["proxmox_nodes"][s["nodeId"]]["ip"]
        vm_cpus.setdefault(ip, []).append(s["proxmoxId"])
    log = io.StringIO()
    with mock.patch.object(df, "open", fake_open, create=True), \
         mock.patch.object(df.subprocess, "run", fake_run_factory(vm_cpus)), \
         mock.patch.object(df.firebase_admin, "initialize_app"), \
         mock.patch.object(df.credentials, "Certificate"), \
         mock.patch.object(df.firestore, "client", lambda: FakeDb(store)), \
         mock.patch.object(df, "RECENT_FILE", os.path.join(tmp, "recent.json")), \
         mock.patch.object(sys, "argv", ["neuravps-defrag.py", *argv]), \
         mock.patch("sys.stdout", log):
        rc = df.main()
    runs = store.get("defrag_runs", {})
    return rc, (list(runs.values())[-1] if runs else None), log.getvalue()


def fleet(cfg=None, extra_nodes=None):
    nodes = {
        "0000051-AX102": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 110, "ip": "n51",
                          "location": "Helsinki"},
        "0000247-AX102-3-LTD": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 96,
                                "ip": "n247", "location": "Helsinki"},
        "0000047-AX102": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 102, "ip": "n47",
                          "location": "Helsinki"},
        "0000262-AX102-3-LTD": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 96,
                                "ip": "n262", "location": "Helsinki"},
    }
    nodes.update(extra_nodes or {})
    servers, vid = {}, 1000

    def add(nid, n, fl, plan, cores=2, status="running"):
        nonlocal vid
        for _ in range(n):
            vid += 1
            servers[f"s{vid}"] = {"nodeId": nid, "proxmoxId": vid, "memoryGb": fl * 2,
                                  "ramFloorGb": fl, "cores": cores, "status": status,
                                  "serverType": plan}
    add("0000051-AX102", 20, 4.0, "mt-plus")       # 80 GB
    add("0000051-AX102", 22, 2.0, "mt")            # +44 = 124 GB = 99 %, 84/110 cores
    add("0000247-AX102-3-LTD", 30, 2.0, "mt")      # 60 GB = 48 %
    add("0000047-AX102", 24, 4.0, "mt-plus")       # 96 GB = 77 %
    return {"config": {"defrag": {"enabled": True, "dryRun": False, "maxMovesPerRun": 40,
                                  **(cfg or {})}},
            "proxmox_nodes": nodes, "servers": servers}


class MainEndToEnd(unittest.TestCase):
    def test_dry_run_plans_ram_gate_moves_within_the_routine_cap(self):
        store = fleet()
        rc, doc, log = run_main(store)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["status"], "dry-run")
        planned = doc["planned"]
        # routineMaxMovesPerRun default 4; all from 0000051, the 4 GB guests first.
        # Best-fit by RAM headroom: 0000047 (96 GB) takes one 4 GB guest and
        # lands exactly on 80 % (100/125); then it is full and 0000247 gets the rest.
        self.assertEqual(len(planned), 4, log)
        self.assertTrue(all("mt-ram-gate" in p for p in planned), planned)
        self.assertEqual([p.split()[1] for p in planned],
                         ["0000051->0000047"] + ["0000051->0000247"] * 3)
        rep = doc["mtRamGate"]["0000051-AX102"]
        self.assertEqual(rep["moves"], 4)
        self.assertEqual(rep["outcome"], "partial")
        self.assertAlmostEqual(rep["after"], (124 - 16) / 125, places=3)
        self.assertNotIn("0000262", " ".join(planned))   # empty node = reserve
        self.assertEqual(store["servers"]["s1001"]["nodeId"], "0000051-AX102")  # dry: no writes

    def test_best_fit_fills_the_fullest_destination_only_up_to_80(self):
        store = fleet({"routineMaxMovesPerRun": 10})
        _, doc, log = run_main(store)
        dsts = [p.split()[1].split("->")[1] for p in doc["planned"]]
        # 0000047 (96 GB) takes exactly one 4 GB guest (-> 100 GB = 80 %), the rest go to 247
        self.assertEqual(dsts.count("0000047"), 1, doc["planned"])
        self.assertEqual(doc["mtRamGate"]["0000051-AX102"]["outcome"], "under")
        self.assertLessEqual(doc["mtRamGate"]["0000051-AX102"]["after"], 0.85)

    def test_gate_disabled_keeps_old_behaviour(self):
        store = fleet({"mtRamGateEnabled": False})
        _, doc, _ = run_main(store)
        self.assertEqual(doc["status"], "empty")
        self.assertNotIn("mtRamGate", doc)

    def test_same_region_preferred_and_crossing_can_be_forbidden(self):
        fsn = {"0000300-AX102": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 96,
                                 "ip": "n300", "location": "Falkenstein"}}
        store = fleet({"routineMaxMovesPerRun": 2}, fsn)
        store["servers"]["s9999"] = {"nodeId": "0000300-AX102", "proxmoxId": 9999,
                                     "memoryGb": 2, "ramFloorGb": 70.0, "cores": 2,
                                     "status": "stopped", "serverType": "mt"}
        _, doc, _ = run_main(store)
        # 0000300 is the fullest fitting by RAM (70 GB) but in another region
        self.assertTrue(all("->0000300" not in p for p in doc["planned"]), doc["planned"])
        # with every Helsinki destination gone, crossing happens only if allowed
        for nid in ("0000247-AX102-3-LTD", "0000047-AX102"):
            store["proxmox_nodes"][nid]["frozen"] = True
        _, doc, _ = run_main(store)
        self.assertTrue(doc["planned"] and all("->0000300" in p for p in doc["planned"]))
        store["config"]["defrag"]["mtRamGateCrossRegion"] = False
        _, doc, _ = run_main(store)
        self.assertEqual(doc["planned"], [])
        self.assertEqual(doc["mtRamGate"]["0000051-AX102"]["outcome"], "no-dest")

    def test_other_mt_moves_never_push_a_destination_over_the_gate(self):
        # cores gate: 0000049 over max_cores; the only node with free cores is
        # 0000048 at 84.8 % RAM -> a 2 GB guest would take it to 86.4 % > 85 %
        extra = {"0000049-AX102": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 10,
                                   "ip": "n49", "location": "Helsinki"},
                 "0000048-AX102": {"nodeType": "MT5", "gbRam": 125.0, "max_cores": 200,
                                   "ip": "n48", "location": "Helsinki"}}
        store = fleet({"mtRamGateEnabled": True}, extra)
        for nid in ("0000051-AX102", "0000247-AX102-3-LTD", "0000047-AX102"):
            store["proxmox_nodes"][nid]["max_cores"] = 1   # no core room elsewhere
        sid = 50000
        for nid, n, fl in (("0000049-AX102", 6, 2.0), ("0000048-AX102", 53, 2.0)):
            for _ in range(n):
                sid += 1
                store["servers"][f"x{sid}"] = {"nodeId": nid, "proxmoxId": sid,
                                               "memoryGb": 4, "ramFloorGb": fl, "cores": 2,
                                               "status": "running", "serverType": "mt"}
        _, doc, log = run_main(store)
        self.assertFalse(any("over-cap" in p for p in doc["planned"]), doc["planned"])
        self.assertIn("0000049-AX102 over-cap but no destination fits", log)
        store["config"]["defrag"]["mtRamGateEnabled"] = False   # old behaviour
        _, doc, _ = run_main(store)
        self.assertTrue(any("over-cap" in p and "->0000048" in p for p in doc["planned"]),
                        doc["planned"])

    def test_relief_mode_never_runs_the_ram_gate(self):
        store = fleet()
        _, doc, log = run_main(store, argv=("--relief", "--dry-run"))
        self.assertNotIn("mt-ram-gate", log)


if __name__ == '__main__':
    unittest.main()
