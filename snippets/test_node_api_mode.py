"""sync-dnat.py without the service account: same behaviour through the node API.

A local HTTP server plays the Cloud Function, so the real urllib path runs.
"""
import importlib.util
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TOKEN = "T" * 44


class Api:
    def __init__(self):
        self.calls = []
        self.headers = []
        self.replies = {}  # op -> list of (code, body); the last one repeats

    def ops(self):
        return [c["op"] for c in self.calls]


@pytest.fixture
def api():
    state = Api()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append(body)
            state.headers.append(self.headers["Authorization"])
            queue = state.replies.get(body["op"], [(200, {})])
            code, reply = queue.pop(0) if len(queue) > 1 else queue[0]
            self.send_response(code)
            if code == 302:
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/node_sync_api"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def sync(tmp_path, monkeypatch, api):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    monkeypatch.setenv("NEURAVPS_NODE_TOKEN_FILE", str(token))
    monkeypatch.setenv("NEURAVPS_NODE_API_URL", api.url)
    monkeypatch.setenv("FIREBASE_CREDENTIALS_FILE", str(tmp_path / "no-sa.json"))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: False if str(p) == '/var/log/sync-dnat.log' else exists(p))
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **kw: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('node_api_mode_test', Path(__file__).with_name('sync-dnat.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.NODE_NAME = "0000266-KDM-9755"
    m.VM_CONFIG_DIR = tmp_path
    (tmp_path / '227.conf').write_text('memory: 4096\n')
    m.run = lambda cmd: 'status: stopped'
    m._remember_status = MagicMock()
    m.time = MagicMock()
    m.ensure_firebase_initialized = MagicMock(side_effect=AssertionError("no SDK in API mode"))
    return m


def docs(status="running", n=1):
    return (200, {"servers": [{"id": f"s{i}", "status": status, "updateTime": f"t{i}"} for i in range(n)]})


def test_service_account_wins_over_token(sync, tmp_path, monkeypatch):
    assert sync.use_node_api()
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    monkeypatch.setenv("FIREBASE_CREDENTIALS_FILE", str(sa))
    assert not sync.use_node_api()


def test_no_token_means_previous_behaviour(sync, tmp_path):
    sync.NODE_TOKEN_FILE = tmp_path / "absent"
    assert not sync.use_node_api()


def test_status_write_carries_the_precondition(sync, api):
    api.replies["get_server"] = [docs()]
    sync.update_status_in_firestore(227, "stopped")
    assert api.calls == [
        {"op": "get_server", "vmid": 227},
        {"op": "set_status", "vmid": 227, "serverId": "s0", "updateTime": "t0", "status": "stopped"},
    ]
    assert set(api.headers) == {f"Bearer 0000266-KDM-9755.{TOKEN}"}
    sync._remember_status.assert_called_once_with(227, "stopped")


def test_conflict_rereads_and_retries(sync, api):
    api.replies["get_server"] = [docs()]
    api.replies["set_status"] = [(409, {}), (200, {})]
    sync.update_status_in_firestore(227, "stopped")
    assert api.ops() == ["get_server", "set_status"] * 2
    sync._remember_status.assert_called_once()


def test_gives_up_after_four_attempts_without_remembering(sync, api):
    api.replies["get_server"] = [(500, {})]
    sync.update_status_in_firestore(227, "stopped")
    assert api.ops() == ["get_server"] * 4
    sync._remember_status.assert_not_called()


def test_state_changing_between_read_and_write_is_not_published(sync, api):
    api.replies["get_server"] = [docs()]
    answers = iter(['status: stopped', 'status: running'])
    sync.run = lambda cmd: next(answers)
    sync.update_status_in_firestore(227, "stopped")
    assert api.ops() == ["get_server"]


def test_migrating_vm_makes_no_call(sync, api, tmp_path):
    (tmp_path / '227.conf').write_text('memory: 4096\nlock: migrate\n')
    sync.update_status_in_firestore(227, "stopped")
    assert api.calls == []


def test_reads_retry_transient_errors_only(sync, api):
    api.replies["get_server"] = [(503, {}), docs("stopped")]
    assert sync.get_firestore_status(227) == "stopped"
    assert len(api.calls) == 2
    api.calls.clear()
    api.replies["get_server"] = [(401, {})]
    assert sync.get_firestore_status(227) is None
    assert len(api.calls) == 1


def test_redirect_is_not_followed(sync, api):
    api.replies["get_node"] = [(302, {})]
    with pytest.raises(sync.NodeApiError, match="HTTP 302"):
        sync._node_api("get_node")


def test_bad_token_file_does_not_reach_the_log(sync, tmp_path, api):
    bad = tmp_path / "token"
    bad.write_text("secret-part\nsecond-line-of-a-very-long-token-value")
    with pytest.raises(sync.NodeApiError) as exc:
        sync._node_api("get_node")
    assert "secret-part" not in str(exc.value)
    assert api.calls == []


def test_set_server_ipv6_exit_semantics(sync, api):
    api.replies["set_ipv6"] = [(200, {"updated": 1, "servers": ["s0"]})]
    assert sync.set_server_ipv6(227, "2a01:4f9:c01f:e::e3") is True
    api.replies["set_ipv6"] = [(200, {"updated": 0, "servers": []})]
    assert sync.set_server_ipv6(227, "2a01:4f9:c01f:e::e3") is False
    api.replies["set_ipv6"] = [(403, {})]
    assert sync.set_server_ipv6(227, "::1") is False


def test_node_boot_stamp_and_finalize_start_vm(sync, api):
    api.replies["get_node"] = [(200, {"exists": True, "pendingPostBootAction": "start-vm",
                                      "maintenance": True, "max_vm": 1})]
    sync.get_all_vms = lambda: {227: "stopped"}
    sync.run = MagicMock(return_value="")
    sync.update_proxmox_node_last_boot_at()
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["node_booted", "get_node", "finalize_boot"]
    assert api.calls[-1] == {"op": "finalize_boot", "vmStarted": True}
    sync.run.assert_called_once_with(["qm", "start", "227"])


def test_nothing_pending_is_not_finalized(sync, api):
    api.replies["get_node"] = [(200, {"exists": True, "pendingPostBootAction": None,
                                      "maintenance": False, "max_vm": 28})]
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["get_node"]


def test_boot_reconcile_starts_what_should_be_running(sync, api, tmp_path):
    api.replies["get_server"] = [docs("running")]
    sync.get_all_vms = lambda: {227: "stopped"}
    sync.run = MagicMock(return_value="")
    sync._boot_history_crashloop = lambda: False
    sync.BOOT_STAGGER_FILE = str(tmp_path / "absent")
    sync._node_boot_reconcile_locked()
    sync.run.assert_called_once_with(["qm", "start", "227"])
    assert api.ops() == ["get_server"]
