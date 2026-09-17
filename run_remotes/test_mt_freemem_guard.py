import importlib.util
import tempfile
import unittest
from pathlib import Path


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


g = load('neuravps-mt-freemem-guard')
g.log = lambda msg: None
NOW = 1_800_000_000
H = 3600


def cfg(**kw):
    c = dict(g.DEFAULTS)
    c['DRY_RUN'] = 0
    c['PROTECT_ENABLED'] = 1
    c['RAISE_MB_PER_RUN'] = 10 ** 6
    c['EXCLUDE_VMIDS'] = set()
    c.update(kw)
    return c


def vm(vmid='719', memory=4096, floor=2048, actual=2560, total=4074, free=300, age=2,
       uuid='u1', lock=None, shares=None):
    return dict(vmid=vmid, memory=memory, floor=floor, actual=actual, total=total, free=free,
                age=age, uuid=uuid, lock=lock, shares=shares)


def ctx(v, entry, c, orig=2048, session=False, session_known=True, pending=False, boot_guard=False, now=NOW):
    fresh = g.observe(v, entry, now, session, c)
    return dict(orig=orig, session=session, session_known=session_known, fresh=fresh,
                pending=pending, boot_guard=boot_guard)


def tick(v, e, c, now, **kw):
    return g.decide(v, e, now, c, ctx(v, e, c, now=now, **kw))


class Protect(unittest.TestCase):
    def test_low_free_with_balloon_held_pins_to_target(self):
        c = cfg(); e = g.new_entry(vm())
        # 25 % of 4074 = 1018.5 -> deficit 719 -> +768 -> 2560+768 = 3328
        self.assertEqual(tick(vm(), e, c, NOW)[:2], ('pin', 3328))

    def test_minimum_step_and_cap_at_memory(self):
        c = cfg(); e = g.new_entry(vm())
        self.assertEqual(tick(vm(actual=3700, free=560), e, c, NOW)[1], 4096)

    def test_no_pin_when_little_held_or_free_is_fine(self):
        c = cfg()
        self.assertIsNone(tick(vm(actual=4000, free=100), g.new_entry(vm()), c, NOW)[0])
        self.assertIsNone(tick(vm(free=900), g.new_entry(vm()), c, NOW)[0])

    def test_stale_pins_to_memory_only_after_fresh_seen(self):
        c = cfg(); e = g.new_entry(vm())
        self.assertIsNone(tick(vm(age=5000, free=1700), e, c, NOW)[0])
        tick(vm(free=1700), e, c, NOW + 60)
        self.assertEqual(tick(vm(age=5000, free=1700), e, c, NOW + 120)[:2], ('stale', 4096))

    def test_never_reported_stats_is_left_alone(self):
        c = cfg()
        self.assertIsNone(tick(vm(total=0, free=0, age=None), g.new_entry(vm()), c, NOW)[0])


def boosted(**kw):
    base = dict(floor=4096, actual=4096, free=2200)
    base.update(kw)
    return vm(**base)


