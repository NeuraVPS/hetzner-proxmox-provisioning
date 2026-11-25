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
        self.by_vmid = {}  # vmid -> list of (code, body), takes precedence
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
            queue = state.by_vmid.get(body.get("vmid")) or state.replies.get(body["op"], [(200, {})])
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
    # A clock that only advances when the script sleeps or a test moves it.
    m.clock = [1000.0]
    m.slept = []
    def sleep(seconds):
        m.slept.append(seconds)
        m.clock[0] += seconds
    m.time = SimpleNamespace(sleep=sleep, monotonic=lambda: m.clock[0], time=time.time,
                             time_ns=time.time_ns)
    m.BOOT_PENDING_FILE = tmp_path / 'boot_pending.json'
    m.BOOT_PENDING_LOCK = str(tmp_path / 'pending.lock')
    m.BOOT_RECONCILE_LOCK = str(tmp_path / 'reconcile.lock')
    m.BOOT_STAGGER_FILE = str(tmp_path / 'absent')
    m._STATUS_CACHE = tmp_path / 'cache.json'
    m._boot_id = lambda: 'boot-1'
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
    assert 2 <= len(api.calls) < 4 and sum(sync.slept) < 3


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
def node_reply(**kw):
    base = {"exists": True, "pendingPostBootAction": "start-vm", "maintenance": True,
            "maintenanceStartedAt": 1.0, "cycle": "c" * 32, "max_vm": 1}
    return (200, dict(base, **kw))


def host(sync, tmp_path, states):
    """VMs of the node: {vmid: running|stopped}; qm start flips them."""
    started = []
    for vmid in states:
        (tmp_path / f'{vmid}.conf').write_text('memory: 4096\n')

    def run(cmd):
        if cmd[:2] == ["qm", "start"]:
            states[int(cmd[2])] = "running"
            started.append(int(cmd[2]))
            return ""
        if cmd[0] == "systemd-run":
            started.append("spawn")
            return ""
        return f"status: {states[int(cmd[2])]}"
    sync.run = run
    sync.get_all_vms = lambda: dict(states)
    return started


def boot(sync):
    sync._boot_budget = sync._ApiBudget(sync.NODE_BOOT_API_BUDGET_S)
    sync._boot_pending_init()
    sync.node_boot_reconcile()


