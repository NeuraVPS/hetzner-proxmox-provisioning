"""cluster.fw writer: validation, Storage Box protocol, queue and node script.

No production I/O: Storage Box and SSH calls are faked; the node script runs
in a temp dir with stub `perl`/`pve-firewall`.
"""
import importlib.util
import logging
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

GOOD = textwrap.dedent("""\
    [OPTIONS]

    policy_in: DROP
    policy_forward: DROP
    policy_out: ACCEPT
    enable: 1

    [IPSET base]

    2001:db8:b0::/64 # BASE 0
    2001:db8:b1::/64 # BASE 1

    [IPSET hosts-ipv6]
    2001:db8:1::/64 # n1
    2001:db8:2::/64 # n2
    [IPSET vm-ident]

    2001:db8:ffff::/64 # ident

    [RULES]

    IN SSH(ACCEPT) -source +dc/base -log nolog
    IN SSH(ACCEPT) -source +dc/hosts-ipv6 -log nolog
    IN ACCEPT -source +dc/base -p gre -log nolog
    IN DHCPfwd(ACCEPT) -i vmbr0 -log nolog

    [group vm-no-internet]

    IN RDP(ACCEPT) -source +dc/base -log nolog
    IN DROP -log nolog
    OUT DROP -log nolog

    [group vm-public-ipv6]

    IN ACCEPT -source ::/0 -log nolog
    """)
# What the 06/09 overlapping writes left behind (shape of the real tail).
TAIL = textwrap.dedent("""\

    EPT) -source +dc/base -log nolog
    IN DROP -log nolog
    OUT DROP -log nolog

    [group vm-public-ipv6]

    IN ACCEPT -source ::/0 -log nolog
    """)
NODES = {"n1": "2001:db8:1::2", "n2": "2001:db8:2::2"}
BASES = ["2001:db8:b0::2", "2001:db8:b1::2"]