class Decay(unittest.TestCase):
    def run_until_lower(self, e, c, v, start, limit=400, **kw):
        t = start
        for _ in range(limit):
            t += 60
            action, new, _ = tick(v, e, c, t, **kw)
            if action:
                return action, new, t
        return None, None, t

    def test_welcome_boost_raise_decays_in_512_steps_to_plan_floor(self):
        c = cfg(); e = g.new_entry(vm())
        floor, t, steps = 4096, NOW, []
        while True:
            action, new, t = self.run_until_lower(e, c, boosted(floor=floor, free=3100), t)
            if not action:
                break
            self.assertEqual(action, 'lower')
            steps.append((new, t))
            floor = new
            e['last_floor'] = new  # what apply() records
        self.assertEqual([s[0] for s in steps], [3584, 3072, 2560, 2048])
        self.assertGreaterEqual(steps[0][1] - NOW, 2 * H)          # 2 h credit + 2 h since raise
        for (_, a), (_, b) in zip(steps, steps[1:]):
            self.assertGreaterEqual(b - a, H)                       # paced 1 h

    def test_decay_stops_where_a_squeeze_would_breach_target_free(self):
        c = cfg(); e = g.new_entry(vm())
        floor, t, got = 4096, NOW, []
        while True:
            action, new, t = self.run_until_lower(e, c, boosted(floor=floor), t)
            if not action:
                break
            got.append(new); floor = new; e['last_floor'] = new
        # uses ~1.9 GB: needs actual >= 1896 + 1018 -> floor stops at 3072
        self.assertEqual(got, [3584, 3072])

    def test_mt_plus_never_below_its_plan_floor_even_with_low_record(self):
        c = cfg(); e = g.new_entry(vm())
        v = vm(memory=8192, floor=4608, actual=8192, total=8170, free=6500)
        action, new, _ = self.run_until_lower(e, c, v, NOW, orig=2048)
        self.assertEqual((action, new), ('lower', 4096))
        e['last_floor'] = 4096
        self.assertIsNone(self.run_until_lower(e, c, vm(memory=8192, floor=4096, actual=8192, total=8170,
                                                        free=6500), NOW + 10 * H)[0])

    def test_active_or_recent_rdp_session_blocks(self):
        c = cfg(); e = g.new_entry(vm())
        self.assertIsNone(self.run_until_lower(e, c, boosted(), NOW, session=True)[0])
        self.assertEqual(e['credit'], 0)
        # session ended: needs NO_SESSION_S of quiet even with credit
        t_end = NOW + 400 * 60
        action, _, t = self.run_until_lower(e, c, boosted(), t_end, limit=600)
        self.assertEqual(action, 'lower')
        self.assertGreaterEqual(t - t_end, c['NO_SESSION_S'])

    def test_not_enough_free_after_step_never_lowers(self):
        c = cfg(); e = g.new_entry(vm())
        # 1900/4074 = 46.6 % now, 34.1 % after -512 -> below 35 %
        self.assertIsNone(self.run_until_lower(e, c, boosted(free=1900), NOW, limit=1000)[0])

    def test_keeps_target_free_even_if_squeezed_to_new_floor(self):
        c = cfg(); e = g.new_entry(vm())
        # pvestatd not squeezing: actual 4096 over floor 2560; squeezing to a lower
        # floor would take the guest under 25 % -> no margin, no lower
        self.assertIsNone(self.run_until_lower(e, c, boosted(floor=2560, free=1950), NOW, limit=600)[0])

    def test_pending_boost_and_boot_guard_block(self):
        c = cfg()
        self.assertIsNone(self.run_until_lower(g.new_entry(vm()), c, boosted(), NOW, pending=True)[0])
        self.assertIsNone(self.run_until_lower(g.new_entry(vm()), c, boosted(), NOW, boot_guard=True)[0])

    def test_without_session_detection_waits_fallback_hours(self):
        c = cfg(); e = g.new_entry(vm())
        action, _, t = self.run_until_lower(e, c, boosted(), NOW, limit=1000, session_known=False)
        self.assertEqual(action, 'lower')
        self.assertGreaterEqual(t - NOW, c['FALLBACK_AFTER_RAISE_S'])

    def test_raise_by_someone_else_resets_clock(self):
        c = cfg(); e = g.new_entry(vm())
        for i in range(100):
            tick(boosted(floor=2560), e, c, NOW + i * 60)
        self.assertGreater(e['credit'], 50)
        tick(boosted(floor=4096), e, c, NOW + 100 * 60)            # welcome-boost again
        self.assertLessEqual(e['credit'], 1)
        self.assertEqual(e['raised_at'], NOW + 100 * 60)

    def test_guard_pin_waits_a_day_before_decay(self):
        c = cfg(); e = g.new_entry(vm())
        self.assertEqual(tick(vm(), e, c, NOW)[0], 'pin')
        e['last_floor'] = 3328
        action, _, t = self.run_until_lower(e, c, vm(floor=3328, actual=3328, free=2300), NOW, limit=2000)
        self.assertEqual(action, 'lower')
        self.assertGreaterEqual(t - NOW, c['PIN_MIN_AGE_S'])

    def test_pin_soon_after_lower_is_a_bounce_with_backoff(self):
        c = cfg(); e = g.new_entry(vm())
        action, new, t = self.run_until_lower(e, c, boosted(), NOW)
        self.assertEqual(action, 'lower')
        e['last_floor'] = new
        action, _, reason = tick(vm(floor=new, actual=2600, free=200), e, c, t + H)
        self.assertEqual(action, 'pin')
        self.assertEqual(e['bounces'], 1)
        self.assertEqual(e['block_until'], t + H + c['BACKOFF_BASE_S'])
        self.assertIn('REBOTE', reason)

    def test_vmid_reuse_resets_state(self):
        c = cfg(); e = g.new_entry(vm())
        tick(vm(), e, c, NOW)
        tick(vm(uuid='other', free=2000), e, c, NOW + 60)
        self.assertEqual(e['last_pin'], 0)
        self.assertEqual(e['uuid'], 'other')


