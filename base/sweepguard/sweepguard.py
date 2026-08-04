#!/usr/bin/env python3
"""neuravps-sweepguard — auto-block RDP port SWEEPERS at a BASE.

Why this exists
---------------
The rdpguard rate limits cannot stop a SLOW sweep. `bf_src` limits a source to
60 new RDP conns/min; the botnet that reached a customer's VM on 2026-07-25 ran
at ~3.5/min spread over 675 different customer ports — three orders of magnitude
under the limit, and under the per-(source,port) ip6 limit too. No rate
threshold separates it from a real client, because it is not fast.

What DOES separate them is PORT DIVERSITY:
  * a real client opens its own VM's ports — the busiest customer owns 7
    servers, and RDP+SMB on the same machine count as ONE (see vm_slot);
  * a sweeper touched 20-686 distinct machines in one conntrack sample.

So this counts DISTINCT DESTINATION PORTS per source over a rolling window
(nftables set `bf_seen`, populated by a no-verdict rule) and drops sources over
the threshold into `bf_auto`, a set with a TIMEOUT so every automatic block
expires by itself.

SECOND DETECTOR — connection flood (added 2026-07-25)
-----------------------------------------------------
Port diversity alone leaves a hole: `bf_seen` records a source on its NEW SYN
and ages out after 1h, so an attacker that OPENS many connections and HOLDS
them becomes invisible. Measured on b1: six sources with 100-1372 live RDP
connections had `bf_seen` counts of ZERO — two coordinated /24 clusters,
2725 connections, completely unseen by the sweep detector.

So a source is also blocked on live RDP connections held concurrently
(`maxConns`), read straight from conntrack. A real client needs 1-3 TCP
connections per RDP session and the busiest customer owns 7 servers. Measured
distribution on b1 at the time of writing:

    1000+ conns   2 sources      30-100 conns   2 sources, BOTH attackers
    300-1000      3 sources      10-30          9 sources  <- highest legit
    100-300      11 sources       1-10        185 sources

Nothing legitimate sat between 30 and 100, so the default of 100 keeps a 3x
margin over the busiest real source ever observed.

Safety (this runs at the edge; a mistake blocks paying customers)
---------------------------------------------------------------
  * `bf_allow` always wins — it is checked first in the chain AND here.
  * `bf_auto` entries EXPIRE (default 24h): a wrong block heals itself.
  * `maxAddsPerRun` caps the blast radius of a bug in this script.
  * `dryRun` only logs what it WOULD block (default OFF — validated 2026-07-25).
  * `enabled: false` is a kill switch; a missing/corrupt config disables it.
  * The v4 family is authoritative for v4 clients: on the ip6 side they all
    arrive SNATed to the BASE's own VIP, so ip6 only judges NATIVE v6 sources
    and explicitly skips the 64:ff9b:1::/96 translation range.
"""
import ipaddress
import json
import re
import subprocess
import sys
from collections import defaultdict

CONFIG = "/etc/neuravps-sweepguard.json"
DEFAULTS = {
    "enabled": True,
    "dryRun": False,         # validado en vivo 2026-07-25; un BASE nuevo protege desde el minuto 0
    "minPorts": 20,          # 3x el cliente con mas servidores (7)
    "maxConns": 100,         # conexiones RDP VIVAS simultaneas; nada legitimo paso de 30
    "hotPortMinTotal": 20,   # un puerto de cliente con tantas conexiones esta bajo ataque
    "hotPortMinSrc": 5,      # y la fuente que aporta tanto a ESE puerto es parte del ataque
    "maxAddsPerRun": 200,
    "blockSeconds": 86400,   # 24h
    # El detector hot-port es el UNICO ambiguo: el dueño legitimo del puerto
    # atacado esta, por definicion, entre las fuentes que se apilan en el. Ver
    # `hot_port_abusers`. Por eso su bloqueo dura menos (el atacante vuelve a
    # detectarse en la pasada siguiente, 5 min) y respeta la memoria de
    # fuentes habituales.
    "hotPortBlockSeconds": 3600,    # 1h
}
NAT64 = ipaddress.ip_network("64:ff9b:1::/96")