@pytest.fixture
def sync(tmp_path, monkeypatch):
    original_exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: False if str(p) == '/var/log/sync-base-nat.log' else original_exists(p))
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **kw: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('sync_fw_test', Path(__file__).with_name('sync-base-nat.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_DIR = tmp_path
    module.PVE_NODES_STATE_FILE = tmp_path / 'pve_nodes.json'
    module.FW_SYNC_PENDING = tmp_path / 'fw-sync.pending'
    module.FW_SYNC_LOCK = tmp_path / '.fw-sync.lock'
    module._fw_alert = lambda subject, body: module._alerts.append((subject, body))
    module._alerts = []
    return module


# --- validation ---------------------------------------------------------------

def test_good_file_passes(sync):
    assert sync.validate_cluster_fw(GOOD, 2, BASES, GOOD) == []


def test_tail_of_overlapping_write_is_rejected(sync):
    errors = sync.validate_cluster_fw(GOOD + TAIL, 2, BASES)
    assert any("EPT)" in e for e in errors)
    assert any("repeated" in e for e in errors)


@pytest.mark.parametrize("mutate, needle", [
    (lambda t: t.replace("[RULES]", "[RULEZ]"), "bad section header"),
    (lambda t: t.split("[RULES]")[0], "missing section [RULES]"),
    (lambda t: t.replace("IN SSH(ACCEPT) -source +dc/base -log nolog\n", ""), "lose SSH"),
    (lambda t: t.replace("enable: 1", "enable: 0"), "enable"),
    (lambda t: t.replace("2001:db8:b1::/64 # BASE 1\n", ""), "2001:db8:b1::2"),
    (lambda t: t.replace("2001:db8:2::/64 # n2\n", ""), "expected 2"),
    (lambda t: t[:-5], "newline"),
    (lambda t: "garbage\n" + t, "outside any section"),
    (lambda t: t.replace("2001:db8:ffff::/64", "2001:db8:zzzz::/64"), "IPSET vm-ident"),
])
def test_broken_files_are_rejected(sync, mutate, needle):
    errors = sync.validate_cluster_fw(mutate(GOOD), 2, BASES)
    assert any(needle in e for e in errors), errors


@pytest.mark.parametrize("mutate, needle", [
    # A DROP before the lifeline makes it dead even though it is present.
    (lambda t: t.replace("[RULES]\n\n", "[RULES]\n\nIN DROP -log nolog\n"), "before the SSH lifeline"),
    (lambda t: t.replace("[RULES]\n\n", "[RULES]\n\nGROUP vm-no-internet\n"), "before the SSH lifeline"),
    # A narrowed lifeline is not a lifeline.
    (lambda t: t.replace("IN SSH(ACCEPT) -source +dc/base -log nolog", "IN SSH(ACCEPT) -source +dc/base -dest 2001:db8::1 -log nolog"), "lose SSH"),
    # PVE lowercases ipset names and resets on a repeat: a second [IPSET BASE] wipes the first.
    (lambda t: t + "\n[IPSET BASE]\n\n2001:db8:dead::/64\n", "repeated"),
    (lambda t: t.replace("[IPSET base]", "[ipset Base]") + "\n[IPSET base]\n2001:db8:b0::/64\n", "repeated"),
    # nomatch entries never admit.
    (lambda t: t.replace("2001:db8:b1::/64 # BASE 1", "!2001:db8:b1::/64 # BASE 1"), "2001:db8:b1::2"),
    (lambda t: t.replace("2001:db8:ffff::/64", "::/0"), "IPSET vm-ident"),
])
def test_lifeline_must_be_effective(sync, mutate, needle):
    errors = sync.validate_cluster_fw(mutate(GOOD), 2, BASES)
    assert any(needle in e for e in errors), errors


def test_missing_base_inventory_is_an_error(sync):
    assert any("BASE_HOSTS" in e for e in sync.validate_cluster_fw(GOOD, 2, []))


def test_lowercase_headers_like_pve_accepts(sync):
    lower = GOOD.replace("[OPTIONS]", "[options]").replace("[RULES]", "[rules]")
    assert sync.validate_cluster_fw(lower, 2, BASES) == []


def test_large_shrink_against_previous_is_rejected(sync):
    prev = GOOD.replace("2001:db8:2::/64 # n2\n", "".join(f"2001:db8:{i:x}::/64 # x\n" for i in range(2, 20)))
    errors = sync.validate_cluster_fw(GOOD, 2, BASES, prev)
    assert any("shrink" in e for e in errors)


def test_replace_then_validate_roundtrip(sync):
    nodes = dict(NODES, n3="2001:db8:3::2")
    new = sync.replace_hosts_ipv6_section(GOOD, nodes)
    assert sync.validate_cluster_fw(new, 3, BASES, GOOD) == []
    assert "2001:db8:3::/64 # n3" in new


# --- Storage Box protocol -----------------------------------------------------

class FakeBox:
    def __init__(self, content):
        self.files = {"/home/firewall/cluster.fw": content}
        self.dirs = set()
        self.owners = set()
        self.calls = []

    def ssh(self, args, timeout=60):
        self.calls.append(args)
        cmd, rest = args[0], args[1:]
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if cmd == "mkdir":
            if rest[0] in self.dirs:
                return subprocess.CompletedProcess(args, 1, "", "mkdir: cannot create directory: File exists")
            self.dirs.add(rest[0])
        elif cmd == "rm":
            target = rest[-1]
            self.dirs.discard(target)
            self.owners = {o for o in self.owners if not o.startswith(target + "/")}
            self.files.pop(target, None)
        elif cmd == "sha256sum":
            import hashlib
            return subprocess.CompletedProcess(args, 0, hashlib.sha256(self.files[rest[0]].encode()).hexdigest() + "  x\n", "")
        elif cmd == "mv":
            self.files[rest[1]] = self.files.pop(rest[0])
        elif cmd == "stat":
            return subprocess.CompletedProcess(args, 0, "0\n", "")
        elif cmd == "ls":
            if getattr(self, "ls_override", None) is not None:
                return subprocess.CompletedProcess(args, 0, "\n".join(self.ls_override) + "\n", "")
            names = [k.rsplit("/", 1)[1] for k in self.owners if k.startswith(rest[0] + "/")]
            return subprocess.CompletedProcess(args, 0, "\n".join(names) + "\n", "")
        elif cmd == "touch":
            self.owners.add(rest[0])
        return ok

    def scp(self, src, dst):
        self.calls.append(["scp", src, dst])
        if ":" in src:
            Path(dst).write_text(self.files[src.split(":", 1)[1]])
        else:
            self.files[dst.split(":", 1)[1]] = Path(src).read_text()
        return subprocess.CompletedProcess([], 0, "", "")


@pytest.fixture
def box(sync, monkeypatch):
    fake = FakeBox(GOOD)
    monkeypatch.setattr(sync, "_fw_storage_ssh", fake.ssh)
    monkeypatch.setattr(sync, "_fw_scp", fake.scp)
    monkeypatch.setattr(sync, "_base_ipv6s_for_validation", lambda: BASES)
    return fake


def publish(sync, nodes):
    with sync.storage_box_fw_lock():
        return sync.publish_cluster_fw_locked(nodes)


def test_update_uploads_to_temp_and_renames(sync, box):
    nodes = dict(NODES, n3="2001:db8:3::2")
    text = publish(sync, nodes)
    assert box.files["/home/firewall/cluster.fw"] == text
    assert "2001:db8:3::/64 # n3" in text
    pushes = [c for c in box.calls if c[0] == "scp" and ":" in c[2]]
    assert len(pushes) == 1 and ".tmp-" in pushes[0][2]
    assert ["mv"] == [c[0] for c in box.calls if c[0] == "mv"]
    assert not box.dirs, "lock released"
    assert [k for k in box.files if ".tmp-" in k] == []


def test_invalid_canonical_is_left_untouched(sync, box):
    box.files["/home/firewall/cluster.fw"] = GOOD + TAIL
    with pytest.raises(ValueError):
        publish(sync, dict(NODES, n3="2001:db8:3::2"))
    assert box.files["/home/firewall/cluster.fw"] == GOOD + TAIL
    assert not any(c[0] == "mv" for c in box.calls)
    assert not box.dirs, "lock released on refusal"


def test_unchanged_does_not_upload(sync, box):
    publish(sync, NODES)
    assert not any(c[0] == "mv" for c in box.calls)


def test_stale_remote_lock_is_never_broken(sync, box):
    box.dirs.add(sync.FW_REMOTE_LOCK_DIR)  # stat() says mtime 0 -> very old
    with pytest.raises(sync.StorageBoxLockStale):
        publish(sync, dict(NODES, n3="2001:db8:3::2"))
    assert sync.FW_REMOTE_LOCK_DIR in box.dirs, "someone else's lock left alone"
    assert not any(c[:2] == ["rm", "-r"] for c in box.calls)
    assert box.files["/home/firewall/cluster.fw"] == GOOD


def test_fresh_remote_lock_waits_then_times_out(sync, box, monkeypatch):
    import time
    box.dirs.add(sync.FW_REMOTE_LOCK_DIR)
    orig = box.ssh
    monkeypatch.setattr(sync, "_fw_storage_ssh", lambda a, timeout=60: (
        subprocess.CompletedProcess(a, 0, f"{int(time.time())}\n", "") if a[0] == "stat" else orig(a)))
    monkeypatch.setattr(sync, "FW_REMOTE_LOCK_WAIT_S", 0)
    with pytest.raises(TimeoutError):
        publish(sync, dict(NODES, n3="2001:db8:3::2"))
    assert box.files["/home/firewall/cluster.fw"] == GOOD


def test_release_does_not_remove_a_lock_that_is_not_ours(sync, box):
    with sync.storage_box_fw_lock():
        box.dirs.discard(sync.FW_REMOTE_LOCK_DIR)  # someone removed ours...
        box.dirs.add(sync.FW_REMOTE_LOCK_DIR)       # ...and took a new one
        box.ls_override = ["otherbase-1-2"]
    assert sync.FW_REMOTE_LOCK_DIR in box.dirs


def test_whole_pass_runs_under_the_lock_and_reads_inventory_inside(sync, box, monkeypatch):
    seen = []
    monkeypatch.setattr(sync, "read_pve_nodes_state", lambda: seen.append(sync.FW_REMOTE_LOCK_DIR in box.dirs) or dict(NODES))
    monkeypatch.setattr(sync, "distribute_cluster_fw", lambda nodes, text: seen.append(sync.FW_REMOTE_LOCK_DIR in box.dirs) or {n: ("unchanged", "") for n in nodes})
    assert sync.run_cluster_fw_sync_once() is True
    assert seen == [True, True]
    assert not box.dirs


def test_refusal_alerts_and_fails(sync, box, monkeypatch):
    box.files["/home/firewall/cluster.fw"] = GOOD + TAIL
    monkeypatch.setattr(sync, "read_pve_nodes_state", lambda: dict(NODES))
    assert sync.run_cluster_fw_sync_once() is False
    assert sync._alerts and "rechazada" in sync._alerts[0][0]


# --- queue / coalescing -------------------------------------------------------

def test_worker_coalesces_requests(sync, monkeypatch):
    runs = []
    monkeypatch.setattr(sync, "run_cluster_fw_sync_once", lambda: runs.append(1) or True)
    sync.FW_SYNC_PENDING.write_text("1\n")
    assert sync.sync_nodes_sync_firewall_worker() == 0
    assert runs == [1]
    # A second worker started for the same burst finds nothing left to do.
    assert sync.sync_nodes_sync_firewall_worker() == 0
    assert runs == [1]


def test_request_arriving_mid_run_is_served(sync, monkeypatch):
    runs = []

    def run():
        runs.append(1)
        if len(runs) == 1:
            sync.FW_SYNC_PENDING.write_text("2\n")  # new request during the pass
        return True

    monkeypatch.setattr(sync, "run_cluster_fw_sync_once", run)
    sync.FW_SYNC_PENDING.write_text("1\n")
    assert sync.sync_nodes_sync_firewall_worker() == 0
    assert runs == [1, 1]


def test_failed_pass_schedules_spaced_retry(sync, monkeypatch):
    scheduled = []
    monkeypatch.setattr(sync, "run_cluster_fw_sync_once", lambda: False)
    monkeypatch.setattr(sync, "_fw_start_worker", lambda extra, delay_s=0: scheduled.append((extra, delay_s)) or "u")
    sync.FW_SYNC_PENDING.write_text("1\n")
    assert sync.sync_nodes_sync_firewall_worker() == 1
    assert scheduled == [(["--retry=1"], sync.FW_RETRY_BASE_S)]
    sync.FW_SYNC_PENDING.write_text("1\n")
    sync.sync_nodes_sync_firewall_worker(attempt=2)
    assert scheduled[-1] == (["--retry=3"], sync.FW_RETRY_BASE_S * 4)
    sync.FW_SYNC_PENDING.write_text("1\n")
    sync.sync_nodes_sync_firewall_worker(attempt=sync.FW_MAX_RETRIES)
    assert len(scheduled) == 2 and "sigue fallando" in sync._alerts[-1][0]


def test_alert_is_not_repeated(sync, monkeypatch, tmp_path):
    import importlib
    sent = []
    spec = importlib.util.spec_from_file_location('sync_fw_alert', Path(__file__).with_name('sync-base-nat.py'))
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    real.FW_ALERT_STATE = tmp_path / "alerts.json"
    env = tmp_path / "env"
    env.write_text("SMTP_PASSWORD=x\n")
    real.FW_ALERT_ENV_FILE = str(env)

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, *a): sent.append(a)
        def quit(self): pass

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    real._fw_alert("x", "same body")
    real._fw_alert("x", "same body")
    real._fw_alert("x", "other body")
    assert len(sent) == 2


def test_cli_request_returns_without_running(sync, monkeypatch):
    started = []
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: started.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(sync, "run_cluster_fw_sync_once", lambda: pytest.fail("must not run inline"))
    sync.sync_nodes_sync_firewall()
    assert sync.FW_SYNC_PENDING.exists()
    assert started[0][0] == "systemd-run" and started[0][-1] == "--worker"


# --- node apply script --------------------------------------------------------

@pytest.fixture
def node_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "pve-firewall").write_text(
        "#!/bin/sh\nif [ \"$1\" = status ]; then echo 'Status: enabled/running'; exit 0; fi\n"
        "[ -e \"$NV_FAIL_RESTART\" ] && exit 1\necho restarted >> \"$NV_LOG\"\n")
    (bindir / "perl").write_text("#!/bin/sh\nif grep -q '^EPT)' \"$2\"; then echo 'parser warning: line 9'; exit 3; fi\necho 'parsed rules=4'\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    target = tmp_path / "cluster.fw"
    target.write_text("old\n")
    return tmp_path, bindir, target


def _run_node(sync, node_env, text, digest=None):
    import hashlib
    tmp, bindir, target = node_env
    script = sync.node_fw_apply_script(digest or hashlib.sha256(text.encode()).hexdigest(), BASES)
    script = (script.replace("/etc/pve/firewall/cluster.fw", str(target))
              .replace("/root/cluster.fw.prev", str(tmp / "prev"))
              .replace(sync.NODE_FW_APPLIED_MARK, str(tmp / "applied")))
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", NV_LOG=str(tmp / "log"),
               NV_FAIL_RESTART=str(tmp / "fail-restart"))
    return subprocess.run(["bash", "-c", script], input=text, capture_output=True, text=True, env=env)


