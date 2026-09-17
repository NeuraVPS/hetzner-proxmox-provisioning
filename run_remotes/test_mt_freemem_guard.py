import importlib.util
import unittest
from pathlib import Path


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


g = load('neuravps-mt-freemem-guard')
NOW = 1_800_000_000


def cfg(**kw):
    c = dict(g.DEFAULTS)
    c['DRY_RUN'] = 0
    c.update(kw)
    return c


def vm(vmid='719', memory=4096, floor=2048, actual=2560, total=4074, free=300, age=2,
       uuid='u1', lock=None, shares=None):
    return dict(vmid=vmid, memory=memory, floor=floor, actual=actual, total=total, free=free,
                age=age, uuid=uuid, lock=lock, shares=shares)


class Decide(unittest.TestCase):
    def test_low_free_with_balloon_held_pins_to_target(self):
        e = g.new_entry(vm())
        action, new, _ = g.decide(vm(), e, NOW, cfg())
        # 25 % of 4074 = 1018.5 -> deficit 719 -> +768 -> 2560+768 = 3328
        self.assertEqual((action, new), ('pin', 3328))
        self.assertTrue(e['owned'])
        self.assertEqual(e['orig'], 2048)

    def test_minimum_step_and_cap_at_memory(self):
        e = g.new_entry(vm())
        _, new, _ = g.decide(vm(actual=3700, free=560), e, NOW, cfg())
        self.assertEqual(new, 4096)

    def test_no_pin_when_little_balloon_held(self):
        e = g.new_entry(vm())
        self.assertIsNone(g.decide(vm(actual=4000, free=100), e, NOW, cfg())[0])

    def test_no_pin_when_free_is_fine(self):
        e = g.new_entry(vm())
        self.assertIsNone(g.decide(vm(free=900), e, NOW, cfg())[0])

    def test_stale_pins_to_memory_only_after_fresh_seen(self):
        e = g.new_entry(vm())
        self.assertIsNone(g.decide(vm(age=5000, free=1700), e, NOW, cfg())[0])
        g.decide(vm(free=1700), e, NOW, cfg())
        action, new, _ = g.decide(vm(age=5000, free=1700), e, NOW + 60, cfg())
        self.assertEqual((action, new), ('stale', 4096))

    def test_never_reported_stats_is_left_alone(self):
        e = g.new_entry(vm())
        self.assertIsNone(g.decide(vm(total=0, free=0, age=None), e, NOW, cfg())[0])

    def test_floor_changed_by_someone_else_drops_ownership(self):
        e = g.new_entry(vm())
        g.decide(vm(), e, NOW, cfg())
        g.decide(vm(floor=4096, actual=4096, free=2000), e, NOW + 60, cfg())  # welcome-boost
        self.assertFalse(e['owned'])

    def test_release_needs_credit_age_and_steps_to_orig(self):
        c = cfg(RELEASE_CREDIT_RUNS=3, RELEASE_PACE_RUNS=2, RELEASE_MIN_AGE_S=600)
        e = g.new_entry(vm())
        g.decide(vm(), e, NOW, c)                       # pinned at 3328
        roomy = vm(floor=3328, actual=3328, free=2200)
        t = NOW
        for _ in range(3):                              # credit but too young
            t += 60
            self.assertIsNone(g.decide(roomy, e, t, c)[0])
        t = NOW + 700
        action, new, _ = g.decide(roomy, e, t, c)
        self.assertEqual((action, new), ('release', 2816))
        a = vm(floor=2816, actual=2816, free=2200)
        self.assertIsNone(g.decide(a, e, t + 60, c)[0])     # paced: credit 1 -> 2
        action, new, _ = g.decide(a, e, t + 120, c)          # credit 3
        self.assertEqual((action, new), ('release', 2304))
        b = vm(floor=2304, actual=2304, free=2200)
        g.decide(b, e, t + 180, c)
        action, new, _ = g.decide(b, e, t + 240, c)
        self.assertEqual((action, new), ('release', 2048))
        self.assertFalse(e['owned'])

    def test_no_credit_when_release_would_leave_it_tight(self):
        c = cfg(RELEASE_CREDIT_RUNS=1, RELEASE_MIN_AGE_S=0)
        e = g.new_entry(vm())
        g.decide(vm(), e, NOW, c)
        # 30 % free now; after -512 it would be ~17 % -> no credit, no release
        self.assertIsNone(g.decide(vm(floor=3328, actual=3328, free=1222), e, NOW + 60, c)[0])
        self.assertEqual(e['credit'], 0)

    def test_pin_soon_after_release_is_a_bounce_with_backoff(self):
        c = cfg(RELEASE_CREDIT_RUNS=1, RELEASE_PACE_RUNS=1, RELEASE_MIN_AGE_S=0)
        e = g.new_entry(vm())
        g.decide(vm(), e, NOW, c)
        self.assertEqual(g.decide(vm(floor=3328, actual=3328, free=2200), e, NOW + 60, c)[0], 'release')
        action, _, reason = g.decide(vm(floor=2816, actual=2816, free=200), e, NOW + 3600, c)
        self.assertEqual(action, 'pin')
        self.assertEqual(e['bounces'], 1)
        self.assertEqual(e['block_until'], NOW + 3600 + c['BACKOFF_BASE_S'])
        self.assertIn('rebote', reason)
        # blocked: plenty of credit, no release before block_until
        for i in range(5):
            self.assertIsNone(g.decide(vm(floor=3840, actual=3840, free=2500), e, NOW + 7200 + i * 60, c)[0])

    def test_vmid_reuse_resets_state(self):
        e = g.new_entry(vm())
        g.decide(vm(), e, NOW, cfg())
        g.decide(vm(uuid='other', free=2000), e, NOW + 60, cfg())
        self.assertFalse(e['owned'])