def run(c, vms, state=None, avail=10 ** 6, pending=({}, 0), floors=None, sessions=(), known=True,
        boot=None, apply=None, now=NOW, dry_seen=None, swapout=0):
    applied = []
    fn = apply or (lambda v, new, action, orig: applied.append((v['vmid'], action, new, orig)))
    st = g.run(c, 'n-AX102', now, vms, {} if state is None else state, avail, pending,
               floors or {}, set(sessions), known, boot or {}, apply=fn, dry_seen=dry_seen,
               swapout_ps=swapout)
    return st, applied


class Tick(unittest.TestCase):
    def test_host_reserve_blocks_raises(self):
        state = {}
        st, applied = run(cfg(), [vm()], state, avail=12288 + 500)
        self.assertEqual(len(st['blockedHost']), 1)
        self.assertEqual(applied, [])
        self.assertEqual(state['719']['last_pin'], 0)

    def test_granted_memory_accumulates_against_reserve(self):
        vms = [vm(vmid=str(i), uuid=str(i)) for i in range(3)]
        st, applied = run(cfg(), vms, avail=12288 + 1600)
        self.assertEqual(len(applied), 2)
        self.assertEqual(len(st['blockedHost']), 1)

    def test_lower_needs_room_to_undo(self):
        state = {'5': dict(g.new_entry({'uuid': '5'}), last_floor=4096, credit=500, raised_at=NOW - 10 * H,
                           fresh_seen=True)}
        _, applied = run(cfg(), [boosted(vmid='5', uuid='5')], state, avail=12288 + 100)
        self.assertEqual(applied, [])
        _, applied = run(cfg(), [boosted(vmid='5', uuid='5')], state, avail=40000, now=NOW + 60)
        self.assertEqual(applied, [('5', 'lower', 3584, 2048)])

    def test_stale_first_then_pins_and_cap(self):
        state = {'9': dict(g.new_entry({'uuid': '9'}), fresh_seen=True, last_floor=2048)}
        vms = [vm(vmid=str(i), uuid=str(i)) for i in range(10)] + [vm(vmid='9', uuid='9', age=4000, free=1700)]
        _, applied = run(cfg(MAX_CHANGES_PER_RUN=2), vms, state)
        self.assertEqual(applied[0][:3], ('9', 'stale', 4096))
        self.assertEqual(len(applied), 2)

    def test_dry_run_keeps_observations_not_actions(self):
        state = {}
        c = cfg(DRY_RUN=1)
        st, _ = run(c, [vm()], state, apply=lambda *a: self.fail('dry run applied'))
        self.assertEqual(len(st['raised']), 1)
        self.assertEqual(state['719']['last_pin'], 0)
        self.assertEqual(state['719']['raised_at'], NOW)
        st, _ = run(c, [boosted()], state, now=NOW + 60, apply=lambda *a: self.fail('dry run applied'))
        self.assertEqual(state['719']['credit'], 1)
        self.assertEqual(st['decayWouldQualify'], 1)
        self.assertEqual(st['aboveTargetMb'], 2048)

    def test_apply_failure_rolls_back(self):
        def boom(*a):
            raise BlockingIOError('lock busy')
        state = {}
        st, _ = run(cfg(), [vm()], state, apply=boom)
        self.assertEqual(len(st['errors']), 1)
        self.assertEqual(state['719']['last_pin'], 0)

    def test_exclusions(self):
        vms = [vm(vmid='1', memory=19456, floor=9932, actual=12000, total=19000, free=500),
               vm(vmid='2', lock='migrate'), vm(vmid='3', shares='0'), vm(vmid='4'),
               vm(vmid='6', floor=4096, actual=4096, free=100)]
        st, applied = run(cfg(EXCLUDE_VMIDS={'4'}), vms)
        self.assertEqual(applied, [])
        self.assertEqual(st['lowFreeFixed'], 1)

    def test_no_floors_record_seeds_plan_floor_on_first_touch(self):
        state = {'5': dict(g.new_entry({'uuid': '5'}), last_floor=3584, credit=500, raised_at=NOW - 10 * H,
                           fresh_seen=True)}
        _, applied = run(cfg(), [boosted(vmid='5', uuid='5', floor=3584)], state, floors={})
        self.assertEqual(applied, [('5', 'lower', 3072, 2048)])