def test_node_installs_valid_file(sync, node_env):
    tmp, _, target = node_env
    r = _run_node(sync, node_env, GOOD)
    assert "NVFW-UPDATED" in r.stdout and r.returncode == 0
    assert target.read_text() == GOOD
    assert (tmp / "prev").read_text() == "old\n"
    assert (tmp / "log").read_text() == "restarted\n"
    r = _run_node(sync, node_env, GOOD)
    assert "NVFW-UNCHANGED" in r.stdout
    assert (tmp / "log").read_text() == "restarted\n", "no restart when unchanged"


def test_failed_restart_is_retried_next_pass(sync, node_env):
    tmp, _, target = node_env
    (tmp / "fail-restart").write_text("1")
    r = _run_node(sync, node_env, GOOD)
    assert r.returncode == 23 and target.read_text() == GOOD
    (tmp / "fail-restart").unlink()
    r = _run_node(sync, node_env, GOOD)
    assert "NVFW-UPDATED" in r.stdout, "same bytes but not applied -> restart again"
    assert (tmp / "log").read_text() == "restarted\n"
    assert "NVFW-UNCHANGED" in _run_node(sync, node_env, GOOD).stdout


def test_node_gets_base_addresses(sync):
    script = sync.node_fw_apply_script("0" * 64, BASES)
    assert all(b in script for b in BASES)


