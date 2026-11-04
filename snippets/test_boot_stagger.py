"""Memory-paced auto-start after a node reboot (opt-in, 2026-11-04).

Pilot 0000260-AX102-3-LTD (03/11): 8 vps-b started 5 s apart on a 125 GB host
took MemAvailable 105 -> 4.5 GB and 8.4 GB to swap. The pacing waits for
room for the next guest's full RAM + reserve, never forever."""
import importlib.util
import logging
from pathlib import Path

import pytest


@pytest.fixture
def m(monkeypatch):
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: False if str(p) == '/var/log/sync-dnat.log' else exists(p))
    monkeypatch.setattr(logging, 'FileHandler', lambda *a, **kw: logging.NullHandler())
    spec = importlib.util.spec_from_file_location('boot_stagger_test', Path(__file__).with_name('sync-dnat.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


CFG = {"reserve_mb": 12288, "min_gap_s": 30, "max_wait_s": 240, "total_max_s": 120}


def test_config_is_opt_in(m, tmp_path, monkeypatch):
    f = tmp_path / "stagger"
    monkeypatch.setattr(m, "BOOT_STAGGER_FILE", str(f))
    assert m._boot_stagger_config() is None                 # no file: old behaviour
    f.write_text("MEM_STAGGER=0\n")
    assert m._boot_stagger_config() is None
    f.write_text("# x\nMEM_STAGGER=1\nMAX_WAIT_S=120\nRESERVE_MB=junk\n")
    assert m._boot_stagger_config() == {"reserve_mb": 12288, "min_gap_s": 30,
                                        "max_wait_s": 120, "total_max_s": 120}


def test_room_and_gap_start_immediately(m):
    c = Clock()
    waited = m._wait_for_boot_memory(101, CFG, None, sleep=c.sleep, now=c.now,
                                     avail=lambda: 100_000, memory=lambda v: 19456)
    assert waited == 0


def test_waits_for_the_gap(m):
    c = Clock()
    waited = m._wait_for_boot_memory(101, CFG, c.t - 10, sleep=c.sleep, now=c.now,
                                     avail=lambda: 100_000, memory=lambda v: 19456)
    assert 20 <= waited <= 25


def test_waits_for_memory_then_goes(m):
    c = Clock()
    seq = iter([20_000] * 6 + [40_000] * 100)               # 19456 + 12288 = 31744 needed
    waited = m._wait_for_boot_memory(101, CFG, None, sleep=c.sleep, now=c.now,
                                     avail=lambda: next(seq), memory=lambda v: 19456)
    assert waited == 30


def test_never_waits_forever(m):
    c = Clock()
    waited = m._wait_for_boot_memory(101, CFG, None, sleep=c.sleep, now=c.now,
                                     avail=lambda: 1_000, memory=lambda v: 19456)
    assert waited == 240


def test_unreadable_meminfo_does_not_block(m):
    c = Clock()
    assert m._wait_for_boot_memory(101, CFG, None, sleep=c.sleep, now=c.now,
                                   avail=lambda: None, memory=lambda v: None) == 0


def test_reconcile_paces_only_with_the_file(m, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "get_all_vms", lambda: {101: "stopped", 102: "stopped"})
    monkeypatch.setattr(m, "_boot_history_crashloop", lambda: False)
    monkeypatch.setattr(m, "get_firestore_status", lambda v: "running")
    monkeypatch.setattr(m, "run", lambda cmd: calls.append(("run", cmd[-1])))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "_wait_for_boot_memory",
                        lambda vmid, cfg, last: calls.append(("wait", vmid)) or 0)
    monkeypatch.setattr(m, "_boot_stagger_config", lambda: None)
    m._node_boot_reconcile_locked()
    assert calls == [("run", "101"), ("run", "102")]
    calls.clear()
    monkeypatch.setattr(m, "_boot_stagger_config", lambda: dict(CFG))
    m._node_boot_reconcile_locked()
    assert calls == [("wait", 101), ("run", "101"), ("wait", 102), ("run", "102")]


def test_ten_vms_never_outlive_the_unit_timeout(m, monkeypatch):
    """Review 04/11: 10 VMs x (30 s gap / 240 s cap) blew past the unit's
    TimeoutStartSec=300 and systemd killed the reconcile half-way. The total
    budget must bound the whole loop, whatever memory does."""
    c = Clock()
    started = []
    vms = {100 + i: "stopped" for i in range(10)}
    monkeypatch.setattr(m, "get_all_vms", lambda: vms)
    monkeypatch.setattr(m, "_boot_history_crashloop", lambda: False)
    monkeypatch.setattr(m, "get_firestore_status", lambda v: "running")
    # qm start measured at a few seconds; 10 s here is pessimistic
    monkeypatch.setattr(m, "run", lambda cmd: (started.append(cmd[-1]), c.sleep(10)))
    monkeypatch.setattr(m.time, "sleep", c.sleep)
    monkeypatch.setattr(m.time, "monotonic", c.now)
    monkeypatch.setattr(m, "_mem_available_mb", lambda: 1_000)   # never enough
    monkeypatch.setattr(m, "_vm_memory_mb", lambda v: 19456)
    real = m._wait_for_boot_memory
    monkeypatch.setattr(m, "_wait_for_boot_memory",
                        lambda vmid, cfg, last: real(vmid, cfg, last, sleep=c.sleep, now=c.now,
                                                     avail=m._mem_available_mb,
                                                     memory=m._vm_memory_mb))
    for total, unit_timeout in ((120, 300), (1200, 1800)):
        started.clear()
        t0 = c.t
        monkeypatch.setattr(m, "_boot_stagger_config", lambda total=total: dict(CFG, total_max_s=total))
        m._node_boot_reconcile_locked()
        assert len(started) == 10
        assert c.t - t0 < unit_timeout - 30, (total, c.t - t0)
