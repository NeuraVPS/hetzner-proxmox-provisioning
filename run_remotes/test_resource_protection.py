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

    def run_boost(self, available=30000, floors=100000, pending=0):
        from io import StringIO
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(guard,'STATE_DIR',Path(directory)), \
             patch('builtins.open',return_value=MagicMock()), \
             patch.object(guard.fcntl,'flock'), \
             patch.object(guard,'inspect',return_value=(19456,9932,12000,available)), \
             patch.object(guard,'meminfo',return_value={'MemTotal':257000,'MemAvailable':available}), \
             patch.object(guard,'floor_total',return_value=floors), \
             patch.object(guard.subprocess,'run') as qm, \
             patch.object(guard,'qmp') as qmp:
            if pending:
                (Path(directory)/'pending-boosts.json').write_text(json.dumps({'other':{'mb':pending,'until':1e12}}))
            result=guard.boost(99,'node-AX162')
            return result, qm.call_count, qmp.call_args

    def test_grant_rechecks_physical_reserve_and_floor_budget(self):
        for kwargs in ({'available':19000},{'floors':230000},{'pending':20000}):
            result,calls,_=self.run_boost(**kwargs)
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(calls,0)

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


if __name__ == '__main__':
    unittest.main()
