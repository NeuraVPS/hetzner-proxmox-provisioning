"""Tests del detector hot-port (27/09/2026): solo conexiones vivas y nunca a
una fuente con sesion RDP demostrada.

    python3 -m pytest base/sweepguard/test_sweepguard_hotport.py -q
    python3 base/sweepguard/test_sweepguard_hotport.py      # sin pytest

Cada caso sale de lo medido en b0/b1 el 27/09 (formas de linea reales de
/proc/net/nf_conntrack) o de los tres clientes bloqueados por error.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sweepguard", os.path.join(HERE, "sweepguard.py"))
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

CFG = dict(sg.DEFAULTS)
EST = 432000                       # nf_conntrack_tcp_timeout_established en b0/b1
VIP = "77.42.49.79"
NOW = 1_790_000_000.0
_sport = [40000]


def _sp():
    _sport[0] += 1
    return _sport[0]


def est(src, dport, idle, dst=VIP):
    """ESTABLISHED fuera de la flowtable, `idle` s desde su ultimo paquete."""
    sp = _sp()
    return (f"ipv4     2 tcp      6 {EST - idle} ESTABLISHED src={src} dst={dst} sport={sp} "
            f"dport={dport} src=10.0.0.3 dst={dst} sport={dport} dport={sp} [ASSURED] mark=0 zone=0 use=2\n")


def off(src, dport, proto="tcp", dst=VIP):
    """Flujo en la flowtable: ni timeout ni estado."""
    sp = _sp()
    num = 6 if proto == "tcp" else 17
    return (f"ipv4     2 {proto}      {num} src={src} dst={dst} sport={sp} dport={dport} "
            f"src=10.0.0.3 dst={dst} sport={dport} dport={sp} [OFFLOAD] mark=0 zone=0 use=3\n")


def syn(src, dport, dst=VIP):
    sp = _sp()
    return (f"ipv4     2 tcp      6 86 SYN_SENT src={src} dst={dst} sport={sp} dport={dport} "
            f"[UNREPLIED] src=10.0.0.3 dst={dst} sport={dport} dport={sp} mark=0 zone=0 use=2\n")


def tw(src, dport, dst=VIP):
    sp = _sp()
    return (f"ipv4     2 tcp      6 60 TIME_WAIT src={src} dst={dst} sport={sp} dport={dport} "
            f"src=10.0.0.3 dst={dst} sport={dport} dport={sp} [ASSURED] mark=0 zone=0 use=2\n")


def udp_unreplied(src, dport, dst=VIP):
    sp = _sp()
    return (f"ipv4     2 udp      17 28 src={src} dst={dst} sport={sp} dport={dport} [UNREPLIED] "
            f"src=10.0.0.3 dst={dst} sport={dport} dport={sp} mark=0 zone=0 use=2\n")


def entries(lines):
    return [sg.parse_ct(ln) for ln in lines if sg.parse_ct(ln)]


def scan(lines, sessions=None, now=NOW, est_timeout=EST, cfg=CFG):
    return sg.hot_port_scan(entries(lines), cfg, est_timeout, sessions, now=now)


def attackers_on(port, n_sources=3, per_source=4, maker=None):
    maker = maker or tw
    out = []
    for i in range(n_sources):
        out += [maker(f"198.51.100.{i + 1}", port) for _ in range(per_source)]
    return out


# --- parseo y piezas -------------------------------------------------------

def test_parse_real_line_shapes():
    e = sg.parse_ct(est("79.158.167.228", 31634, 97410))
    assert (e["proto"], e["state"], e["timeout"], e["dport"]) == ("tcp", "ESTABLISHED", EST - 97410, 31634)
    o = sg.parse_ct(off("79.158.167.228", 21742, "udp"))
    assert o["proto"] == "udp" and o["offload"] and o["timeout"] is None and o["state"] == ""
    s = sg.parse_ct(syn("179.254.105.105", 25565))
    assert s["state"] == "SYN_SENT" and s["unreplied"]
    assert sg.parse_ct("ipv4     2 icmp     1 29 src=1.2.3.4 dst=5.6.7.8 type=8 code=0 id=1\n") is None


def test_is_live():
    live = lambda ln, t=EST: sg.is_live(sg.parse_ct(ln), t, 300)
    assert live(off("1.1.1.1", 21000))                  # en la flowtable: trafico ahora
    assert live(syn("1.1.1.1", 21000))                  # intento de conexion
    assert live(tw("1.1.1.1", 21000))                   # cerrada hace <2 min
    assert live(est("1.1.1.1", 21000, 299))
    assert not live(est("1.1.1.1", 21000, 301))
    assert live(est("1.1.1.1", 21000, 97410), None)     # sin sysctl: como antes


def test_proves_rdp_session():
    p = lambda ln: sg.proves_rdp_session(sg.parse_ct(ln))
    assert p(off("1.1.1.1", 21742, "udp"))
    assert not p(udp_unreplied("1.1.1.1", 21742))       # sin respuesta no demuestra nada
    assert not p(off("1.1.1.1", 31742, "udp"))          # no es RDP
    assert not p(off("1.1.1.1", 21742, "tcp"))          # el TCP lo tiene tambien el atacante


# --- los clientes bloqueados por error ---------------------------------------

def test_customer_20260927_ghosts_are_not_counted():
    # 79.158.167.228 a las 10:41Z: 24 SSH a SU 31634 sin un paquete desde el
    # 26/09 07:19-07:43Z (1617-1641 min), mas RDP vivo al 21742. La regla vieja
    # lo re-bloqueaba cada hora con esas 24.
    ip = "79.158.167.228"
    lines = [est(ip, 31634, 97000 + 60 * i) for i in range(24)]
    lines += [est(ip, 31741, 97500), off(ip, 21742), off(ip, 21742, "udp")]
    abusers, skipped = scan(lines)
    assert abusers == {} and skipped == {}      # ni siquiera llega al umbral: estan muertas


def test_customer_20260927_ghosts_alone_without_rdp():
    # la liveness sola basta, aunque no hubiera sesion RDP que lo demostrara
    ip = "79.158.167.228"
    lines = [est(ip, 31634, 97000 + 60 * i) for i in range(24)]
    assert scan(lines) == ({}, {})


def test_customer_20260926_first_block_reconstructed():
    # Primer bloqueo, 26/09 07:43:22Z: de sus 24-26 SSH, 7 tenian trafico en los
    # 5 min previos. Solo el tiene conexiones en su puerto: 7 < minTotal.
    ip = "79.158.167.228"
    idles = [9, 134, 169, 204, 267, 283, 293, 301, 309, 320, 348, 350, 413, 418, 449, 499, 517,
             1346, 1350, 1362, 1421, 1439, 1441, 1442]
    lines = [est(ip, 31634, i) for i in idles]
    assert scan(lines) == ({}, {})


def test_tool_with_many_live_ssh_and_rdp_session_is_skipped():
    # Si su herramienta hubiera tenido 12 vivas, lo salva la sesion RDP.
    ip = "79.158.167.228"
    lines = [off(ip, 31634) for _ in range(12)] + [off(ip, 21742), off(ip, 21742, "udp")]
    abusers, skipped = scan(lines)
    assert abusers == {} and skipped[ip][:2] == (12, 31634)


def test_customer_20260830_stale_shortcut_to_foreign_vm():
    # Cliente con RDP a sus 3 VMs y un .rdp viejo que reintenta al 22000 (VM de
    # otro cliente, con ataque en curso). La sesion RDP lo salva; sin ella, 6
    # reintentos vivos contra una VM atacada son indistinguibles de un ataque.
    ip = "88.11.69.115"
    mine = [off(ip, p) for p in (21019, 21020, 22114)] + [off(ip, 21019, "udp")]
    retries = [syn(ip, 22000) for _ in range(6)]
    attack = attackers_on(22000)
    abusers, skipped = scan(mine + retries + attack)
    assert ip not in abusers and skipped[ip][1] == 22000
    abusers, _ = scan(retries + attack)
    assert ip in abusers


def test_customer_20260729_owner_reconnecting_remembered_session():
    # Dueño de la VM atacada: su sesion muere, reconecta y apila 6 intentos.
    # En ese momento ya no hay UDP, pero lo hubo hace una hora.
    ip = "173.52.118.229"
    sessions = {}
    scan([off(ip, 20509), off(ip, 20509, "udp")], sessions, now=NOW - 3600)
    assert ip in sessions
    lines = [syn(ip, 20509) for _ in range(6)] + attackers_on(20509)
    abusers, skipped = scan(lines, sessions, now=NOW)
    assert ip not in abusers and skipped[ip][2] == "RDP session in memory"
    # la memoria caduca: 25 h despues ya no le protege
    abusers, _ = scan(lines, sessions, now=NOW - 3600 + 25 * 3600)
    assert ip in abusers and ip not in sessions


def test_memory_off_only_trusts_current_session():
    cfg = dict(CFG, sessionMemorySeconds=0)
    ip = "173.52.118.229"
    sessions = {}
    scan([off(ip, 20509), off(ip, 20509, "udp")], sessions, now=NOW - 60, cfg=cfg)
    assert sessions == {}
    lines = [syn(ip, 20509) for _ in range(6)] + attackers_on(20509)
    assert ip in scan(lines, sessions, cfg=cfg)[0]


# --- los ataques se siguen bloqueando ----------------------------------------

def test_syn_flood_on_one_port_still_blocked():
    # 25565 en b1 a las 10:41Z: 23 SYN_SENT de una fuente y 14 de otras
    lines = [syn("179.254.105.105", 25565) for _ in range(23)]
    lines += [syn(f"203.0.113.{i}", 25565) for i in range(14)]
    abusers, _ = scan(lines)
    assert abusers["179.254.105.105"] == (23, 25565)


def test_burst_pair_on_31337_blocked():
    # 20.46.236.61 + 13.86.104.138, 24/09 02:09-02:11Z: 8 conexiones cada una en
    # 10 s, bloqueadas 2 min despues. Vivas: 16 en el puerto, 8 por fuente.
    lines = [est("20.46.236.61", 31337, 120) for _ in range(8)]
    lines += [est("13.86.104.138", 31337, 280) for _ in range(8)]
    abusers, _ = scan(lines)
    assert set(abusers) == {"20.46.236.61", "13.86.104.138"}


def test_distributed_bruteforce_on_rdp_port_blocked():
    # forma del ataque a 21845 (07/2026): varias fuentes conectando y cerrando
    lines = [tw("203.0.113.9", 21845) for _ in range(5)] + [off("203.0.113.9", 21845) for _ in range(3)]
    lines += attackers_on(21845, n_sources=4, per_source=3)
    abusers, _ = scan(lines)
    assert abusers["203.0.113.9"] == (8, 21845)


def test_attacker_ghosts_not_reblocked_but_caught_when_back():
    # 20.46.236.61: 118 re-bloqueos con conexiones muertas desde el 24/09
    ghosts = [est("20.46.236.61", 31337, 4829 * 60) for _ in range(8)]
    ghosts += [est(f"20.98.130.{i}", 31337, 2270 * 60) for i in range(10) for _ in range(7)]
    assert scan(ghosts) == ({}, {})
    back = ghosts + [syn("20.46.236.61", 31337) for _ in range(6)] + attackers_on(31337, 2, 3, syn)
    abusers, _ = scan(back)
    assert abusers["20.46.236.61"] == (6, 31337)


def test_attacker_cannot_launder_with_unreplied_udp():
    lines = [tw("203.0.113.9", 21845) for _ in range(6)] + attackers_on(21845)
    lines += [udp_unreplied("203.0.113.9", 21845)]
    assert "203.0.113.9" in scan(lines)[0]


def test_single_legit_session_far_below():
    # medido: ninguna fuente paso de 2 vivas en un puerto de VM real
    lines = [off("192.0.2.7", 20202), off("192.0.2.7", 20202), off("192.0.2.7", 20202, "udp")]
    lines += attackers_on(20202, 4, 3)
    assert "192.0.2.7" not in scan(lines)[0]


def test_unreadable_sysctl_falls_back_to_old_counting():
    ip = "79.158.167.228"
    lines = [est(ip, 31634, 97000) for _ in range(24)]
    abusers, _ = scan(lines, est_timeout=None)
    assert abusers[ip] == (24, 31634)


# --- memoria de sesiones y run_family ---------------------------------------

def test_sessions_roundtrip_and_corrupt_file():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "sub", "sessions.json")
    sg.save_sessions({"1.2.3.4": NOW}, path)
    assert sg.load_sessions(path) == {"1.2.3.4": NOW}
    with open(path, "w") as fh:
        fh.write("{no json")
    assert sg.load_sessions(path) == {}
    assert sg.load_sessions(os.path.join(d, "missing.json")) == {}


def _with_conntrack(lines, fn):
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.writelines(lines)
        path = fh.name
    old = sg.CONNTRACK
    sg.CONNTRACK = path
    try:
        return fn()
    finally:
        sg.CONNTRACK = old
        os.unlink(path)


def test_run_family_skips_session_blocks_attacker():
    nuestras4 = [{"set": {"name": "nuestras4", "elem": [VIP]}}]
    sg.nft_json = lambda args: {"nuestras4": nuestras4}.get(args[-1], [])
    old_est = sg.established_timeout
    sg.established_timeout = lambda: EST
    calls, logs = [], []
    real_run, real_log = sg.subprocess.run, sg.log
    sg.subprocess.run = lambda cmd, **k: calls.append(cmd) or real_run(["true"])
    sg.log = lambda m: logs.append(m)
    cust = "79.158.167.228"
    lines = [off(cust, 31634) for _ in range(12)] + [off(cust, 21742, "udp")]
    lines += [tw("203.0.113.9", 21845) for _ in range(6)] + attackers_on(21845)
    try:
        sessions = {}
        n = _with_conntrack(lines, lambda: sg.run_family("ip", CFG, sessions))
    finally:
        sg.subprocess.run, sg.log, sg.established_timeout = real_run, real_log, old_est
    blocked = [c[-1] for c in calls if "bf_auto" in c]
    assert n == 1 and blocked and "203.0.113.9" in blocked[0], (n, calls)
    assert any(cust in m and "SKIP" in m for m in logs), logs
    assert cust in sessions


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
