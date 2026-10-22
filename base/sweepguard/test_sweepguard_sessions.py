"""Tests de la memoria de sesiones en el detector de diversidad de puertos y
de la memoria compartida b0<->b1 (17/10/2026).

    python3 -m pytest base/sweepguard/test_sweepguard_sessions.py -q
    python3 base/sweepguard/test_sweepguard_sessions.py      # sin pytest

Caso real: 90.249.134.23, un socio que entra en VPS de clientes, demostro una
sesion RDP en b0 a las 09:34Z y b1 lo bloqueo 24 h a las 10:04Z por «20
distinct ports». Los datos son sinteticos con la forma de `nft -j` (bf_seen) y
de /proc/net/nf_conntrack.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sweepguard", os.path.join(HERE, "sweepguard.py"))
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

CFG = dict(sg.DEFAULTS)
VIP = "77.42.49.79"
PAUL = "90.249.134.23"
SWEEPER = "203.0.113.50"
NOW = sg.time.time()


def seen(addr, n_machines, first=200):
    """Elementos de bf_seen como los da `nft -j`: RDP y SMB de cada maquina
    (cuentan como una sola, ver vm_slot)."""
    out = []
    for i in range(n_machines):
        for base in (20000, 10000):
            out.append({"elem": {"val": {"concat": [addr, base + first + i]},
                                 "timeout": 21600, "expires": 20000}})
    return out


def off(src, dport, proto="tcp", dst=VIP):
    num = 6 if proto == "tcp" else 17
    return (f"ipv4     2 {proto}      {num} src={src} dst={dst} sport=40001 dport={dport} "
            f"src=10.0.0.3 dst={dst} sport={dport} dport=40001 [OFFLOAD] mark=0 zone=0 use=3\n")


class Harness:
    """run_family/main con nft, conntrack, ssh y log simulados."""

    def __init__(self, bf_seen=(), conntrack=(), allow=(), ssh=None):
        self.sets = {"bf_seen": list(bf_seen), "bf_allow": list(allow), "bf_static": [],
                     "bf_auto": [], "nuestras4": [VIP]}
        self.conntrack, self.ssh = list(conntrack), ssh
        self.calls, self.logs = [], []

    def nft_json(self, args):
        name = args[-1]
        if args[:2] == ["list", "set"] and args[2] == "ip" and name in self.sets:
            return [{"set": {"name": name, "elem": self.sets[name]}}]
        return []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "ssh":
            if self.ssh is None:
                return subprocess.CompletedProcess(cmd, 255, b"", b"Permission denied (publickey).")
            if isinstance(self.ssh, Exception):
                raise self.ssh
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.ssh).encode(), b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def __enter__(self):
        fd, self.ct_path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.writelines(self.conntrack)
        self.saved = {k: getattr(sg, k) for k in
                      ("nft_json", "log", "CONNTRACK", "established_timeout")}
        self.saved_run = sg.subprocess.run
        sg.nft_json, sg.log, sg.CONNTRACK = self.nft_json, self.logs.append, self.ct_path
        sg.established_timeout = lambda: 432000
        sg.subprocess.run = self.run
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(sg, k, v)
        sg.subprocess.run = self.saved_run
        os.unlink(self.ct_path)

    def blocked(self):
        return [c[-1] for c in self.calls if c[0] == "nft" and "bf_auto" in c]


def run_family(sessions, cfg=CFG, **kw):
    with Harness(**kw) as h:
        n = sg.run_family("ip", cfg, sessions)
    return n, h


# --- diversidad de puertos y sesiones ---------------------------------------

def test_paul_20261017_spared_with_session_from_other_base():
    sessions = {PAUL: NOW - 30 * 60}           # b0, media hora antes
    n, h = run_family(sessions, bf_seen=seen(PAUL, 20) + seen(SWEEPER, 25, 600))
    assert n == 1 and len(h.blocked()) == 1 and SWEEPER in h.blocked()[0], h.calls
    assert any(PAUL in m and "SKIP" in m and "port diversity" in m for m in h.logs), h.logs


def test_paul_without_session_blocked_as_before():
    n, h = run_family({}, bf_seen=seen(PAUL, 20))
    assert n == 1 and PAUL in h.blocked()[0]


def test_session_seen_now_in_conntrack_counts_in_same_pass():
    # hot-port corre antes y anota la sesion; diversidad la ve en la misma pasada
    sessions = {}
    n, h = run_family(sessions, bf_seen=seen(PAUL, 20),
                      conntrack=[off(PAUL, 20210), off(PAUL, 20210, "udp")])
    assert n == 0 and not h.blocked() and PAUL in sessions


def test_session_older_than_memory_does_not_spare():
    sessions = {PAUL: NOW - 31 * 86400}
    n, h = run_family(sessions, bf_seen=seen(PAUL, 20))
    assert n == 1 and PAUL in h.blocked()[0]


def test_session_within_30_days_spares():
    sessions = {PAUL: NOW - 29 * 86400}
    n, _ = run_family(sessions, bf_seen=seen(PAUL, 20))
    assert n == 0


def test_over_sweep_max_with_session_blocked_anyway():
    sessions = {PAUL: NOW - 60}
    n, h = run_family(sessions, bf_seen=seen(PAUL, 150))
    assert n == 1 and PAUL in h.blocked()[0]
    assert any("NOTE" in m and PAUL in m for m in h.logs)
    n, _ = run_family(sessions, bf_seen=seen(PAUL, 149))
    assert n == 0


def test_flood_with_session_judged_on_live_connections():
    # desde el 22/10 el flood tambien mira la sesion (test_sweepguard_customers.py):
    # con sesion y pocas VMs se bloquea a partir de maxLiveConnsWithSession VIVAS
    sessions = {PAUL: NOW - 60}
    dead = (f"ipv4     2 tcp      6 1000 ESTABLISHED src={PAUL} dst={VIP} sport=40002 dport=20210 "
            f"src=10.0.0.3 dst={VIP} sport=20210 dport=40002 [ASSURED] mark=0 use=1\n")
    flood = [off(PAUL, 20210) for _ in range(CFG["maxLiveConnsWithSession"] - 1)] + [dead] * 20
    n, h = run_family(sessions, bf_seen=seen(PAUL, 20), conntrack=flood)
    assert n == 0 and not h.blocked(), h.calls
    flood = [off(PAUL, 20210) for _ in range(CFG["maxLiveConnsWithSession"])]
    n, h = run_family(sessions, bf_seen=seen(PAUL, 20), conntrack=flood)
    assert n == 1 and PAUL in h.blocked()[0]
    assert any("live connections" in m and "BLOCKED" in m for m in h.logs), h.logs


def test_memory_off_no_exemption():
    cfg = dict(CFG, sessionMemorySeconds=0)
    n, _ = run_family({PAUL: NOW - 60}, cfg=cfg, bf_seen=seen(PAUL, 20))
    assert n == 1


def test_v6_session_key_matches_bf_seen_form():
    v6 = "2a02:c7c:1234:5600::1"
    assert sg.sweep_with_session(v6, 20, {v6: NOW}, CFG)[0]
    assert sg.sweep_with_session("2a02:0c7c:1234:5600:0000:0000:0000:0001", 20, {v6: NOW}, CFG)[0]


# --- memoria compartida ------------------------------------------------------

def test_parse_peer_drops_garbage_and_future():
    raw = json.dumps({PAUL: NOW - 10, "no-ip": NOW, "1.2.3.4": "x", "5.6.7.8": NOW + 3600,
                      "2a02:0c7c:0:0:0:0:0:1": NOW - 5})
    assert sg.parse_peer_sessions(raw, NOW) == {PAUL: NOW - 10, "2a02:c7c::1": NOW - 5}
    for bad in ("[]", "{no json", ""):
        try:
            sg.parse_peer_sessions(bad, NOW)
            assert False, bad
        except ValueError:
            pass


def test_merge_keeps_max():
    local = {"1.1.1.1": 100.0, "2.2.2.2": 300.0}
    assert sg.merge_sessions(local, {"1.1.1.1": 200.0, "2.2.2.2": 250.0, "3.3.3.3": 50.0}) == 1
    assert local == {"1.1.1.1": 200.0, "2.2.2.2": 300.0, "3.3.3.3": 50.0}


def test_peer_address_from_watchdog_env_and_override():
    d = tempfile.mkdtemp()
    env = os.path.join(d, "failover-watchdog.env")
    with open(env, "w") as fh:
        fh.write("SELF=b1\nPEER=b0\nPEER_V6=2a01::2\nPEER_V4=116.202.118.221\nTOKEN=x\n")
    old = sg.PEER_ENV
    sg.PEER_ENV = env
    try:
        assert sg.session_peer(CFG) == "116.202.118.221"
        assert sg.session_peer(dict(CFG, sessionPeer="")) is None
        assert sg.session_peer(dict(CFG, sessionPeer="10.0.0.2")) == "10.0.0.2"
        sg.PEER_ENV = os.path.join(d, "missing")
        assert sg.session_peer(CFG) is None
    finally:
        sg.PEER_ENV = old


def test_fetch_peer_ok_and_failures_never_raise():
    cfg = dict(CFG, sessionPeer="192.0.2.1")
    with Harness(ssh={PAUL: NOW - 60}) as h:
        assert sg.fetch_peer_sessions(cfg) == {PAUL: NOW - 60}
        cmd = h.calls[0]
        assert "StrictHostKeyChecking=yes" in cmd and "BatchMode=yes" in cmd
    for failure in (None, subprocess.TimeoutExpired("ssh", 15), OSError("no ssh")):
        with Harness(ssh=failure) as h:
            assert sg.fetch_peer_sessions(cfg) is None
            assert any("local memory only" in m for m in h.logs), h.logs


def _main(harness, sessions_on_disk, cfg_extra=None):
    d = tempfile.mkdtemp()
    sess, conf = os.path.join(d, "sessions.json"), os.path.join(d, "sg.json")
    with open(sess, "w") as fh:
        json.dump(sessions_on_disk, fh)
    with open(conf, "w") as fh:
        json.dump(dict({"sessionPeer": "192.0.2.1"}, **(cfg_extra or {})), fh)
    old = sg.SESSIONS_FILE, sg.CONFIG
    sg.SESSIONS_FILE, sg.CONFIG = sess, conf
    try:
        with harness as h:
            rc = sg.main([])
    finally:
        sg.SESSIONS_FILE, sg.CONFIG = old
    return rc, h, sg.load_sessions(sess)


def test_main_merges_peer_and_spares_paul():
    h = Harness(bf_seen=seen(PAUL, 20) + seen(SWEEPER, 25, 600), ssh={PAUL: NOW - 1800})
    rc, h, saved = _main(h, {"198.51.100.7": NOW - 100})
    assert rc == 0 and len(h.blocked()) == 1 and SWEEPER in h.blocked()[0]
    assert set(saved) == {PAUL, "198.51.100.7"}          # la memoria guardada lleva las dos
    assert any("from peer" in m for m in h.logs)


def test_main_peer_down_still_blocks_and_keeps_local():
    h = Harness(bf_seen=seen(PAUL, 20) + seen(SWEEPER, 25, 600), ssh=None)
    rc, h, saved = _main(h, {"198.51.100.7": NOW - 100})
    assert rc == 0 and len(h.blocked()) == 2               # sin la otra base: como antes
    assert saved == {"198.51.100.7": NOW - 100}
    assert any("local memory only" in m for m in h.logs)


def test_sessions_max_and_size_hold_30_days():
    # 30 dias al ritmo de b1 (402 fuentes/24 h) sin repetir ninguna: ~12000
    d = tempfile.mkdtemp()
    path = os.path.join(d, "s.json")
    many = {f"10.{i // 65536}.{i // 256 % 256}.{i % 256}": NOW - i for i in range(sg.SESSIONS_MAX + 10)}
    sg.save_sessions(many, path)
    back = sg.load_sessions(path)
    assert len(back) == sg.SESSIONS_MAX and "10.0.0.0" in back      # caen las mas viejas
    assert os.path.getsize(path) < sg.PEER_MAX_BYTES


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:          # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(exc))
    sys.exit(1 if fails else 0)
