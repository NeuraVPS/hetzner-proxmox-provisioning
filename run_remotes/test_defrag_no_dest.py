"""MT CPU relief classifies WHY a hot node was not relieved.

Only a measured "no-dest" may be read as missing capacity: NeuraVPS buys a
452 EUR/month server on it (ltd_procurement, 2026-09-16)."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('defrag_no_dest', Path(__file__).with_name('neuravps-defrag.py'))
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)


def plan(hot, victims, dests, loads, threads=32, agent=True):
    """victims: {nid: [(demand, vm)]}; dests: ordered candidate list offered by
    pick_dest; loads: {dst: p95 or None}."""
    booked = []

    def pick_dest(vm, excluded):
        return next((d for d in dests if d not in excluded), None)

    moved, outcomes = df.plan_mt_cpu_relief(
        hot, lambda nid: victims.get(nid, []), pick_dest, lambda d: loads.get(d),
        lambda d: threads, lambda nid, vm: agent,
        lambda vm, nid, dst, demand: booked.append((vm, nid, dst)))
    return moved, outcomes, booked


class MtCpuReliefOutcomes(unittest.TestCase):
    def test_moves_to_first_measured_destination_with_room(self):
        moved, outcomes, booked = plan(['a'], {'a': [(1.5, 'vm1')]}, ['full', 'cool'],
                                       {'full': .69, 'cool': .40})
        self.assertEqual(moved, 'a')
        self.assertEqual(outcomes, {'a': 'moved'})
        self.assertEqual(booked, [('vm1', 'a', 'cool')])

    def test_every_destination_measured_and_full_is_no_dest(self):
        _, outcomes, booked = plan(['a'], {'a': [(1.5, 'vm1')]}, ['x', 'y'], {'x': .69, 'y': .80})
        self.assertEqual(outcomes, {'a': 'no-dest'})
        self.assertEqual(booked, [])

    def test_no_candidate_destination_at_all_is_no_dest(self):
        self.assertEqual(plan(['a'], {'a': [(1.5, 'vm1')]}, [], {})[1], {'a': 'no-dest'})

    def test_an_unreadable_destination_is_never_capacity(self):
        # SSH/RRD failure on one destination: "no room" was not proven.
        _, outcomes, _ = plan(['a'], {'a': [(1.5, 'vm1')]}, ['x', 'y'], {'x': None, 'y': .90})
        self.assertEqual(outcomes, {'a': 'unmeasured'})
        _, outcomes, _ = plan(['a'], {'a': [(1.5, 'vm1')]}, ['x'], {'x': .1}, threads=0)
        self.assertEqual(outcomes, {'a': 'unmeasured'})

    def test_no_measured_victim_and_dead_agent_are_not_capacity(self):
        self.assertEqual(plan(['a'], {}, ['x'], {'x': .1})[1], {'a': 'no-victim'})
        self.assertEqual(plan(['a'], {'a': [(1.5, 'vm1')]}, ['x'], {'x': .1}, agent=False)[1],
                         {'a': 'agent-down'})

    def test_partial_pain_is_recorded_and_nodes_after_the_move_are_not_probed(self):
        victims = {'a': [(9.0, 'big')], 'b': [(1.0, 'small')], 'c': [(1.0, 'other')]}
        moved, outcomes, booked = plan(['a', 'b', 'c'], victims, ['x'], {'x': .60})
        # a: 0.60 + 9/32 > 0.70 -> no-dest; b fits -> moved; c never tried.
        self.assertEqual(moved, 'b')
        self.assertEqual(outcomes, {'a': 'no-dest', 'b': 'moved'})
        self.assertEqual(df.no_dest_nodes(outcomes, {'0000096-AX162-R'}),
                         {'MT5': ['a'], 'SQX': ['0000096-AX162-R']})

    def test_no_dest_record_always_has_both_models(self):
        self.assertEqual(df.no_dest_nodes({}, set()), {'MT5': [], 'SQX': []})


if __name__ == '__main__':
    unittest.main()