# La base reenvia TRES puertos por VM: SMB (445) en 1xxxx, RDP en 2xxxx y SSH
# (22) en 3xxxx, con el MISMO sufijo — 10202, 20202 y 30202 son la misma
# maquina. Contar puertos a secas multiplicaria a cada cliente (7 servidores
# -> 21 puertos) y estrecharia el margen de minPorts a un tercio. Se normaliza
# a "ranura de VM" para contar MAQUINAS.
PORT_LO, PORT_HI = 10000, 39999


def vm_slot(port):
    """Puerto reenviado -> identidad de VM (SMB, RDP y SSH de la misma maquina = 1)."""
    if port < 20000:
        return port - 10000
    if port < 30000:
        return port - 20000
    return port - 30000


def in_range(port):
    return PORT_LO <= port <= PORT_HI
# src= puede ser v4 (1.2.3.4) o v6 (2a01:...); dport= llega despues en la misma linea
_CT_RE = re.compile(r"src=([0-9a-fA-F.:]+)\s.*?dport=(\d+)")


def log(msg):
    print(f"sweepguard: {msg}", flush=True)


def nft_json(args):
    """Run `nft -j <args>` and return the parsed object list (empty on error)."""
    try:
        out = subprocess.run(["nft", "-j"] + args, capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            return []
        return json.loads(out.stdout).get("nftables", [])
    except Exception:
        return []


def set_elements(family, name):
    """Elements of a set. Returns the raw element list (shape varies by type)."""
    for obj in nft_json(["list", "set", family, "rdpguard", name]):
        s = obj.get("set")
        if s and s.get("name") == name:
            return s.get("elem", []) or []
    return []


def plain_addrs(family, name):
    """Set of plain addresses in a simple addr set (ignores prefixes/ranges —
    those only ever appear in the hand-maintained lists and a sweeper matching
    one is already dropped by the chain before us)."""
    out = set()
    for e in set_elements(family, name):
        if isinstance(e, str):
            out.add(e)
        elif isinstance(e, dict) and "elem" in e:
            v = e["elem"].get("val")
            if isinstance(v, str):
                out.add(v)
    return out


def covered_by_lists(family, addr, allow_elems):
    """True if addr falls inside any allow entry (plain, prefix or range)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True   # unparseable -> never touch it
    for e in allow_elems:
        try:
            if isinstance(e, str):
                if ip == ipaddress.ip_address(e):
                    return True
            elif isinstance(e, dict):
                if "prefix" in e:
                    p = e["prefix"]
                    if ip in ipaddress.ip_network(f"{p['addr']}/{p['len']}", strict=False):
                        return True
                elif "range" in e:
                    lo, hi = e["range"]
                    if ipaddress.ip_address(lo) <= ip <= ipaddress.ip_address(hi):
                        return True
                elif "elem" in e:
                    v = e["elem"].get("val")
                    if isinstance(v, str) and ip == ipaddress.ip_address(v):
                        return True
        except Exception:
            continue
    return False


def sweepers(family, cfg):
    """(addr -> distinct port count) for sources over the threshold."""
    per_src = defaultdict(set)
    for e in set_elements(family, "bf_seen"):
        val = e.get("elem", {}).get("val") if isinstance(e, dict) else None
        if not isinstance(val, dict):
            continue
        cat = val.get("concat")
        if not cat or len(cat) < 2:
            continue
        addr, port = cat[0], cat[1]
        if not isinstance(addr, str):
            continue
        if family == "ip6":
            # v4 clients reach us SNATed to our own VIP; judging them here would
            # mean judging every customer as one source. v4 family owns them.
            try:
                if ipaddress.ip_address(addr) in NAT64:
                    continue
            except ValueError:
                continue
        try:
            if not in_range(int(port)):
                continue
        except (TypeError, ValueError):
            continue
        per_src[addr].add(vm_slot(int(port)))     # cuenta MAQUINAS, no puertos
    return {a: len(p) for a, p in per_src.items() if len(p) >= cfg["minPorts"]}


CONNTRACK = "/proc/net/nf_conntrack"


def flooders(family, cfg):
    """(addr -> live RDP connection count) for sources over `maxConns`.

    Counts what the source is holding open RIGHT NOW, which needs no state
    between runs and cannot be evaded by going slow — the trick that defeats
    the port-diversity detector."""
    want = "ipv4" if family == "ip" else "ipv6"
    per_src = defaultdict(int)
    try:
        with open(CONNTRACK) as fh:
            for ln in fh:
                if not ln.startswith(want) or " tcp " not in ln:
                    continue
                if "dport=" not in ln:
                    continue
                m = _CT_RE.search(ln)
                if not m:
                    continue
                if not in_range(int(m.group(2))):
                    continue
                addr = m.group(1)
                if family == "ip6":
                    try:
                        if ipaddress.ip_address(addr) in NAT64:
                            continue
                    except ValueError:
                        continue
                per_src[addr] += 1
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"{family}: conntrack unreadable ({exc}) — flood detector skipped")
        return {}
    return {a: n for a, n in per_src.items() if n >= cfg["maxConns"]}


def hot_port_abusers(family, cfg):
    """(addr -> (conns, port)) for sources piling connections onto ONE attacked port.

    Detectors 1 and 2 both miss the slow DISTRIBUTED attack on a single VM:
    six sources at ~2 conns/min each touch one port (invisible to minPorts),
    hold ~12 connections each (invisible to maxConns) and stay far under the
    per-source rate limit. Measured on b1: the VM behind port 21845 had 17936
    failed logons in 24h and ZERO successful ones.

    Rate limiting cannot fix that case at all — the auth attempts ride INSIDE
    connections already open, so no new SYN is ever sent and nothing is there
    to rate limit. Only a block drops packets on an established connection.

    Two conditions must BOTH hold, which is what keeps it safe:
      * the port itself carries `hotPortMinTotal` connections — no real
        customer VM does (measured: 472 ports at 1-4, 422 at 5-9);
      * and this source contributes `hotPortMinSrc` of them — a real client
        holds 1-4 (measured p95 = 4).

    Ese "p95 = 4" es lo unico que separaba al cliente del atacante, y NO basta:
    el dueño del puerto atacado esta SIEMPRE entre las fuentes apiladas en el,
    y al reconectar contra un servidor que no responde acumula intentos. Caso
    real 2026-07-29: un cliente con 4 servidores llego a 6 conexiones sobre su
    propio puerto durante un ataque y quedo bloqueado 24h — sin poder entrar a
    NINGUNO de sus servidores ni a la consola VNC, porque el bloqueo es por IP
    de origen en la VIP compartida. Diez horas de corte y cinco correos de
    soporte sin diagnostico, porque el log no decia ni que puerto era.

    NO hay forma fiable de separarlos desde la BASE. Conviene no engañarse: se
    probaron cuatro discriminantes con medidas reales sobre b1 (2026-07-29) y
    los cuatro fallan.
      * bytes en conntrack — el camino de datos no se contabiliza aqui: maximo
        medido 1767 bytes, atacante y cliente indistinguibles;
      * estado/antiguedad TCP — atacantes p90 64006 vs resto p90 307405, pero
        solapan hasta el maximo del rango;
      * memoria de "fuentes habituales" del puerto — aprendia como habituales
        a 88.214.25.121/124 y 91.238.181.94, los mismos clusters que este
        detector existe para cazar: mantienen 1-4 conexiones sobre pocas VMs,
        exactamente el perfil de un cliente;
      * el invitado como arbitro — no sirve: todo llega SNATeado a la VIP de la
        BASE, asi que en los 4624/4625 de Windows solo se ve la BASE.

    Mientras no exista un arbitro de verdad (correlacionar el 4624 del invitado
    con la fuente que tenia la conexion en ese instante), este detector se
    asume AMBIGUO y se le acotan los daños: bloqueo corto
    (`hotPortBlockSeconds` — al atacante se le vuelve a detectar en la pasada
    siguiente, 5 min despues; al cliente mal bloqueado se le devuelve el
    servicio en 1h en vez de en 24h), y el puerto y la VM SIEMPRE en el log,
    para que soporte responda en dos minutos y no en diez horas.
    """
    want = "ipv4" if family == "ip" else "ipv6"
    pair = defaultdict(int)
    per_port = defaultdict(int)
    try:
        with open(CONNTRACK) as fh:
            for ln in fh:
                if not ln.startswith(want) or " tcp " not in ln or "dport=" not in ln:
                    continue
                m = _CT_RE.search(ln)
                if not m:
                    continue
                port = int(m.group(2))
                if not in_range(port):
                    continue
                addr = m.group(1)
                if family == "ip6":
                    try:
                        if ipaddress.ip_address(addr) in NAT64:
                            continue
                    except ValueError:
                        continue
                pair[(addr, port)] += 1
                per_port[port] += 1
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"{family}: conntrack unreadable ({exc}) — hot-port detector skipped")
        return {}

    out = {}
    for (addr, port), n in pair.items():
        if n < cfg["hotPortMinSrc"] or per_port[port] < cfg["hotPortMinTotal"]:
            continue
        prev = out.get(addr)
        if prev is None or n > prev[0]:
            out[addr] = (n, port)
    return out


def run_family(family, cfg):
    # tres detectores independientes; la razon se conserva para el log
    cand = {a: ("ports", n, None) for a, n in sweepers(family, cfg).items()}
    for a, n in flooders(family, cfg).items():
        if a not in cand:                      # diversidad de puertos manda
            cand[a] = ("conns", n, None)
    for a, (n, port) in hot_port_abusers(family, cfg).items():
        if a not in cand:
            cand[a] = ("hotport", n, port)
    if not cand:
        return 0
    allow = set_elements(family, "bf_allow")
    static = set_elements(family, "bf_static")
    already = plain_addrs(family, "bf_auto")

    picked = []
    for addr, (why, metric, port) in sorted(cand.items(), key=lambda kv: -kv[1][1]):
        if addr in already:
            continue
        if covered_by_lists(family, addr, allow):
            log(f"{family}: SKIP {addr} ({metric} {why}) — in bf_allow")
            continue
        if covered_by_lists(family, addr, static):
            continue          # ya bloqueada a mano
        picked.append((addr, why, metric, port))

    if not picked:
        return 0
    if len(picked) > cfg["maxAddsPerRun"]:
        log(f"{family}: {len(picked)} candidates exceeds maxAddsPerRun "
            f"{cfg['maxAddsPerRun']} — blocking the worst ones only")
        picked = picked[:cfg["maxAddsPerRun"]]

    for addr, why, metric, port in picked:
        # El puerto y la VM van SIEMPRE en el log del detector ambiguo: sin eso
        # es imposible saber si acabamos de bloquear a un atacante o al dueño de
        # la maquina, que es exactamente lo que costo 10h de corte el 2026-07-29.
        what = {"ports": "%d distinct ports" % metric,
                "conns": "%d live connections" % metric,
                "hotport": "%d connections onto ONE attacked port (port %s, VM %s)"
                           % (metric, port, vm_slot(port) if port else "?")}[why]
        secs = cfg["hotPortBlockSeconds"] if why == "hotport" else cfg["blockSeconds"]
        if cfg["dryRun"]:
            log(f"{family}: DRY-RUN would block {addr} ({what}) for {secs}s")
            continue
        r = subprocess.run(
            ["nft", "add", "element", family, "rdpguard", "bf_auto",
             "{ %s timeout %ds }" % (addr, secs)],
            capture_output=True, text=True)
        if r.returncode == 0:
            log(f"{family}: BLOCKED {addr} ({what}) for {secs}s")
            if why == "hotport":
                log(f"{family}: NOTE {addr} was blocked by the AMBIGUOUS detector "
                    f"on VM {vm_slot(port) if port else '?'} — if this is that "
                    f"customer they just lost ALL their servers and the VNC "
                    f"console; check before assuming it is an attacker")
        else:
            log(f"{family}: FAILED to block {addr}: {r.stderr.strip()[:120]}")
    return len(picked)


def main():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        log(f"no {CONFIG} — using defaults (dryRun={cfg['dryRun']})")
    except Exception as exc:
        log(f"config unreadable ({exc}) — refusing to act")
        return 0
    if not cfg.get("enabled"):
        log("disabled by config — nothing to do")
        return 0

    total = 0
    for family in ("ip", "ip6"):
        try:
            total += run_family(family, cfg)
        except Exception as exc:            # nunca romper el timer
            log(f"{family}: ERROR {exc}")
    if total:
        log(f"done: {total} sweeper(s) {'identified' if cfg['dryRun'] else 'blocked'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
