"""Tests del flood con sesion probada y de las excepciones `cust_allow` del
panel (22/10/2026).

    python3 -m pytest base/sweepguard/test_sweepguard_customers.py -q
    python3 base/sweepguard/test_sweepguard_customers.py      # sin pytest

Caso real (22/10, b1): un cliente con dos VMs (1670 y 1671), sesion RDP
probada, ~105 conexiones SSH ESTABLISHED contra 31670/31671 que se abren a
~1/min y se quedan calladas, bloqueado por «>100 live connections» cada 24 h
desde el 20/10, en v4 y en v6. Los 145 bloqueos restantes de esa regla en 30
dias (b0+b1) eran fuentes sin ninguna sesion, contra 1-3 VMs. Los datos son
sinteticos con la forma de /proc/net/nf_conntrack y de `nft -j`.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sweepguard", os.path.join(HERE, "sweepguard.py"))
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

CFG = dict(sg.DEFAULTS)
NOW = sg.time.time()
VIP4 = "77.42.49.79"
VIP6_FULL = "2a01:04f9:fff1:005f:0000:0000:0000:0002"
ANDRES4 = "47.194.157.202"
ATTACKER = "15.235.169.211"                               # 100-153 conexiones, 1 VM, sin sesion
EST = 432000


def est(src, dport, idle, dst=VIP4, fam="ipv4"):
    """ESTABLISHED fuera de la flowtable, `idle` s sin paquetes."""
    return (f"{fam}     2 tcp      6 {EST - idle} ESTABLISHED src={src} dst={dst} sport=50000 "
            f"dport={dport} src=10.0.0.3 dst={dst} sport={dport} dport=50000 [ASSURED] mark=0 use=1\n")


def udp_rdp(src, dport, dst=VIP4, fam="ipv4"):
    return (f"{fam}     2 udp      17 src={src} dst={dst} sport=50001 dport={dport} "
            f"src=10.0.0.3 dst={dst} sport={dport} dport=50001 [OFFLOAD] mark=0 zone=0 use=3\n")


def andres(n=105, live=5, src=ANDRES4, dst=VIP4, fam="ipv4"):
    """n conexiones SSH a sus dos VMs; `live` de ellas con trafico reciente."""
    out = []
    for i in range(n):
        port = 31670 if i % 2 else 31671
        out.append(est(src, port, 60 if i < live else 3600 + i * 60, dst, fam))
    return out


def cust_elems(addr, ports):
    return [{"elem": {"val": {"concat": [addr, p]}, "timeout": 2592000, "expires": 2500000}}
            for p in ports]


def seen(addr, n_machines, first=200):
    return [{"elem": {"val": {"concat": [addr, 20000 + first + i]}, "expires": 20000}}
            for i in range(n_machines)]


class Harness:
    def __init__(self, conntrack=(), bf_seen=(), cust=(), family="ip"):
        self.family = family
        self.sets = {"bf_seen": list(bf_seen), "bf_allow": [], "bf_static": [], "bf_auto": [],
                     "cust_allow": list(cust),
                     "nuestras4": [VIP4], "nuestras6": [{"prefix": {"addr": "2a01:4f9:fff1:5f::", "len": 64}}]}
        self.conntrack = list(conntrack)
        self.calls, self.logs = [], []

    def nft_json(self, args):
        name = args[-1]
        if args[:2] == ["list", "set"] and args[2] == self.family and name in self.sets:
            return [{"set": {"name": name, "elem": self.sets[name]}}]
        return []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def __enter__(self):
        fd, self.ct_path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.writelines(self.conntrack)
        self.saved = {k: getattr(sg, k) for k in ("nft_json", "log", "CONNTRACK", "established_timeout")}
        self.saved_run = sg.subprocess.run
        sg.nft_json, sg.log, sg.CONNTRACK = self.nft_json, self.logs.append, self.ct_path
        sg.established_timeout = lambda: EST
        sg.subprocess.run = self.run
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(sg, k, v)
        sg.subprocess.run = self.saved_run
        os.unlink(self.ct_path)

    def blocked(self):
        return [c[-1] for c in self.calls if c[0] == "nft" and "bf_auto" in c]


def run_family(sessions, family="ip", cfg=CFG, **kw):
    with Harness(family=family, **kw) as h:
        n = sg.run_family(family, cfg, sessions)
    return n, h


# --- 1) flood con sesion probada y pocas VMs --------------------------------

def test_andres_v4_with_session_not_blocked():
    n, h = run_family({ANDRES4: NOW - 0.6 * 86400}, conntrack=andres())
    assert n == 0 and not h.blocked(), h.calls
    skip = [m for m in h.logs if "SKIP" in m and ANDRES4 in m]
    assert skip and "105 connections, 5 live, onto 2 VM(s)" in skip[0], h.logs


def test_andres_without_session_blocked_as_before():
    n, h = run_family({}, conntrack=andres())
    assert n == 1 and ANDRES4 in h.blocked()[0]
    assert any("BLOCKED" in m and "105 live connections" in m for m in h.logs), h.logs


def test_andres_v6_uncompressed_conntrack_matches_session_key():
    # conntrack escribe la v6 sin comprimir; la memoria de sesiones la guarda comprimida
    full, short = "2606:4d80:1300:22c5:0000:0000:0000:0001", "2606:4d80:1300:22c5::1"
    n, h = run_family({short: NOW - 2.6 * 86400}, family="ip6",
                      conntrack=andres(src=full, dst=VIP6_FULL, fam="ipv6"))
    assert n == 0 and not h.blocked(), (h.calls, h.logs)
    n, h = run_family({}, family="ip6", conntrack=andres(src=full, dst=VIP6_FULL, fam="ipv6"))
    assert n == 1 and short in h.blocked()[0]


def test_session_older_than_memory_blocks():
    n, h = run_family({ANDRES4: NOW - 31 * 86400}, conntrack=andres())
    assert n == 1


def test_session_but_too_many_vms_blocks():
    lines = []
    for vm in range(CFG["connsMaxVmsWithSession"] + 1):
        lines += [est(ANDRES4, 31000 + vm, 3600)] * 10
    n, h = run_family({ANDRES4: NOW - 60}, conntrack=lines)
    assert n == 1 and ANDRES4 in h.blocked()[0]
    lines = []
    for vm in range(CFG["connsMaxVmsWithSession"]):
        lines += [est(ANDRES4, 31000 + vm, 3600)] * 11
    n, _ = run_family({ANDRES4: NOW - 60}, conntrack=lines)
    assert n == 0


def test_session_but_live_flood_blocks():
    lim = CFG["maxConnsWithSession"]
    n, h = run_family({ANDRES4: NOW - 60}, conntrack=andres(n=lim, live=lim))
    assert n == 1 and ANDRES4 in h.blocked()[0]
    n, _ = run_family({ANDRES4: NOW - 60}, conntrack=andres(n=lim, live=lim - 1))
    assert n == 0


def test_session_proven_in_this_pass_counts():
    # el UDP de RDP de ahora mismo (hot-port lo anota) vale para el flood en la misma pasada
    sessions = {}
    n, h = run_family(sessions, conntrack=andres() + [udp_rdp(ANDRES4, 21671)])
    assert n == 0 and ANDRES4 in sessions, h.logs


def test_attacker_without_session_still_blocked():
    lines = [est(ATTACKER, 21422, 120)] * 100
    n, h = run_family({ANDRES4: NOW - 60}, conntrack=lines)
    assert n == 1 and ATTACKER in h.blocked()[0]


def test_flood_scan_unreadable_sysctl_counts_all_as_live():
    entries = [sg.parse_ct(x) for x in andres(n=300, live=0)]
    for e in entries:
        e["src"] = sg.canon(e["src"])
    out, spared = sg.flood_scan(entries, CFG, None, {ANDRES4: NOW - 60})
    assert out == {ANDRES4: 300} and not spared          # nunca mas laxo
    out, spared = sg.flood_scan(entries, CFG, EST, {ANDRES4: NOW - 60})
    assert not out and spared[ANDRES4][:3] == (300, 0, 2)


# --- 2) cust_allow: los puertos de SUS VMs no cuentan -----------------------

MY_PORTS = [p + vm for vm in (1670, 1671) for p in (10000, 20000, 30000)] + [443]


def test_cust_allow_parsed_and_canonical():
    elems = cust_elems(ANDRES4, [21671, 443]) + [{"elem": {"val": "1.2.3.4"}}, "x",
                                                  {"elem": {"val": {"concat": ["1.2.3.4"]}}}]
    with Harness(cust=elems):
        assert sg.customer_allowed("ip") == {(ANDRES4, 21671), (ANDRES4, 443)}
    with Harness(family="ip6", cust=cust_elems("2606:4d80:1300:22c5:0:0:0:1", [31670])):
        assert sg.customer_allowed("ip6") == {("2606:4d80:1300:22c5::1", 31670)}
    with Harness() as h:
        del h.sets["cust_allow"]
        assert sg.customer_allowed("ip") == set()


def test_cust_allow_exempts_flood_without_session():
    n, h = run_family({}, conntrack=andres(), cust=cust_elems(ANDRES4, MY_PORTS))
    assert n == 0 and not h.blocked(), h.logs


def test_cust_allow_only_covers_listed_ports():
    # sus dos VMs exentas, pero 100 conexiones a otra VM cuentan igual
    lines = andres() + [est(ANDRES4, 21999, 3600)] * 100
    n, h = run_family({}, conntrack=lines, cust=cust_elems(ANDRES4, MY_PORTS))
    assert n == 1 and ANDRES4 in h.blocked()[0]


def test_cust_allow_exempts_hot_port_but_session_still_learned():
    # 8 vivas suyas en su puerto atacado + 5 fuentes ajenas con 2 cada una
    lines = [est(ANDRES4, 21671, 10)] * 8 + [udp_rdp(ANDRES4, 21671)]
    for i in range(5):
        lines += [est(f"198.51.100.{i}", 21671, 10)] * 2
    sessions = {}
    n, h = run_family(sessions, cust=cust_elems(ANDRES4, MY_PORTS), conntrack=lines)
    assert n == 0 and ANDRES4 in sessions, h.logs
    # sin la excepcion, y sin sesion, el hot-port le habria bloqueado
    lines = [est(ANDRES4, 21671, 10)] * 8
    for i in range(5):
        lines += [est(f"198.51.100.{i}", 21671, 10)] * 2
    n, h = run_family({}, conntrack=lines)
    assert n == 1 and ANDRES4 in h.blocked()[0]


def test_cust_allow_exempts_bf_seen_but_sweep_elsewhere_blocks():
    mine = [{"elem": {"val": {"concat": [ANDRES4, p]}}} for p in MY_PORTS if p != 443]
    n, _ = run_family({}, bf_seen=mine + seen(ANDRES4, 19), cust=cust_elems(ANDRES4, MY_PORTS))
    assert n == 0                      # 19 ajenas + las suyas exentas: por debajo de 20
    n, h = run_family({}, bf_seen=mine + seen(ANDRES4, 20), cust=cust_elems(ANDRES4, MY_PORTS))
    assert n == 1 and ANDRES4 in h.blocked()[0]


def test_cust_allow_of_other_ip_does_not_exempt():
    n, h = run_family({}, conntrack=andres(), cust=cust_elems("203.0.113.9", MY_PORTS))
    assert n == 1


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
