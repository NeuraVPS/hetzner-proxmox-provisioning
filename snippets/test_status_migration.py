"""A live migration must not publish the source's transient stopped state."""
import importlib.util
import logging
from pathlib import Path
from unittest.mock import MagicMock
import pytest


@pytest.fixture
def sync(tmp_path, monkeypatch):
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: False if str(p) == '/var/log/sync-dnat.log' else exists(p))
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **kw: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('status_migration_test', Path(__file__).with_name('sync-dnat.py'))
    m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.VM_CONFIG_DIR = tmp_path
    m.ensure_firebase_initialized = lambda: True
    m.run = lambda cmd: 'status: stopped'
    m._remember_status = MagicMock()
    m.time.sleep = lambda seconds: None
    db = MagicMock()
    m.firestore = MagicMock()
    m.firestore.client.return_value = db
    doc = MagicMock(id='server', update_time='generation-1')
    m._query_local_server_doc = lambda db, vmid: [doc]
    return m, db, doc


@pytest.mark.parametrize('lock', ['migrate'])
def test_migration_state_never_reaches_firestore(sync, lock):
    m, db, _ = sync
    (m.VM_CONFIG_DIR/'227.conf').write_text(f'memory: 4096\nlock: {lock}\n')
    m.update_status_in_firestore(227, 'stopped')
    db.collection.assert_not_called()
    m._remember_status.assert_not_called()


def test_departed_vm_never_changes_firestore(sync):
    m, db, _ = sync
    m.update_status_in_firestore(227, 'stopped')
    db.collection.assert_not_called()


def test_stale_sample_is_discarded(sync):
    m, db, _ = sync
    (m.VM_CONFIG_DIR/'227.conf').write_text('memory: 4096\n')
    m.run = lambda cmd: 'status: running'
    m.update_status_in_firestore(227, 'stopped')
    db.collection.assert_not_called()


def test_real_shutdown_is_written_with_ownership_precondition(sync):
    m, db, doc = sync
    (m.VM_CONFIG_DIR/'227.conf').write_text('memory: 4096\n')
    m.update_status_in_firestore(227, 'stopped')
    db.write_option.assert_called_once_with(last_update_time=doc.update_time)
    db.collection().document().update.assert_called_once_with(
        {'status':'stopped', 'lastStatusUpdate':m.firestore.SERVER_TIMESTAMP},
        option=db.write_option.return_value)
    m._remember_status.assert_called_once_with(227, 'stopped')


def test_migration_starting_during_query_is_discarded(sync):
    m, db, doc = sync
    config=m.VM_CONFIG_DIR/'227.conf';config.write_text('memory: 4096\n')
    def query(db, vmid):
        config.write_text('memory: 4096\nlock: migrate\n')
        return [doc]
    m._query_local_server_doc=query
    m.update_status_in_firestore(227, 'stopped')
    db.collection.assert_not_called()
    m._remember_status.assert_not_called()


def test_ownership_change_retries_query_without_caching_failed_write(sync):
    m, db, doc = sync
    (m.VM_CONFIG_DIR/'227.conf').write_text('memory: 4096\n')
    m._query_local_server_doc=MagicMock(side_effect=[[doc], []])
    db.collection().document().update.side_effect=RuntimeError('precondition failed: node changed')
    m.update_status_in_firestore(227, 'stopped')
    assert m._query_local_server_doc.call_count==2
    m._remember_status.assert_not_called()


def test_normal_creation_still_reports_a_running_guest(sync):
    m, db, _ = sync
    (m.VM_CONFIG_DIR/'227.conf').write_text('memory: 4096\nlock: create\n')
    m.run = lambda cmd: 'status: running'
    m.update_status_in_firestore(227, 'running')
    m._remember_status.assert_called_once_with(227, 'running')