def test_boot_starts_what_should_be_running_and_forgets_it(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_server"] = [docs("running")]
    boot(sync)
    assert started == [227]
    assert sync._boot_pending_vms() == set()


def test_no_doc_is_resolved_not_a_failure(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_server"] = [(200, {"servers": []})]
    boot(sync)
    assert started == [] and api.ops() == ["get_server"] and sync._boot_pending_vms() == set()


def test_failed_read_is_retried_within_the_same_node_boot(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped", 228: "stopped"})
    api.by_vmid[227] = [(503, {}), docs("running")]
    api.by_vmid[228] = [docs("running")]
    boot(sync)
    assert sorted(started) == [227, 228]
    assert sync._boot_pending_vms() == set()


def test_function_down_for_whole_boot(sync, api, tmp_path):
    states = {vmid: "stopped" for vmid in range(227, 255)}
    started = host(sync, tmp_path, states)
    api.replies["get_server"] = [(503, {})]
    boot(sync)
    assert started == [] and sync._boot_pending_vms() == set(states)
    assert len(api.calls) < 40 and sum(sync.slept) <= sync.NODE_BOOT_RETRY_WINDOW_S

    # The watch must not publish them as stopped, and hands them over.
    sync._save_status_cache({vmid: "running" for vmid in states})
    sync._node_uptime_sec = lambda: 3600.0
    api.calls.clear()
    sync.watch_statuses()
    assert started == ["spawn"] and api.calls == []
    sync.sync_all_statuses()
    assert api.calls == []
    sync._watch_should_stand_down.held.close()  # the watch process exits

    # The function is back: the retry starts them.
    started.clear()
    api.replies["get_server"] = [docs("running")]
    api.replies["get_node"] = [node_reply(pendingPostBootAction=None, maintenance=False)]
    sync.node_boot_retry()
    assert sorted(started) == sorted(states)
    assert sync._load_boot_state() is None


def test_rejected_token_stops_after_one_read(sync, api, tmp_path):
    host(sync, tmp_path, {227: "stopped", 228: "stopped"})
    api.replies["get_server"] = [(401, {})]
    boot(sync)
    assert len(api.calls) == 1 and sync._boot_pending_vms() == {227, 228}


def test_hook_settles_a_vm_and_watch_publishes_it_again(sync, api, tmp_path, monkeypatch):
    host(sync, tmp_path, {227: "stopped"})
    sync._boot_pending_init()
    monkeypatch.setattr(sync.sys, "argv", ["sync-dnat.py", "227", "post-stop"])
    sync.is_running_under_backup = lambda: False
    api.replies["get_server"] = [docs()]
    sync.main()
    assert sync._boot_pending_vms() == set()


def test_previous_boot_file_is_ignored(sync, api, tmp_path):
    host(sync, tmp_path, {227: "stopped"})
    sync._boot_pending_init()
    sync._boot_id = lambda: "boot-2"
    assert sync._boot_pending_vms() == set()


def test_reboot_loop_is_remembered_by_the_retry(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    sync._boot_history_crashloop = lambda: True
    api.replies["get_server"] = [(503, {})]
    boot(sync)
    api.replies["get_server"] = [docs("running")]
    api.replies["get_node"] = [node_reply(pendingPostBootAction=None, maintenance=False)]
    sync._boot_history_crashloop = MagicMock(side_effect=AssertionError("counted twice"))
    sync.node_boot_retry()
    assert started == [] and sync._load_boot_state() is None


def test_unwritable_state_file_still_starts_vms(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    (tmp_path / "file").write_text("")
    sync.BOOT_PENDING_FILE = tmp_path / "file" / "pending.json"
    api.replies["get_server"] = [docs("running")]
    boot(sync)
    assert started == [227]


# --- maintenance cycle -------------------------------------------------------
def test_post_boot_action_finalizes_the_cycle_it_saw(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [node_reply()]
    sync.update_proxmox_node_last_boot_at()
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["node_booted", "get_node", "finalize_boot"]
    assert api.calls[-1] == {"op": "finalize_boot", "cycle": "c" * 32, "vmStarted": True}
    assert started == [227]
    assert sync._load_boot_state()["action"] is None


def test_nothing_pending_is_not_finalized(sync, api, tmp_path):
    host(sync, tmp_path, {227: "running"})
    api.replies["get_node"] = [node_reply(pendingPostBootAction=None, maintenance=False, cycle=None)]
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["get_node"]


def test_failed_finalize_is_retried_without_starting_again(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [node_reply()]
    api.replies["finalize_boot"] = [(503, {})]
    sync.handle_pending_post_boot_action()
    assert api.ops().count("finalize_boot") == 3
    assert sync._load_boot_state()["action"]["finalize"]["vmStarted"] is True
    sync._write_boot_state(dict(sync._load_boot_state(), vms=[]))
    api.calls.clear()
    api.replies["finalize_boot"] = [(200, {"finalized": True})]
    sync.node_boot_retry()
    assert api.ops() == ["finalize_boot"] and started == [227]
    assert sync._load_boot_state() is None


def test_newer_cycle_is_left_alone(sync, api, tmp_path):
    host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [node_reply()]
    api.replies["finalize_boot"] = [(409, {})]
    sync.handle_pending_post_boot_action()
    assert api.ops() == ["get_node", "finalize_boot"]
    assert sync._load_boot_state()["action"] is None


def test_retry_ignores_a_cycle_opened_after_the_boot(sync, api, tmp_path):
    started = host(sync, tmp_path, {227: "stopped"})
    api.replies["get_node"] = [(503, {})]
    sync.handle_pending_post_boot_action()
    sync._write_boot_state(dict(sync._load_boot_state(), vms=[]))
    api.replies["get_node"] = [node_reply(maintenanceStartedAt=time.time() - 60)]
    api.calls.clear()
    sync.node_boot_retry()
    assert api.ops() == ["get_node"] and started == []


def test_maintenance_retries_are_bounded(sync, api, tmp_path, caplog):
    host(sync, tmp_path, {227: "running"})
    api.replies["get_node"] = [(401, {})]
    sync.handle_pending_post_boot_action()
    sync._write_boot_state(dict(sync._load_boot_state(), vms=[]))
    api.calls.clear()
    for _ in range(sync.ACTION_MAX_RETRIES + 5):
        sync.node_boot_retry()
    assert len(api.calls) == sync.ACTION_MAX_RETRIES
    assert sync._load_boot_state() is None