class Run(unittest.TestCase):
    def test_host_reserve_blocks_and_rolls_back_state(self):
        state = {}
        st = g.run(cfg(), 'n-AX102', NOW, [vm()], state, avail_mb=12288 + 500, pending_mb=0,
                   apply=lambda *a, **k: self.fail('must not apply'))
        self.assertEqual(len(st['blockedHost']), 1)
        self.assertFalse(state['719']['owned'])

    def test_granted_memory_accumulates_against_reserve(self):
        applied = []
        vms = [vm(vmid=str(i), uuid=str(i)) for i in range(3)]
        st = g.run(cfg(), 'n-AX102', NOW, vms, {}, avail_mb=12288 + 1600, pending_mb=0,
                   apply=lambda v, new, push: applied.append(v['vmid']))
        self.assertEqual(len(applied), 2)          # 768 + 768 fits, the third does not
        self.assertEqual(len(st['blockedHost']), 1)

    def test_stale_goes_first_and_cap_per_run(self):
        applied = []
        state = {'9': dict(g.new_entry({'uuid': '9'}), fresh_seen=True)}
        vms = [vm(vmid=str(i), uuid=str(i)) for i in range(10)] + \
              [vm(vmid='9', uuid='9', age=4000, free=1700)]
        g.run(cfg(MAX_CHANGES_PER_RUN=2), 'n-AX102', NOW, vms, state, 10 ** 6, 0,
              apply=lambda v, new, push: applied.append((v['vmid'], new)))
        self.assertEqual(applied[0], ('9', 4096))
        self.assertEqual(len(applied), 2)

    def test_dry_run_never_applies_nor_keeps_ownership(self):
        state = {}
        st = g.run(cfg(DRY_RUN=1), 'n-AX102', NOW, [vm()], state, 10 ** 6, 0,
                   apply=lambda *a, **k: self.fail('dry run applied'))
        self.assertEqual(len(st['pinned']), 1)
        self.assertFalse(state['719']['owned'])
        self.assertTrue(state['719']['fresh_seen'])

    def test_apply_failure_rolls_back(self):
        def boom(*a, **k):
            raise BlockingIOError('lock busy')
        state = {}
        st = g.run(cfg(), 'n-AX102', NOW, [vm()], state, 10 ** 6, 0, apply=boom)
        self.assertEqual(len(st['errors']), 1)
        self.assertFalse(state['719']['owned'])

    def test_skips_big_vms_locked_fixed_and_manual_shares(self):
        vms = [vm(vmid='1', memory=19456, floor=9932, actual=12000, total=19000, free=500),
               vm(vmid='2', lock='migrate'), vm(vmid='3', shares='0'),
               vm(vmid='4', floor=4096, actual=4096, free=100)]
        st = g.run(cfg(), 'n-AX102', NOW, vms, {}, 10 ** 6, 0,
                   apply=lambda *a, **k: self.fail('touched an excluded VM'))
        self.assertEqual(st['lowFreeFixed'], 1)


if __name__ == '__main__':
    unittest.main()
