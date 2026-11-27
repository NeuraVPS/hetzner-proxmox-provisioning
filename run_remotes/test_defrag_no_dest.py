"""MT CPU relief classifies WHY a hot node was not relieved.

Only a measured "no-dest" may be read as missing capacity: NeuraVPS buys a
452 EUR/month server on it (ltd_procurement, 2026-09-16)."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('defrag_no_dest', Path(__file__).with_name('neuravps-defrag.py'))
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)


def plan(hot, victims, dests, loads, threads=32, agent=True, max_moves=1):
    """victims: {nid: [(demand, vm)]}; dests: ordered candidate list offered by
    pick_dest; loads: {dst: p95 or None}."""
    booked = []

    def pick_dest(vm, excluded):
        return next((d for d in dests if d not in excluded), None)

    moved, outcomes = df.plan_mt_cpu_relief(
        hot, lambda nid: victims.get(nid, []), pick_dest, lambda d: loads.get(d),
        lambda d: threads, lambda nid, vm: agent,
        lambda vm, nid, dst, demand: booked.append((vm, nid, dst)),
        max_moves=max_moves)
    return moved, outcomes, booked


class MtCpuReliefSeveralMoves(unittest.TestCase):
    """mtReliefMaxMovesPerTick (2026-11-27): several hot nodes relieved per tick."""

    def test_default_is_still_one_move(self):
        moved, outcomes, booked = plan(['a', 'b'], {'a': [(1.5, 'v1')], 'b': [(1.5, 'v2')]},
                                       ['cool'], {'cool': .10})
        self.assertEqual((moved, booked), ('a', [('v1', 'a', 'cool')]))
        self.assertEqual(outcomes, {'a': 'moved'})   # b not probed

    def test_one_move_per_hot_node_up_to_the_cap(self):
        victims = {n: [(1.5, 'v' + n), (1.0, 'w' + n)] for n in 'abcd'}
        moved, outcomes, booked = plan(list('abcd'), victims, ['c1', 'c2', 'c3'],
                                       {'c1': .10, 'c2': .10, 'c3': .10}, max_moves=3)
        self.assertEqual(moved, 'a')
        self.assertEqual([b[1] for b in booked], ['a', 'b', 'c'])   # one each, d not probed
        self.assertEqual(outcomes, {'a': 'moved', 'b': 'moved', 'c': 'moved'})

    def test_booked_demand_counts_against_the_destination(self):
        # First: 0.60 + 2.0/32 = 0.6625 <= 0.70 -> booked. Second would be
        # 0.6625 + 2.0/32 = 0.725 > 0.70 -> must NOT reuse the same headroom.
        victims = {'a': [(2.0, 'va')], 'b': [(2.0, 'vb')]}
        _, outcomes, booked = plan(['a', 'b'], victims, ['only'], {'only': .60}, max_moves=3)
        self.assertEqual(booked, [('va', 'a', 'only')])
        self.assertEqual(outcomes, {'a': 'moved', 'b': 'no-dest'})

    def test_dead_agent_tries_next_victim(self):
        calls = []

        def pick_dest(vm, excluded):
            return next((d for d in ['cool'] if d not in excluded), None)

        booked = []
        moved, outcomes = df.plan_mt_cpu_relief(
            ['a'], lambda nid: [(1.5, 'dead'), (1.0, 'alive')], pick_dest, lambda d: .1,
            lambda d: 32, lambda nid, vm: vm != 'dead',
            lambda vm, nid, dst, demand: booked.append(vm), max_moves=3)
        self.assertEqual((moved, booked, outcomes), ('a', ['alive'], {'a': 'moved'}))


class MtCpuReliefSeveralMovesEdges(unittest.TestCase):
    def test_unreadable_destination_mixed_with_measured(self):
        victims = {'a': [(1.5, 'va')], 'b': [(1.5, 'vb')]}
        _, outcomes, booked = plan(['a', 'b'], victims, ['x', 'dead'],
                                   {'x': .10, 'dead': None}, max_moves=2)
        self.assertEqual(booked[0], ('va', 'a', 'x'))
        self.assertEqual(outcomes['a'], 'moved')
        # b: x is still measurable and has room (.10 + 1.5/32*1 + 1.5/32 < .70)
        self.assertEqual(outcomes['b'], 'moved')
        _, outcomes, _ = plan(['a'], {'a': [(30.0, 'big')]}, ['x', 'dead'],
                              {'x': .10, 'dead': None}, max_moves=2)
        self.assertEqual(outcomes, {'a': 'unmeasured'})   # never no-dest

    def test_no_victim_does_not_use_the_quota(self):
        victims = {'b': [(1.5, 'vb')], 'c': [(1.5, 'vc')]}
        _, outcomes, booked = plan(['a', 'b', 'c'], victims, ['x', 'y'],
                                   {'x': .10, 'y': .10}, max_moves=2)
        self.assertEqual(outcomes, {'a': 'no-victim', 'b': 'moved', 'c': 'moved'})
        self.assertEqual(len(booked), 2)

    def test_a_hot_node_is_never_a_destination(self):
        _, _, booked = plan(['a', 'b'], {'a': [(1.5, 'va')], 'b': []}, ['b', 'x'],
                            {'b': .01, 'x': .30}, max_moves=2)
        self.assertEqual(booked, [('va', 'a', 'x')])


class MtCpuReliefCoolestDestination(unittest.TestCase):
    def test_coolest_measured_destination_wins_over_pick_dest_order(self):
        # pick_dest offers the fullest first (best-fit); relief must take the coolest.
        _, _, booked = plan(['a'], {'a': [(1.5, 'v1')]}, ['full', 'warm', 'empty'],
                            {'full': .60, 'warm': .50, 'empty': .05})
        self.assertEqual(booked, [('v1', 'a', 'empty')])

    def test_two_moves_spread_by_load(self):
        victims = {'a': [(8.0, 'va')], 'b': [(8.0, 'vb')]}
        _, _, booked = plan(['a', 'b'], victims, ['x', 'y'], {'x': .05, 'y': .10}, max_moves=2)
        # 8/32 = .25: a -> x (.05 -> .30), then b -> y (.10 < .30)
        self.assertEqual(booked, [('va', 'a', 'x'), ('vb', 'b', 'y')])


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