class ProtectGatesV3(unittest.TestCase):
    def test_protect_is_off_by_default(self):
        self.assertEqual(g.DEFAULTS['PROTECT_ENABLED'], 0)
        state = {}
        st, applied = run(cfg(PROTECT_ENABLED=0), [vm()], state)
        self.assertEqual((applied, st['raised']), ([], []))
        self.assertEqual(state['719']['last_pin'], 0)

    def test_decay_still_runs_with_protect_off(self):
        state = {'5': dict(g.new_entry({'uuid': '5'}), last_floor=4096, credit=500, raised_at=NOW - 10 * H,
                           fresh_seen=True)}
        _, applied = run(cfg(PROTECT_ENABLED=0), [boosted(vmid='5', uuid='5')], state)
        self.assertEqual(applied, [('5', 'lower', 3584, 2048)])

    def test_no_raise_while_host_swaps_out(self):
        st, applied = run(cfg(), [vm()], swapout=500)
        self.assertEqual(applied, [])
        self.assertEqual(st['blockedSwap'], 1)

    def test_raise_budget_per_run(self):
        vms = [vm(vmid=str(i), uuid=str(i)) for i in range(4)]    # 768 MB each
        _, applied = run(cfg(RAISE_MB_PER_RUN=1024), vms)
        self.assertEqual(len(applied), 1)
        _, applied = run(cfg(RAISE_MB_PER_RUN=1024), [vm(actual=2048, free=100)])
        self.assertEqual(len(applied), 1)                          # first raise always fits


class Sessions(unittest.TestCase):
    TEXT = """tcp      6 431999 ESTABLISHED src=2a01:4f9:c01f:e:ffff::2 dst=2a01:4f9:c01f:e::475 sport=7869 dport=3389 src=2a01:4f9:c01f:e::475 dst=2a01:4f9:c01f:e:ffff::2 sport=3389 dport=7869 [ASSURED] mark=0 use=1
tcp      6 400000 ESTABLISHED src=2a01:4f9:c01f:e:ffff::2 dst=2a01:4f9:c01f:e::383 sport=53931 dport=3389 src=2a01:4f9:c01f:e::383 dst=2a01:4f9:c01f:e:ffff::2 sport=3389 dport=53931 [ASSURED] mark=0 use=1
tcp      6 110 TIME_WAIT src=1.2.3.4 dst=2a01:4f9:c01f:e::d7 sport=1 dport=3389 src=x dst=y sport=3389 dport=1 [ASSURED] mark=0 use=1
tcp      6 431990 ESTABLISHED src=2a01:4f9:c01f:e:ffff::2 dst=2a01:4f9:c01f:e::d7 sport=51851 dport=3389 src=2a01:4f9:c01f:e::d7 dst=2a01:4f9:c01f:e:ffff::2 sport=3389 dport=51851 [ASSURED] mark=0 use=1
"""

    def test_parse_live_flows_to_vmid(self):
        # 0x475 = 1141 live; 0x383 idle > 10 min; 0xd7 = 215 live (TIME_WAIT ignored)
        self.assertEqual(g.parse_rdp_sessions(self.TEXT, 432000, 600), {'1141', '215'})

    def test_load_cfg_exclude_and_ints(self):
        with tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False) as f:
            f.write('ENABLED=1\nDRY_RUN=0\nEXCLUDE_VMIDS="215, 1141"\n# c\n')
        c = g.load_cfg(f.name, env={})
        self.assertEqual((c['DRY_RUN'], c['EXCLUDE_VMIDS']), (0, {'215', '1141'}))


if __name__ == '__main__':
    unittest.main()
