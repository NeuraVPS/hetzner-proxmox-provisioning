import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import time
import os
import unittest
from unittest.mock import patch, MagicMock


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wb = load('neuravps-welcome-boost')
guard = load('neuravps-ram-guard')
df = load('neuravps-defrag')


class Ownership(unittest.TestCase):
    def test_identity_ipv6_does_not_determine_host(self):
        self.assertEqual(wb.node_addr({'nodeId':'n2','ipv6':'2a01:4f9:c01f:e::99'},
                                     {'n2':'2001:db8:2::2'}), '2001:db8:2::2')
        self.assertIsNone(wb.node_addr({'nodeId':'gone'}, {}))

    def test_only_mapped_ports_including_large_vmids(self):
        from io import StringIO
        text = '\n'.join(f'ipv4 tcp 6 ESTABLISHED src=1.2.3.4 dst=5.6.7.8 sport=55 dport={p} [ASSURED]' for p in (22131,23333))
        with patch('builtins.open', return_value=StringIO(text)):
            self.assertEqual(wb.conntrack_flows({22131}), {('1.2.3.4',55,22131)})

    def test_wrong_hostname_cannot_mutate_vm(self):
        with patch.object(guard.socket,'gethostname',return_value='wrong'), patch.object(guard,'qmp') as qmp:
            with self.assertRaisesRegex(RuntimeError,'identity'):
                guard.inspect(99,'expected')
            qmp.assert_not_called()


