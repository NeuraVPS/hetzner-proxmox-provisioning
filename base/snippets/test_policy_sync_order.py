"""Verify policy snapshots cannot overtake a completed firewall revocation."""
import importlib.util
import logging
import multiprocessing
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def sync(tmp_path, monkeypatch):
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **k: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('sync_policy_test', Path(__file__).with_name('sync-base-nat.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_DIR = tmp_path
    module.LOCK_FILE = tmp_path / '.sync.lock'
    module.STATE_FILE = tmp_path / 'state.json'
    for name in ('reconcile_dynamic_dnat_rules', 'reconcile_vm_routes',
                 'reconcile_sin_internet', 'reconcile_egress_pools'):
        monkeypatch.setattr(module, name, lambda *a: None)
    monkeypatch.setattr(module, '_egress_maps_exist', lambda: True)
    monkeypatch.setattr(module, 'load_egress_config', lambda: {})
    return module


def test_full_sync_reads_firewall_after_waiting_for_earlier_toggle(sync):
    ctx = multiprocessing.get_context('fork')
    go = ctx.Event()
    read = ctx.Event()
    applied = ctx.Queue()
    current_enabled = ctx.Value('b', True)

    def read_servers():
        read.set()
        return {100: sync._server_entry('2a01:4f9:c01f:e::64',
                                      ipv6_enabled=bool(current_enabled.value))}

    sync.firestore_list_configured_servers = read_servers
    sync.reconcile_base_ipv6_policy = lambda desired: applied.put(desired[100]['ipv6Enabled'])
    child = ctx.Process(target=lambda: (go.wait(), sync.sync_full()))
    child.start()  # Fork before opening the parent's lock descriptor.
    try:
        with sync.state_lock():
            go.set()
            assert not read.wait(.2), 'Full sync read a stale snapshot before acquiring the lock'
            current_enabled.value = False  # Earlier per-VM revocation committed.
        assert applied.get(timeout=5) is False
        child.join(5)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)


def test_required_policy_refuses_disabled_base_before_mutating(sync, monkeypatch):
    monkeypatch.setenv('BASE_IPV6_POLICY_ENABLED', '0')
    with pytest.raises(RuntimeError, match='disabled'):
        sync.require_base_ipv6_policy()


def test_required_policy_refuses_missing_live_table(sync, monkeypatch):
    monkeypatch.setenv('BASE_IPV6_POLICY_ENABLED', '1')
    monkeypatch.setattr(sync.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match='running ruleset'):
        sync.require_base_ipv6_policy()


def test_required_policy_accepts_installed_table(sync, monkeypatch):
    monkeypatch.setenv('BASE_IPV6_POLICY_ENABLED', '1')
    monkeypatch.setattr(sync.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    sync.require_base_ipv6_policy()


def test_required_smb_policy_refuses_feature_disabled_before_reconcile(sync, monkeypatch):
    monkeypatch.setenv('BASE_SMB_POLICY_ENABLED', '0')
    with pytest.raises(RuntimeError, match='SMB policy is disabled'):
        sync.require_base_smb_policy(live=False)


def test_required_smb_policy_requires_current_receipt_and_live_table(sync, monkeypatch, tmp_path):
    fake = types.ModuleType('base_smb_policy')
    fake.RUNTIME_SCHEMA_VERSION = 2
    fake.installed_mode_from_runtime = lambda _receipt: 'enforce'
    fake._receipt_runtime_schema = lambda _receipt: 2
    monkeypatch.setitem(sys.modules, 'base_smb_policy', fake)
    monkeypatch.setenv('BASE_SMB_POLICY_ENABLED', '1')
    sync.require_base_smb_policy()

    fake.installed_mode_from_runtime = lambda _receipt: None
    with pytest.raises(RuntimeError, match='not installed'):
        sync.require_base_smb_policy()


def test_sync_policy_required_flag_checks_before_and_after_reconcile(sync, monkeypatch):
    calls = []
    monkeypatch.setattr(sync, 'require_base_smb_policy', lambda *, live=True: calls.append(('require', live)))
    monkeypatch.setattr(sync, 'reconcile_base_smb_policy', lambda required=False: calls.append(('reconcile', required)))
    monkeypatch.setattr(sync, 'state_lock', __import__('contextlib').nullcontext)
    monkeypatch.setattr(sys, 'argv', ['sync-base-nat.py', 'sync', 'policy', '--require-smb-policy'])
    sync.main()
    assert calls == [('require', False), ('reconcile', True)]
