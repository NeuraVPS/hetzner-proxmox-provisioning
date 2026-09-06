"""Cooldown history merges concurrent writers without exposing partial JSON."""
import importlib.util
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('defrag_recent',Path(__file__).with_name('neuravps-defrag.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def writer(path, start, gate):
    m.RECENT_FILE=path
    gate.wait()
    for vmid in range(start,start+8):
        if not m.record_recent_moves([vmid]):raise RuntimeError('write failed')


class RecentMovesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'recent.json'
        self.old=m.RECENT_FILE;m.RECENT_FILE=str(self.path)
        self.logs=patch.object(m,'log');self.logs.start()

    def tearDown(self):
        m.RECENT_FILE=self.old;self.logs.stop();self.temp.cleanup()

    def test_first_write_and_retention_preserve_unrelated_recent_vm(self):
        now=time.time()
        self.path.write_text(json.dumps({'1':now-3600,'2':now-31*86400,'3':'invalid'}))
        self.assertTrue(m.record_recent_moves([4]))
        self.assertEqual(m.load_recent_moves(72),{1,4})

    def test_missing_file_initializes_successfully(self):
        self.assertTrue(m.record_recent_moves([9]))
        self.assertEqual(m.load_recent_moves(72),{9})

    def test_corrupt_existing_file_is_preserved(self):
        self.path.write_text('{invalid')
        self.assertFalse(m.record_recent_moves([9]))
        self.assertEqual(self.path.read_text(),'{invalid')

    def test_publish_failure_keeps_old_history_and_cleans_owned_temp(self):
        self.path.write_text(json.dumps({'1':time.time()}))
        original=self.path.read_bytes()
        with patch.object(m.os,'replace',side_effect=OSError('read-only filesystem')):
            self.assertFalse(m.record_recent_moves([9]))
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(list(self.path.parent.glob('.recent-moves-*')),[])

    def test_parallel_writers_keep_every_move_and_readers_always_see_seed(self):
        self.assertTrue(m.record_recent_moves([1]))
        ctx=multiprocessing.get_context('fork');gate=ctx.Event()
        starts=[100,200,300,400]
        workers=[ctx.Process(target=writer,args=(str(self.path),n,gate)) for n in starts]
        for p in workers:p.start()
        gate.set()
        while any(p.is_alive() for p in workers):
            self.assertIn(1,m.load_recent_moves(72))
            time.sleep(.001)
        for p in workers:
            p.join(5);self.assertEqual(p.exitcode,0)
        self.assertEqual(m.load_recent_moves(72),{1}|{v for n in starts for v in range(n,n+8)})


if __name__=='__main__':unittest.main()