class Capacity(unittest.TestCase):
    def test_inventory_is_clamped_to_physical_ram(self):
        self.assertEqual(df.base_ram({'max_base_ram':251,'gbRam':219.87}),219.87)
        self.assertEqual(df.base_ram({'max_base_ram':251,'gbRam':0}),251)

    def test_friday_peak_blocks_sunday_destination(self):
        self.assertFalse(df.weekday_cpu_ok({'cpuProfileAt':1000,'cpuWeekdayP95Pct':94},1001))
        self.assertTrue(df.weekday_cpu_ok({'cpuProfileAt':1000,'cpuWeekdayP95Pct':55},1001))

    def test_cpu_relief_requires_two_recent_bad_windows(self):
        self.assertFalse(df.sustained_cpu_harm([{'at':1000,'ms':80}],2000))
        self.assertFalse(df.sustained_cpu_harm([{'at':1000,'ms':80},{'at':1900,'ms':10}],2000))
        self.assertFalse(df.sustained_cpu_harm([{'at':1000,'ms':80},{'at':1001,'ms':80}],2000))
        self.assertTrue(df.sustained_cpu_harm([{'at':1000,'ms':80},{'at':1900,'ms':80}],2000))
        self.assertFalse(df.sustained_cpu_harm([{'at':1000,'ms':80},{'at':1900,'ms':80}],20000))

    def test_fixed_and_stopped_configs_consume_budget(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(guard,'CONF_DIR',Path(directory)):
            for vmid, floor in [(1,0),(2,2048)]:
                (Path(directory)/f'{vmid}.conf').write_text(f'memory: 4096\nballoon: {floor}\n[snapshot]\nmemory: 99999\n')
            self.assertEqual(guard.floor_total(),6144)

    def run_boost(self, available=30000, floors=100000, pending=0, reconciler=True, entitle=None,
                  inspected=(19456,9932,12000)):
        from io import StringIO
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(guard,'STATE_DIR',Path(directory)), \
             patch.object(guard,'RECONCILER_DEFAULTS',Path(directory)/'reconciler-defaults'), \
             patch.object(guard,'RECONCILER_TIMER',
                          Path(directory) if reconciler else Path(directory)/'absent.timer'), \
             patch('builtins.open',return_value=MagicMock()), \
             patch.object(guard.fcntl,'flock'), \
             patch.object(guard,'inspect',return_value=(*inspected,available)), \
             patch.object(guard,'meminfo',return_value={'MemTotal':257000,'MemAvailable':available}), \
             patch.object(guard,'floor_total',return_value=floors), \
             patch.object(guard.subprocess,'run') as qm, \
             patch.object(guard,'qmp') as qmp:
            if entitle is not None:
                (Path(directory)/'reconciler-defaults').write_text(f'# test\nENTITLE_ENABLED={entitle}\n')
            if pending:
                (Path(directory)/'pending-boosts.json').write_text(json.dumps({'other':{'mb':pending,'until':1e12}}))
            result=guard.boost(99,'node-AX162')
            self.last_qm_args = qm.call_args
            return result, qm.call_count, qmp.call_args

    def test_grant_rechecks_physical_reserve_and_floor_budget(self):
        for kwargs in ({'available':19000},{'floors':230000},{'pending':20000}):
            result,calls,_=self.run_boost(**kwargs)
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(calls,0)

    def test_budget_follows_the_reconciler_not_the_node_name(self):
        # 2026-11-04: 90 % only where the balloon reconciler runs (all SQX
        # nodes, including an AX102 assigned to SQX); 100 % without it (MT).
        self.assertEqual(self.run_boost(floors=230000)[0]['status'], 'blocked')
        self.assertEqual(self.run_boost(floors=230000, reconciler=False)[0]['status'], 'boosted')

    def test_entitlement_caps_over_budget_boost_at_memory_minus_3g(self):
        # 2026-11-27: over the PAPER budget, boost to memory - 3 GB (19456 ->
        # 16384) instead of refusing, when >= 28 GB are really available.
        result,calls,args=self.run_boost(available=40000, floors=230000, entitle=1)
        self.assertEqual(result, {'status':'boosted','before':12000,'target':16384,'capped':'entitlement'})
        self.assertEqual(calls,1)
        self.assertEqual(args.args,(99,'balloon',{'value':16384*1048576}))

    def test_entitlement_needs_real_ram_switch_and_reconciler(self):
        self.assertEqual(self.run_boost(available=25000, floors=230000, entitle=1)[0]['status'],'blocked')
        self.assertEqual(self.run_boost(available=40000, floors=230000, entitle=0)[0]['status'],'blocked')
        self.assertEqual(self.run_boost(available=40000, floors=230000)[0]['status'],'blocked')
        self.assertEqual(self.run_boost(available=40000, floors=230000, entitle=1, pending=15000)[0]['status'],'blocked')
        # MT (no reconciler): the band does not apply, 100 % budget unchanged
        self.assertEqual(self.run_boost(available=40000, floors=260000, entitle=1, reconciler=False)[0]['status'],'blocked')

    def test_entitlement_never_shrinks_a_live_guest(self):
        # review 2026-11-27: ent (58368) < actual (60000) < memory - 1 GB on a
        # 60 GB vps-e: only the floor may move, no qmp balloon below actual.
        result,calls,args=self.run_boost(available=60000, floors=200000, entitle=1,
                                         inspected=(61440,18432,58000))
        self.assertEqual(result['target'],51200)
        self.assertEqual(calls,1)          # qm set --balloon 51200
        self.assertIsNone(args)            # no qmp call at all

    def test_already_at_entitlement_is_full_not_a_retry_loop(self):
        result,calls,_=self.run_boost(available=60000, floors=230000, entitle=1,
                                      inspected=(19456,16384,16000))
        self.assertEqual(result,{'status':'full','reason':'entitlement'})
        self.assertEqual(calls,0)

    def test_entitlement_absolute_cap_ram_minus_reserve(self):
        # MemTotal 257000: floors 250000 + (16384-9932) > 257000 - 12288
        self.assertEqual(self.run_boost(available=60000, floors=250000, entitle=1)[0]['status'],'blocked')

    def test_entitlement_config_parsing_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'d'
            p.write_text('ENTITLE_ENABLED=1\nENTITLE_AVAIL_MIN_MB=30000\n')
            self.assertEqual(guard.entitle_cfg(p),{'ENTITLE_ENABLED':1,'ENTITLE_AVAIL_MIN_MB':30000})
            p.write_text('ENTITLE_ENABLED=1  # canario 27/11\n')
            self.assertEqual(guard.entitle_cfg(p)['ENTITLE_ENABLED'],1)
            p.write_text('ENTITLE_ENABLED=yes\n')
            self.assertEqual(guard.entitle_cfg(p)['ENTITLE_ENABLED'],0)
            self.assertEqual(guard.entitle_cfg(Path(directory)/'missing')['ENTITLE_ENABLED'],0)
        self.assertEqual([guard.entitle_mb(m) for m in (16384,19456,31744,61440,4096)],[13312,16384,28672,51200,1024])

    def test_qmp_uses_bytes_and_only_after_budget_passes(self):
        result,calls,args=self.run_boost()
        self.assertEqual(result['status'],'boosted')
        self.assertEqual(calls,1)
        self.assertEqual(args.args,(99,'balloon',{'value':19456*1048576}))


class BootOwnership(unittest.TestCase):
    def control(self, held, token):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(guard,'STATE_DIR',Path(directory)), \
             patch('builtins.open',return_value=MagicMock()), \
             patch.object(guard.fcntl,'flock'), \
             patch.object(guard.socket,'gethostname',return_value='node'), \
             patch.object(guard,'config',return_value={'balloon':'19456','smbios1':'uuid=one'}), \
             patch.object(guard.subprocess,'run') as qm:
            p=Path(directory)/'boot-guards.json'
            p.write_text(json.dumps({'99':{'token':'current','floor':9932,'target':19456,'uuid':'uuid=one','held':held}}))
            guard.boot_control(99,'node',token)
            return qm.call_count, json.loads(p.read_text())

    def test_original_boot_can_restore_its_floor(self):
        calls,state=self.control(False,'current')
        self.assertEqual(calls,1)
        self.assertEqual(state,{})

    def test_login_preserves_demand_floor_after_boot(self):
        calls,state=self.control(True,'current')
        self.assertEqual(calls,0)
        self.assertEqual(state,{})

    def test_old_boot_cannot_restore_a_new_boot(self):
        calls,state=self.control(False,'old')
        self.assertEqual(calls,0)
        self.assertEqual(state['99']['token'],'current')


class LockContention(unittest.TestCase):
    """2026-11-15: the boot restore hit EAGAIN against a reconciler pass and was lost."""

    def setUp(self):
        import fcntl
        self.fcntl = fcntl
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.lock_path = str(Path(self.dir.name) / 'ram.lock')
        self.holder = open(self.lock_path, 'a')
        self.addCleanup(self.holder.close)
        fcntl.flock(self.holder, fcntl.LOCK_EX)  # the "reconciler"

    def run_restore(self, wait):
        guards = {'99': {'token': 'tok', 'floor': 9932, 'target': 19456,
                         'uuid': 'uuid=one', 'held': False}}
        state = Path(self.dir.name) / 'state'
        state.mkdir()
        (state / 'boot-guards.json').write_text(json.dumps(guards))
        with patch.object(guard, 'LOCK_PATH', self.lock_path), \
             patch.object(guard, 'STATE_DIR', state), \
             patch.object(guard, 'LOCK_WAIT_S', {'restore': wait}), \
             patch.object(guard, 'LOCK_POLL_S', (0.05,)), \
             patch.object(guard.socket, 'gethostname', return_value='node'), \
             patch.object(guard, 'config', return_value={'balloon': '19456', 'smbios1': 'uuid=one'}), \
             patch.object(guard.subprocess, 'run') as qm:
            try:
                result = guard.boot_control(99, 'node', 'tok')
            except RuntimeError as exc:
                result = exc
            return result, qm.call_count, json.loads((state / 'boot-guards.json').read_text())

    def test_busy_for_a_few_seconds_then_restore_applies(self):
        import threading
        threading.Timer(0.4, lambda: self.fcntl.flock(self.holder, self.fcntl.LOCK_UN)).start()
        started = time.monotonic()
        result, calls, state = self.run_restore(wait=5)
        self.assertEqual(result, {'status': 'boot-guard-updated'})
        self.assertEqual(calls, 1)  # qm set --balloon <floor>
        self.assertEqual(state, {})
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_busy_forever_gives_up_with_log_and_does_not_hang(self):
        from io import StringIO
        err = StringIO()
        started = time.monotonic()
        with patch.object(guard.sys, 'stderr', err):
            result, calls, state = self.run_restore(wait=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('busy', str(result))
        self.assertEqual(calls, 0)
        self.assertIn('gave up', err.getvalue())
        self.assertIn('99', json.dumps(state))  # guard entry kept, nothing lost

    def test_every_action_has_a_bounded_wait(self):
        for action in ('boot', 'restore', 'hold', 'boost', 'state'):
            self.assertGreater(guard.LOCK_WAIT_S[action], 0)
        # welcome-boost's SSH timeouts: hold 15 s, boost 30 s
        self.assertLess(guard.LOCK_WAIT_S['hold'], 15)
        self.assertLess(guard.LOCK_WAIT_S['boost'], 30)
        self.assertGreaterEqual(guard.LOCK_WAIT_S['restore'], 90)


class MigrationHistory(unittest.TestCase):
    def test_import_preserves_bounces_without_reusing_old_host_rates(self):
        import base64
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(guard,'STATE_DIR',Path(directory)), \
             patch('builtins.open',return_value=MagicMock()), \
             patch.object(guard.fcntl,'flock'), \
             patch.object(guard.socket,'gethostname',return_value='node'), \
             patch.object(guard,'config',return_value={'smbios1':'uuid=one'}):
            data={'uuid':'uuid=one','floor':9932,'sample':['99',123456,1000,2000,12,800,3,9999999999]}
            encoded=base64.b64encode(json.dumps(data).encode()).decode()
            self.assertEqual(guard.transfer_state(99,'node',encoded)['status'],'state-imported')
            row=(Path(directory)/'samples.tsv').read_text().split()
            self.assertEqual(row[:5],['99','0','0','0','0'])
            self.assertEqual(row[6:],['3','9999999999'])
            data['uuid']='uuid=other'
            with self.assertRaisesRegex(RuntimeError,'UUID'):
                guard.transfer_state(99,'node',base64.b64encode(json.dumps(data).encode()).decode())


class ReconcilerTelemetry(unittest.TestCase):
    def run_sample(self, stale):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            script=Path(__file__).with_name('neuravps-balloon-reconciler.sh').read_text()
            script=__import__('re').sub(r'/var/run|/var/lib|/etc/pve|/etc/default|/proc|/run',
                lambda match: str(root/match.group().lstrip('/')),script)
            for part in ('var/run/qemu-server','var/lib/neuravps-balloon','etc/pve/qemu-server','etc/default','proc/pressure','run','bin'):
                (root/part).mkdir(parents=True,exist_ok=True)
            now=int(time.time())
            (root/'proc/meminfo').write_text('MemTotal: 67108864 kB\nMemAvailable: 33554432 kB\n')
            (root/'proc/vmstat').write_text('pswpin 0\n')
            (root/'proc/pressure/io').write_text('some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
            (root/'etc/pve/qemu-server/99.conf').write_text('memory: 19456\nballoon: 19456\n')
            (root/'var/run/qemu-server/99.pid').write_text('1')
            (root/'var/lib/neuravps-balloon/floors.json').write_text('{"99":9932}')
            sample=f'99\t100\t{now-60}\t2000\t0\t0\t0\t0\n'
            (root/'var/lib/neuravps-balloon/samples.tsv').write_text(sample)
            qm=root/'bin/qm'
            qm.write_text(f'''#!/bin/sh
if [ "$1" = monitor ]; then
 echo 'balloon: actual=19456 major_page_faults=100 last_update={now-600 if stale else now}'
else
 echo "$*" >> '{root}/qm-writes'
fi
''')
            qm.chmod(0o755)
            logger=root/'bin/logger';logger.write_text('#!/bin/sh\nexit 0\n');logger.chmod(0o755)
            run=root/'reconcile.sh';run.write_text(script)
            subprocess.run(['bash',str(run)],env={**os.environ,'PATH':str(root/'bin')+':'+os.environ['PATH']},capture_output=True,text=True,check=True)
            return (root/'qm-writes').exists()

    def test_stale_guest_with_large_idle_credit_cannot_be_lowered(self):
        self.assertFalse(self.run_sample(True))

    def test_fresh_idle_guest_keeps_the_existing_decay_behavior(self):
        self.assertTrue(self.run_sample(False))


class ReconcilerEntitlement(unittest.TestCase):
    """v11 (2026-11-27): below memory - 3 GB a raise blocked ONLY by the paper
    budget goes straight to the entitlement when the host really has the RAM;
    and an entitled floor can still decay on an over-budget node."""

    def run_node(self, entitle, avail_mb, floor=4915, faults_ps=200, idle=0, actual=7000, big=True,
                 n98_floor=230000):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = Path(__file__).with_name('neuravps-balloon-reconciler.sh').read_text()
            script = __import__('re').sub(r'/var/run|/var/lib|/etc/pve|/etc/default|/proc|/run',
                lambda match: str(root/match.group().lstrip('/')), script)
            for part in ('var/run/qemu-server','var/lib/neuravps-balloon','etc/pve/qemu-server','etc/default','proc/pressure','run','bin'):
                (root/part).mkdir(parents=True, exist_ok=True)
            now = int(time.time())
            (root/'proc/meminfo').write_text(f'MemTotal: {262144*1024} kB\nMemAvailable: {avail_mb*1024} kB\n')
            (root/'proc/vmstat').write_text('pswpin 0\n')
            (root/'proc/pressure/io').write_text('some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
            # vm 99: vps-a 16G squeezed; vm 98: a big neighbour whose floor
            # eats the paper budget (90 % of 256 GB = 235,929 MB).
            (root/'etc/pve/qemu-server/99.conf').write_text(f'memory: 16384\nballoon: {floor}\n')
            (root/'etc/pve/qemu-server/98.conf').write_text(
                f'memory: 240000\nballoon: {n98_floor}\n' if big else 'memory: 20000\nballoon: 10000\n')
            for v in (98, 99):
                (root/f'var/run/qemu-server/{v}.pid').write_text('1')
            (root/'var/lib/neuravps-balloon/floors.json').write_text('{"99":4915,"98":230000}')
            (root/'var/lib/neuravps-balloon/samples.tsv').write_text(
                f'99\t100\t{now-60}\t{idle}\t0\t0\t0\t0\n98\t100\t{now-60}\t0\t0\t0\t0\t0\n')
            f99 = 100 + faults_ps * 60
            qm = root/'bin/qm'
            qm.write_text(f"""#!/bin/sh
if [ "$1" = monitor ]; then
 if [ "$2" = 99 ]; then echo 'balloon: actual={actual} major_page_faults={f99} last_update={now}'
 else echo 'balloon: actual=240000 major_page_faults=100 last_update={now}'; fi
else
 echo "$*" >> '{root}/qm-writes'
fi
""")
            qm.chmod(0o755)
            logger = root/'bin/logger'; logger.write_text(f'#!/bin/sh\necho "$*" >> {root}/log\n'); logger.chmod(0o755)
            run = root/'reconcile.sh'; run.write_text(script)
            env = {**os.environ, 'PATH': str(root/'bin')+':'+os.environ['PATH'], 'ENTITLE_ENABLED': str(entitle)}
            subprocess.run(['bash', str(run)], env=env, capture_output=True, text=True, check=True)
            writes = (root/'qm-writes').read_text() if (root/'qm-writes').exists() else ''
            status = json.loads((root/'var/run/neuravps-balloon-status.json').read_text())
            pend = root/'var/lib/neuravps-balloon/pending-boosts.json'
            self.pending = json.loads(pend.read_text()) if pend.exists() else {}
            log = (root/'log').read_text() if (root/'log').exists() else ''
            return writes, status, log

    def test_paper_budget_blocks_without_entitlement(self):
        writes, status, log = self.run_node(0, 40000)
        self.assertEqual(writes, '')
        self.assertIn('NO BUDGET', log)
        self.assertEqual(status['entitle_enabled'], 0)

    def test_entitled_raise_goes_straight_to_memory_minus_3g(self):
        writes, status, log = self.run_node(1, 40000)
        self.assertEqual(writes.strip(), 'set 99 --balloon 13312')
        self.assertEqual(status['entitle_raises'], 1)
        self.assertEqual(self.pending['rec-99']['mb'], 13312 - 4915)
        self.assertIn('ENTITLEMENT over paper budget', log)

    def test_entitlement_needs_real_free_ram(self):
        writes, _, log = self.run_node(1, 28000)   # < 28672 available
        self.assertEqual(writes, '')
        self.assertIn('NO BUDGET', log)

    def test_entitlement_absolute_cap(self):
        # RAM 262144 -> cap 249856. 236000+4915+8397 = 249312 fits;
        # 239000+4915+8397 = 252312 does not (and still logs NO BUDGET).
        writes, _, _ = self.run_node(1, 60000, n98_floor=236000)
        self.assertEqual(writes.strip(), 'set 99 --balloon 13312')
        writes, _, log = self.run_node(1, 60000, n98_floor=239000)
        self.assertEqual(writes, '')
        self.assertIn('NO BUDGET', log)

    def test_never_above_entitlement_over_budget(self):
        writes, _, _ = self.run_node(1, 40000, floor=13312, actual=13312)
        self.assertEqual(writes, '')

    def test_entitled_floor_can_decay_on_over_budget_node(self):
        writes, status, log = self.run_node(1, 40000, floor=13312, faults_ps=0, idle=5000, actual=16384)
        self.assertEqual(writes.strip(), 'set 99 --balloon 9216')
        self.assertEqual(status['entitle_lowers'], 1)
        self.assertIn('via entitlement band', log)

    def test_floor_at_max_can_step_into_the_band(self):
        # an older full boost (16384) over budget: one 4G step lands at 12288,
        # inside the band (<= 13312), so it may start
        writes, status, _ = self.run_node(1, 40000, floor=16384, faults_ps=0, idle=5000, actual=16384)
        self.assertEqual(writes.strip(), 'set 99 --balloon 12288')
        self.assertEqual(status['entitle_lowers'], 1)

    def test_rounding_gap_does_not_lower(self):
        writes, _, _ = self.run_node(0, 200000, floor=8398, faults_ps=0, idle=5000, actual=16384, big=False)
        self.assertEqual(writes, '')

    def test_decay_still_blocked_over_budget_without_entitlement(self):
        writes, _, _ = self.run_node(0, 40000, floor=13312, faults_ps=0, idle=5000, actual=16384)
        self.assertEqual(writes, '')


class DefragSqxOverGate(unittest.TestCase):
    """2026-11-27: the origin over-gate for SQX floors is configurable so the
    entitlement band is not undone nightly; destinations keep FLOOR."""
    def test_default_and_validation(self):
        self.assertEqual(df.sqx_floor_over_gate({}), df.FLOOR)
        self.assertEqual(df.sqx_floor_over_gate({'sqxFloorOverGatePct': 0.96}), 0.96)
        for bad in ('x', 1.2, 0.5, None):
            self.assertEqual(df.sqx_floor_over_gate({'sqxFloorOverGatePct': bad}), df.FLOOR)
        self.assertEqual(df.EXPECTED_FLOOR['mt'], 3.0)
        self.assertEqual(df.EXPECTED_FLOOR['mt-plus'], 6.0)


if __name__ == '__main__':
    unittest.main()