def test_first_boot_carries_the_same_validator(sync):
    fb = (Path(__file__).parents[2] / "first_boot.sh").read_text()
    body = fb.split("<<'NVFWPL'\n", 1)[1].split("\nNVFWPL\n", 1)[0] + "\n"
    assert body == sync.NODE_FW_VALIDATOR_PL.lstrip("\n")
    assert "get /home/firewall/cluster.fw /etc/pve/firewall/cluster.fw" not in fb


def test_node_keeps_old_file_when_parser_warns(sync, node_env):
    _, _, target = node_env
    r = _run_node(sync, node_env, GOOD + TAIL)
    assert r.returncode == 21 and "NVFW-REJECT" in r.stdout
    assert target.read_text() == "old\n"


def test_node_keeps_old_file_on_truncated_transfer(sync, node_env):
    import hashlib
    _, _, target = node_env
    r = _run_node(sync, node_env, GOOD[:200], digest=hashlib.sha256(GOOD.encode()).hexdigest())
    assert r.returncode == 20 and "checksum" in r.stdout
    assert target.read_text() == "old\n"


def test_node_validator_matches_real_pve_parser_contract(sync):
    # The perl gate must refuse warnings and missing lifelines; keep the
    # strings the base keys on in sync with the script.
    assert "load_clusterfw_conf" in sync.NODE_FW_VALIDATOR_PL
    assert "+dc/base" in sync.NODE_FW_VALIDATOR_PL
