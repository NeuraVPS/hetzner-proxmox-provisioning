"""`sync <vmid> <ipv6> node=<id>`: quien migra fija el nodo de la ruta.

Sin el parámetro, la vía con IPv6 explícita relee `nodeId` de Firestore, que
durante una migración aún nombra el nodo VIEJO (VM 243, 01/10/2026)."""
import importlib.util
import logging
from pathlib import Path

import pytest

IPV6 = '2a01:4f9:c01f:e::f3'


@pytest.fixture
def sync(tmp_path, monkeypatch):
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **k: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('sync_node_override_test', Path(__file__).with_name('sync-base-nat.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_DIR = tmp_path
    module.LOCK_FILE = tmp_path / '.sync.lock'
    module.STATE_FILE = tmp_path / 'state.json'
    module.IDENT_PREFIX = '2a01:4f9:c01f:e::/64'
    seen = {}
    monkeypatch.setattr(module, 'reconcile_vm_routes', lambda desired: seen.update(desired))
    for name in ('reconcile_dynamic_dnat_rules', 'reconcile_sin_internet',
                 'reconcile_egress_pools', 'reconcile_base_ipv6_policy'):
        monkeypatch.setattr(module, name, lambda *a: None)
    monkeypatch.setattr(module, 'firestore_server_for_vmid', lambda vmid: module._server_entry(
        IPV6, node_id='0000242-AX102-3-LTD', ipv4='10.64.0.243'))
    module._seen = seen
    return module


def run(sync, flags):
    sync.sync_single_vmid(243, sync._server_entry(IPV6), delete_only=False,
                          ipv6_override=True, flags_override=flags)
    return sync._seen[243]


def test_without_node_reads_firestore_node(sync):
    assert run(sync, None)['nodeId'] == '0000242-AX102-3-LTD'


def test_node_override_wins_over_stale_firestore(sync):
    ent = run(sync, {'node': '0000245-AX102-3-LTD'})
    assert ent['nodeId'] == '0000245-AX102-3-LTD'
    assert ent['ipv4'] == '10.64.0.243'  # the rest still comes from Firestore


def test_parse_node_flag(sync):
    assert sync._parse_flag_args(['node=0000245-AX102-3-LTD', 'rdp=1']) == {
        'node': '0000245-AX102-3-LTD', 'rdp': True}
    with pytest.raises(SystemExit) as e:
        sync._parse_flag_args(['node=../etc'])
    assert e.value.code == 2
