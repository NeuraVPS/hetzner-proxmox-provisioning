"""sync-dnat.py without the service account: same behaviour through the node API.

A local HTTP server plays the Cloud Function, so the real urllib path runs.
"""
import importlib.util
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

TOKEN = "T" * 44


class Api:
    def __init__(self):
        self.calls = []
        self.headers = []
        self.replies = {}  # op -> list of (code, body); the last one repeats
        self.delay = 0.0

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
            if state.delay:
                time.sleep(state.delay)
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
    m._remember_status = MagicMock(wraps=m._remember_status)
    # No real waiting and no jitter: the clock only advances when the script sleeps.
    m.clock = [1000.0]
    m.slept = []
    def sleep(seconds):
        m.slept.append(seconds)
        m.clock[0] += seconds
    m.time = SimpleNamespace(sleep=sleep, monotonic=lambda: m.clock[0], time=time.time,
                             time_ns=time.time_ns)
    m.random = SimpleNamespace(uniform=lambda a, b: 0.0)
    m._STATUS_CACHE = tmp_path / 'cache.json'
    m.BOOT_FINALIZE_FILE = tmp_path / 'boot_finalize.json'
    m.BOOT_RECONCILE_LOCK = str(tmp_path / 'reconcile.lock')
    m.BOOT_STAGGER_FILE = str(tmp_path / 'absent')
    m._boot_history_crashloop = lambda: False
    m._node_uptime_sec = lambda: 3600.0
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


# --- hooks -------------------------------------------------------------------
def test_hook_leaves_at_once_on_rejected_token(sync, api):
    for code in (401, 403):
        api.calls.clear()
        api.replies["get_server"] = [(code, {})]
        assert sync.update_status_in_firestore(227, "stopped") is False
        assert api.ops() == ["get_server"] and sync.slept == []


def test_hook_stops_when_its_budget_is_spent(sync, api):
    api.replies["get_server"] = [(503, {})]
    sync.HOOK_BUDGET_S = 3
    assert sync.update_status_in_firestore(227, "stopped") is False
    # 0.5 s + 1 s of backoff; the next one (2 s) would not fit.
    assert len(api.calls) == 3 and sync.slept == [0.5, 1.0]


def test_hook_has_a_hard_wall_clock_bound(sync, api):
    api.delay = 1.0
    sync.HOOK_BUDGET_S = 0.2
    api.replies["get_server"] = [docs()]
    began = time.monotonic()
    assert sync.update_status_in_firestore(227, "stopped") is False
    assert time.monotonic() - began < 0.9


def test_failed_write_leaves_the_vm_drifted_for_the_watch(sync, api):
    sync._save_status_cache({227: "stopped"})
    api.replies["get_server"] = [(401, {})]
    sync.update_status_in_firestore(227, "stopped")
    assert 227 not in sync._load_status_cache()


# --- node-boot ---------------------------------------------------------------
def host(sync, tmp_path, states):
    """VMs of the node: {vmid: running|stopped}; returns the qm start calls."""
    started = []
    for vmid in states:
        (tmp_path / f'{vmid}.conf').write_text('memory: 4096\n')

    def run(cmd):
        if cmd[:2] == ["qm", "start"]:
            states[int(cmd[2])] = "running"
            started.append(int(cmd[2]))
            return ""
        return f"status: {states[int(cmd[2])]}"
    sync.run = run
    sync.get_all_vms = lambda: dict(states)
    return started


def test_boot_starts_what_should_be_running(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_server"] = [docs("running")]
    sync.node_boot_reconcile()
    assert started == [227]


def test_function_down_at_boot_starts_nothing_and_stays_bounded(sync, api, tmp_path, caplog):
    started = host(sync, tmp_path, {vmid: "stopped" for vmid in range(227, 255)})
    api.replies["get_server"] = [(503, {})]
    real_sleep = sync.time.sleep
    # Each failed read costs 31.5 s of fake time, as with a black-holed endpoint.
    sync.time.sleep = lambda s: (real_sleep(s), sync.clock.__setitem__(0, sync.clock[0] + 10))
    sync.node_boot_reconcile()
    assert started == []
    assert len(api.calls) <= 15
    assert "will NOT be auto-started" in caplog.text


def test_watch_never_starts_a_vm_and_publishes_what_it_sees(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    sync._save_status_cache({227: "running"})
    api.replies["get_server"] = [docs("running")]
    sync.watch_statuses()
    assert started == [] and api.ops() == ["get_server", "set_status"]


# --- maintenance cycle -------------------------------------------------------
def node_reply(**kw):
    base = {"exists": True, "pendingPostBootAction": "start-vm", "maintenance": True,
            "maintenanceStartedAt": time.time() - 7200, "cycle": "c" * 32, "max_vm": 1}
    return (200, dict(base, **kw))


def test_post_boot_action_finalizes_the_cycle_it_saw(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [node_reply()]
    sync.update_proxmox_node_last_boot_at()
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["node_booted", "get_node", "finalize_boot"]
    assert api.calls[-1] == {"op": "finalize_boot", "cycle": "c" * 32, "vmStarted": True}
    assert started == [227] and not sync.BOOT_FINALIZE_FILE.exists()


@pytest.mark.parametrize("reply", [
    {"pendingPostBootAction": None, "maintenance": False, "cycle": None},  # nothing pending
    {"maintenanceStartedAt": time.time() - 60},  # opened after the boot (an hour ago)
    {"maintenanceStartedAt": None},
])
def test_cycle_that_this_boot_does_not_end_is_not_touched(sync, api, tmp_path, reply):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [node_reply(**reply)]
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["get_node"] and started == []


def test_unread_flags_are_never_decided_later(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [(503, {})]
    sync.handle_pending_post_boot_action()
    api.calls.clear()
    sync.watch_statuses()
    assert "get_node" not in api.ops() and started == []


def test_failed_finalize_is_only_rewritten_later(sync, api, tmp_path):
    states = {227: "stopped"}
    started = host(sync, tmp_path, states)
    api.replies["get_node"] = [node_reply()]
    api.replies["finalize_boot"] = [(503, {})]
    sync.handle_pending_post_boot_action()
    assert sync.BOOT_FINALIZE_FILE.exists()
    states[227] = "stopped"  # the user shut it down afterwards
    api.calls.clear()
    api.replies["finalize_boot"] = [(200, {"finalized": True})]
    sync._api_retry_finalize()
    assert api.calls == [{"op": "finalize_boot", "cycle": "c" * 32, "vmStarted": True}]
    assert started == [227] and not sync.BOOT_FINALIZE_FILE.exists()


def test_newer_cycle_is_left_alone(sync, api, tmp_path):
    host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [node_reply()]
    api.replies["finalize_boot"] = [(409, {})]
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["get_node", "finalize_boot"] and not sync.BOOT_FINALIZE_FILE.exists()


def test_finalize_retries_are_bounded(sync, api, tmp_path):
    host(sync, tmp_path, {227: "running"})
    api.replies["get_node"] = [node_reply()]
    api.replies["finalize_boot"] = [(401, {})]
    sync.handle_pending_post_boot_action()
    for _ in range(sync.FINALIZE_MAX_RETRIES + 5):
        sync._api_retry_finalize()
    assert api.ops().count("finalize_boot") == sync.FINALIZE_MAX_RETRIES + 1
    assert not sync.BOOT_FINALIZE_FILE.exists()
