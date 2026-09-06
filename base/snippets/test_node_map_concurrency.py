"""Exercise node inventory updates with concurrent processes; no production I/O."""
import importlib.util
import json
import logging
import multiprocessing
import time
from pathlib import Path

import pytest


@pytest.fixture
def sync(tmp_path, monkeypatch):
    # Import the real module, redirecting its import-time log/config reads.
    original_exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: False if str(p) == '/var/log/sync-base-nat.log' else original_exists(p))
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **kw: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('sync_nodes_test', Path(__file__).with_name('sync-base-nat.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_DIR = tmp_path
    module.PVE_NODES_STATE_FILE = tmp_path / 'pve_nodes.json'
    module.sync_nodes_apply_state = module.write_pve_nodes_state
    module.ensure_firebase = lambda: True
    module.firestore_ip_for_proxmox_node = lambda nid: '2001:db8::2'
    return module


def test_parallel_registrations_keep_every_node(sync):
    ctx = multiprocessing.get_context('fork')
    start = ctx.Event()
    original_read = sync.read_pve_nodes_state

    def slow_read():
        result = original_read()
        time.sleep(0.03)
        return result

    sync.read_pve_nodes_state = slow_read

    def add(number):
        start.wait()
        if number % 2:
            sync.sync_nodes_single(str(number))
        else:
            sync.sync_nodes_add(str(number), '2001:db8::2')

    children = [ctx.Process(target=add, args=(n,)) for n in range(8)]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    assert set(original_read()) == {str(n) for n in range(8)}


@pytest.mark.parametrize('operation', ['full', 'single', 'add', 'del'])
def test_all_updates_wait_for_inventory_lock(sync, operation):
    import fcntl
    ctx = multiprocessing.get_context('fork')
    entered = ctx.Event()
    sync.firestore_list_proxmox_nodes = lambda: {'a': '2001:db8::1'}
    sync.sync_nodes_apply_state = lambda state: entered.set()
    calls = {
        'full': lambda: sync.sync_nodes_full(),
        'single': lambda: sync.sync_nodes_single('a'),
        'add': lambda: sync.sync_nodes_add('a', '2001:db8::1'),
        'del': lambda: sync.sync_nodes_del('a'),
    }
    # Parent locks after fork, avoiding inheritance of its open lock descriptor.
    ready = ctx.Event()
    child = ctx.Process(target=lambda: (ready.wait(), calls[operation]()))
    child.start()
    with (sync.STATE_DIR / '.pve-nodes.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready.set()
        assert not entered.wait(0.15)
    assert entered.wait(3)
    child.join(3)
    assert child.exitcode == 0


def test_failed_atomic_replace_preserves_previous_generation(sync, monkeypatch):
    sync.write_pve_nodes_state({'old': '2001:db8::1'})

    def failed_replace(source, target):
        assert json.loads(Path(source).read_text()) == {'new': '2001:db8::2'}
        assert json.loads(Path(target).read_text()) == {'old': '2001:db8::1'}
        raise OSError('simulated replacement failure')

    monkeypatch.setattr(sync.os, 'replace', failed_replace)
    with pytest.raises(OSError):
        sync.write_pve_nodes_state({'new': '2001:db8::2'})
    assert sync.read_pve_nodes_state() == {'old': '2001:db8::1'}
    assert not list(sync.STATE_DIR.glob('.pve-nodes-*'))
