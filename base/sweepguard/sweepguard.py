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

Only connections to OUR addresses count (2026-09-18)
----------------------------------------------------
Since 2026-08-15 every guest's Internet egress crosses the BASE with source
10.64.x.y (v4) or its 2a01:4f9:c01f:e::/64 identity (v6). The detectors only
looked at the DESTINATION PORT, so a guest talking to an Internet service that
happens to live in 10000-39999 (a MetaTrader broker on AWS Global Accelerator
at 214xx/220xx, RustDesk 21116, Syncthing 22000, a DigitalOcean service on
25060...) was counted as "attacking our forwards". From 05/09 to 18/09 all 113
guest blocks in the journal were exactly that, and vm570/vm581 were re-blocked
every day — a blocked guest loses every NEW connection to those ports, i.e.
its broker reconnect.

A forward only exists on OUR addresses (main IPs of both bases, the VIPs and
the internal ranges). The chain now records guests only when they go there
(`ip daddr != @nuestras4 accept`, see deploy_guest_dst_scope.sh), and here the
conntrack detectors skip any flow whose ORIGINAL destination is outside the
same set. The set lives in nftables (`nuestras4` / `nuestras6` in each
rdpguard table) so there is ONE list; if it is missing this script falls back
to the old behaviour (no destination filter) and says so in the log.

A proven RDP session also exempts from port diversity (2026-10-17)
------------------------------------------------------------------
A partner who logs into many customers' VPS touched 20 machines from one IP and
was blocked 24h on b1, half an hour after proving an RDP session on b0. Port
diversity now spares a source with a proven session in the last
`sessionMemorySeconds` (30 days) below `sweepMaxWithSession` machines (150),
and b0/b1 share that memory (see fetch_peer_sessions). 30 days of journal on
both bases: of 9521 port blocks, that one was the only one it would have saved.

The connection flood spares a proven session on few VMs (2026-10-22)
--------------------------------------------------------------------
30 days of journal on b0+b1 (22/10): 155 blocks by `maxConns`; 147 from 29
sources with no RDP session ever seen, and 8 from 3 sources (two customers)
that had proven one hours or days before, holding SSH sessions to their own
1-2 VMs (one of them re-blocked every 24 h because the block freezes those
connections in ESTABLISHED for 5 days). What separates them is the session,
not the number of VMs: the attackers also aim at 1-3 VMs. A source with a
session in the memory whose connections go to at most `connsMaxVmsWithSession`
VMs is blocked only from `maxLiveConnsWithSession` LIVE connections (is_live)
or `maxConnsTotalWithSession` in total, live or dead.

Customer allowances from the panel (2026-10-22)
-----------------------------------------------
When a customer opens the panel from a blocked IP, the panel puts that IP in
`cust_allow` (addr . port, see neuravps-unblock-ip allow-vm) for the ports of
THEIR VMs only, for 48 h. The chain accepts those pairs only while the IP is in
`bf_auto`; out of the block they go through `bf_seen` and the rate limits like
anyone else. Here every detector ignores them while the entry lives, blocked or
not (see customer_allowed).
"""
import ipaddress
import json
import re
import os
import subprocess
import sys
import time
from collections import defaultdict

CONFIG = "/etc/neuravps-sweepguard.json"
DEFAULTS = {
    "enabled": True,
    "dryRun": False,         # validado en vivo 2026-07-25; un BASE nuevo protege desde el minuto 0
    "minPorts": 20,          # 3x el cliente con mas servidores (7)
    "maxConns": 100,         # conexiones RDP VIVAS simultaneas; nada legitimo paso de 30
    # Umbrales sobre conexiones VIVAS (27/09/2026, 5 muestras de b0+b1): ningun
    # puerto de VM real paso de 2 vivas ni ninguna fuente de 2 vivas en un
    # puerto. Contando tambien las muertas hacian falta 20; en vivas, 10 deja 5x
    # de margen y recupera los pares de 31337 (14-16 vivas entre dos fuentes).
    "hotPortMinTotal": 10,   # un puerto de cliente con tantas conexiones VIVAS esta bajo ataque
    "hotPortMinSrc": 5,      # y la fuente que aporta tantas VIVAS a ESE puerto es parte del ataque
    # Viva = con trafico en los ultimos hotPortMaxIdle s (una pasada). Ver is_live.
    "hotPortMaxIdle": 300,
    # Cuanto se recuerda que una fuente demostro una sesion RDP (UDP con
    # respuesta a un 2xxxx). 0 = sin memoria (solo cuenta la sesion de ahora).
    # 30 dias desde el 17/10/2026: un socio que entra en VPS de clientes de vez
    # en cuando debe seguir contando como sesion probada (ver sweep_with_session).
    "sessionMemorySeconds": 30 * 86400,
    # Una fuente con sesion probada no se bloquea por diversidad de puertos
    # salvo que toque al menos tantas MAQUINAS. 30 dias b0+b1: 7975 bloqueos en
    # 20-29, 757 en 50-149 y 19 en 150+; el unico con sesion, 20.
    "sweepMaxWithSession": 150,
    # Base par con la que se comparte la memoria de sesiones (pull por SSH con
    # orden forzada, ver fetch_peer_sessions). None = PEER_V4 de PEER_ENV;
    # "" = no compartir.
    "sessionPeer": None,
    "sessionPeerTimeout": 15,
    # Flood (maxConns) con sesion probada en sessionMemorySeconds y pocas VMs:
    # 30 dias b0+b1 (22/10), los 8 bloqueos a clientes tenian sesion y 1-2
    # VMs; los 147 restantes, ninguna sesion. Se bloquea a partir de tantas
    # VIVAS (is_live: las muertas las fabrica el propio bloqueo) o de tantas
    # en total, vivas o muertas. -1 en connsMaxVmsWithSession = sin excepcion.
    "connsMaxVmsWithSession": 10,       # el cliente con mas servidores tiene 7
    "maxLiveConnsWithSession": 100,     # el cliente del 22/10: ~5 vivas de ~105
    "maxConnsTotalWithSession": 500,
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
# src= puede ser v4 (1.2.3.4) o v6 (2a01:...); dst= va justo detras y dport=
# llega despues en la misma linea. Es la tupla ORIGINAL (la primera), o sea el
# destino antes del DNAT: la VIP / IP principal a la que apunto el cliente.
_CT_RE = re.compile(r"src=([0-9a-fA-F.:]+)\s+dst=([0-9a-fA-F.:]+)\s.*?dport=(\d+)")

# Set de nftables con NUESTRAS direcciones (bases, VIPs, rangos internos): solo
# lo que va ahi puede ser un ataque a un forward. Ver deploy_guest_dst_scope.sh.
GUARDED_SET = {"ip": "nuestras4", "ip6": "nuestras6"}


def canon(addr):
    """Forma canonica de una IP. /proc/net/nf_conntrack escribe la v6 SIN
    comprimir (2600:1900:...:0000:031a:0000:0000) y nft la devuelve comprimida
    (2600:1900:...:0:31a::): sin normalizar, `addr in already` no casaba nunca y
    cada pasada volvia a loguear BLOCKED de una fuente ya bloqueada (210 lineas
    en b1 del 10/09 al 18/09 para un solo escaner de Google Cloud)."""
    try:
        return ipaddress.ip_address(addr).compressed
    except ValueError:
        return addr


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


def guarded_destinations(family):
    """Predicate addr->bool for 'this destination is one of ours', built from
    the `nuestras4`/`nuestras6` set. Returns None when the set does not exist,
    which means: no destination filter (the behaviour before 2026-09-18)."""
    name = GUARDED_SET[family]
    found = False
    nets, ranges = [], []
    for obj in nft_json(["list", "set", family, "rdpguard", name]):
        s = obj.get("set")
        if not s or s.get("name") != name:
            continue
        found = True
        for e in s.get("elem", []) or []:
            try:
                if isinstance(e, str):
                    nets.append(ipaddress.ip_network(e))
                elif isinstance(e, dict) and "prefix" in e:
                    p = e["prefix"]
                    nets.append(ipaddress.ip_network(f"{p['addr']}/{p['len']}", strict=False))
                elif isinstance(e, dict) and "range" in e:
                    lo, hi = e["range"]
                    ranges.append((ipaddress.ip_address(lo), ipaddress.ip_address(hi)))
                elif isinstance(e, dict) and "elem" in e:
                    v = e["elem"].get("val")
                    if isinstance(v, str):
                        nets.append(ipaddress.ip_network(v))
            except (ValueError, KeyError, TypeError):
                continue
    if not found:
        return None
    if not nets and not ranges:
        # un set vacio NO debe apagar la deteccion entera (todo seria "ajeno")
        log(f"{family}: set {name} is EMPTY — destination filter OFF")
        return None

    def ours(addr):
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True       # no parseable -> se cuenta, como antes
        return any(ip in n for n in nets) or any(lo <= ip <= hi for lo, hi in ranges)
    return ours


CUSTOMER_SET = "cust_allow"


def customer_allowed(family):
    """Pares (addr canonica, puerto) de `cust_allow`: un cliente del panel hacia
    los puertos de SUS VMs (ver neuravps-unblock-ip allow-vm). Ningun detector
    los cuenta. Set ausente o ilegible = vacio (lo de antes).

    Se ignoran este o no la IP en bf_auto, y es a proposito: mientras esta
    bloqueada, sweepguard ya no la juzga (run_family salta lo que esta en
    bf_auto), asi que la excepcion solo tiene efecto FUERA del bloqueo. Ahi
    sirve para que el trafico del dueño a su propia VM no lo vuelva a bloquear
    (el 22/10: ~13 SSH/h en v4 y ~45/h en v6, casi todas calladas; sin
    sesion RDP, el flood lo rebloquearia en horas). Lo acotan el TTL de la
    entrada (48 h, solo se renueva entrando en el panel ESTANDO bloqueado),
    los limites de ritmo de la cadena, que fuera del bloqueo si se aplican, y
    que solo son esa IP y esos puertos."""
    out = set()
    for e in set_elements(family, CUSTOMER_SET):
        val = e.get("elem", {}).get("val") if isinstance(e, dict) else None
        cat = val.get("concat") if isinstance(val, dict) else None
        if not cat or len(cat) != 2 or not isinstance(cat[0], str):
            continue
        try:
            out.add((canon(cat[0]), int(cat[1])))
        except (TypeError, ValueError):
            continue
    return out


def sweepers(family, cfg, exempt=None):
    """(addr -> distinct port count) for sources over the threshold.
    `exempt`: pares (addr, puerto) de customer_allowed, que no cuentan."""
    exempt = exempt or set()
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
        if exempt and (canon(addr), int(port)) in exempt:
            continue                  # cliente hacia su propia VM (cust_allow)
        per_src[addr].add(vm_slot(int(port)))     # cuenta MAQUINAS, no puertos
    return {a: len(p) for a, p in per_src.items() if len(p) >= cfg["minPorts"]}


CONNTRACK = "/proc/net/nf_conntrack"


def flooders(family, cfg, ours=None, sessions=None, exempt=None):
    """(addr -> RDP connection count) for sources over `maxConns`.

    Counts what the source is holding open RIGHT NOW, which needs no state
    between runs and cannot be evaded by going slow — the trick that defeats
    the port-diversity detector. `ours` (see guarded_destinations) drops flows
    that do not go to one of our addresses: a guest's Internet egress.
    `sessions` and `exempt`: see flood_scan."""
    try:
        entries = conntrack_entries(family, ours)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"{family}: conntrack unreadable ({exc}) — flood detector skipped")
        return {}
    est = established_timeout() if sessions else None
    out, spared = flood_scan(entries, cfg, est, sessions, exempt)
    for addr, (n, live, vms, t) in sorted(spared.items()):
        log(f"{family}: SKIP {addr} ({n} connections, {live} live, onto {vms} VM(s)) — "
            f"RDP session proven {int((time.time() - t) // 60)} min ago: flood limits "
            f"{cfg['maxLiveConnsWithSession']} live / {cfg['maxConnsTotalWithSession']} total")
    return out


def flood_scan(entries, cfg, est_timeout, sessions=None, exempt=None, now=None):
    """Nucleo del detector de flood, sin E/S.

    Cuenta las conexiones TCP de cada fuente (todas, vivas o no: lo de siempre)
    salvo los pares de `exempt` (customer_allowed). Una fuente que llega a
    `maxConns` se perdona si demostro una sesion RDP en `sessionMemorySeconds`
    (memoria b0+b1), sus conexiones van a `connsMaxVmsWithSession` VMs o menos,
    tiene menos de `maxLiveConnsWithSession` VIVAS (is_live) y menos de
    `maxConnsTotalWithSession` en total. Sin dato de silencio (sysctl
    ilegible) todas cuentan como vivas: nunca mas laxo que eso.
    Devuelve (flooders addr -> n, spared addr -> (n, vivas, vms, epoch sesion))."""
    now = time.time() if now is None else now
    exempt = exempt or set()
    total = defaultdict(int)
    live = defaultdict(int)
    vms = defaultdict(set)
    max_idle = cfg["hotPortMaxIdle"]
    for e in entries:
        if e["proto"] != "tcp" or (e["src"], e["dport"]) in exempt:
            continue
        a = e["src"]
        total[a] += 1
        vms[a].add(vm_slot(e["dport"]))
        if is_live(e, est_timeout, max_idle):
            live[a] += 1
    out, spared = {}, {}
    memory = cfg["sessionMemorySeconds"]
    for a, n in total.items():
        if n < cfg["maxConns"]:
            continue
        t = sessions.get(a) if (sessions and memory > 0) else None
        if (t is not None and now - t <= memory
                and len(vms[a]) <= cfg["connsMaxVmsWithSession"]
                and live[a] < cfg["maxLiveConnsWithSession"]
                and n < cfg["maxConnsTotalWithSession"]):
            spared[a] = (n, live[a], len(vms[a]), t)
            continue
        out[a] = n
    return out, spared


# --- hot-port: solo conexiones VIVAS, y nunca a quien ha demostrado una sesion --
#
# Linea de /proc/net/nf_conntrack, cabecera incluida. El timeout y el estado
# faltan cuando el flujo esta en la flowtable ([OFFLOAD]) y el estado falta
# siempre en UDP:
#   ipv4 2 tcp 6 334590 ESTABLISHED src=.. dst=.. sport=.. dport=31634 .. [ASSURED]
#   ipv4 2 tcp 6 src=.. dst=.. sport=.. dport=21742 .. [OFFLOAD]
#   ipv4 2 udp 17 src=.. dst=.. sport=.. dport=21742 .. [OFFLOAD]
_CT_HEAD_RE = re.compile(r"^(ipv4|ipv6)\s+\d+\s+(tcp|udp)\s+\d+\s+(?:(\d+)\s+)?(?:([A-Z_]+)\s+)?src=")
EST_TIMEOUT_SYSCTL = "/proc/sys/net/netfilter/nf_conntrack_tcp_timeout_established"
SESSIONS_FILE = "/var/lib/neuravps-sweepguard/sessions.json"
SESSIONS_MAX = 50000
RDP_LO, RDP_HI = 20000, 29999


def established_timeout():
    """Timeout de ESTABLISHED del kernel (s), o None si no se puede leer.

    Hace falta para saber cuanto lleva callada una conexion: el kernel reinicia
    el timeout con cada paquete, asi que `timeout_established - restante` es el
    tiempo desde el ultimo paquete. Sin este dato no se puede separar una
    conexion viva de una muerta y el detector vuelve al criterio viejo."""
    try:
        with open(EST_TIMEOUT_SYSCTL) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def parse_ct(ln):
    """Linea de conntrack -> dict, o None si no es TCP/UDP con puerto destino."""
    h = _CT_HEAD_RE.match(ln)
    m = _CT_RE.search(ln) if h else None
    if not m:
        return None
    fam, proto, timeout, state = h.groups()
    return {"fam": fam, "proto": proto,
            "timeout": int(timeout) if timeout else None,
            "state": state or "",
            "src": m.group(1), "dst": m.group(2), "dport": int(m.group(3)),
            "offload": "[OFFLOAD]" in ln, "assured": "[ASSURED]" in ln,
            "unreplied": "[UNREPLIED]" in ln}


def is_live(e, est_timeout, max_idle):
    """¿La conexion ha tenido trafico en los ultimos `max_idle` segundos?

    * [OFFLOAD]: esta en la flowtable, que la echa a los 30 s sin paquetes —
      tiene trafico ahora mismo.
    * cualquier estado que no sea ESTABLISHED (SYN_SENT, SYN_RECV, TIME_WAIT,
      CLOSE, FIN_WAIT...): sus timeouts son de 2 min o menos, asi que es una
      conexion abierta o cerrada hace nada. Es justo el rastro de la fuerza
      bruta: conectar, fallar, cerrar, volver a conectar.
    * ESTABLISHED fuera de la flowtable: callada al menos 30 s. Cuenta solo si
      el ultimo paquete es de hace `max_idle` o menos.
    Si falta el dato (sysctl ilegible), cuenta — el comportamiento de antes."""
    if e["offload"] or e["state"] != "ESTABLISHED":
        return True
    if est_timeout is None or e["timeout"] is None:
        return True
    return est_timeout - e["timeout"] <= max_idle


def proves_rdp_session(e):
    """Flujo UDP con respuesta hacia un forward RDP (2xxxx).

    El transporte UDP de RDP (MS-RDPEMT) lo abre el cliente cuando el servidor
    se lo ofrece, y el servidor solo lo ofrece despues de que la conexion haya
    terminado su secuencia, credenciales incluidas (NLA). Un fuerza-bruta que
    falla la autenticacion nunca llega ahi. Medido 27/09/2026: 113 fuentes con
    este flujo en b0+b1 y NINGUNA bloqueada por ningun detector desde el 05/09,
    salvo el cliente al que el hot-port bloqueaba por error."""
    return (e["proto"] == "udp" and RDP_LO <= e["dport"] <= RDP_HI
            and not e["unreplied"] and (e["assured"] or e["offload"]))


def load_sessions(path=None):
    path = path or SESSIONS_FILE
    try:
        with open(path) as fh:
            d = json.load(fh)
        return {str(a): float(t) for a, t in d.items()}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        # sin memoria el detector es MAS estricto, nunca menos: fallo seguro
        log(f"sessions memory unreadable ({exc}) — starting empty")
        return {}


def save_sessions(sessions, path=None):
    path = path or SESSIONS_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if len(sessions) > SESSIONS_MAX:          # acota el fichero; caen las mas viejas
            keep = sorted(sessions.items(), key=lambda kv: -kv[1])[:SESSIONS_MAX]
            sessions = dict(keep)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(sessions, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)       # la base par lee este fichero: nunca a medias
    except Exception as exc:
        log(f"sessions memory not saved ({exc})")


# --- memoria compartida entre b0 y b1 (17/10/2026) ----------------------------
#
# Un cliente entra por la base que le toque, y puede cambiar de una pasada a
# otra. El 17/10 un socio demostro sesion en b0 a las 09:34Z y b1, que no lo
# sabia, le bloqueo 24 h a las 10:04Z. Cada pasada LEE el fichero de la otra
# base y fusiona por la marca mas reciente; nunca escribe en ella. El canal es
# una clave propia (PEER_KEY) que en la otra base solo puede ejecutar
# `cat sessions.json` (command= forzado, restrict, from=), con la clave de host
# de la otra base fijada en PEER_KNOWN_HOSTS. Cualquier fallo deja solo la
# memoria local, que es lo de antes: el detector nunca queda mas laxo.
PEER_ENV = "/etc/neuravps/failover-watchdog.env"
PEER_KEY = "/root/.ssh/sweepguard_peer"
PEER_KNOWN_HOSTS = "/var/lib/neuravps-sweepguard/peer_known_hosts"
PEER_MAX_BYTES = 16 * 1024 * 1024


def session_peer(cfg):
    """Direccion de la base par, o None si no hay que compartir."""
    peer = cfg.get("sessionPeer")
    if peer is not None:
        return peer or None
    try:
        with open(PEER_ENV) as fh:
            for ln in fh:
                k, _, v = ln.strip().partition("=")
                if k == "PEER_V4" and v.strip():
                    return v.strip().strip('"')
    except OSError:
        pass
    return None


def parse_peer_sessions(raw, now):
    """JSON de la base par -> {addr: epoch} validado. Lanza ValueError si no es
    un objeto; las entradas raras se descartan una a una (direccion que no es
    IP, marca no numerica o del futuro)."""
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("not a JSON object")
    out = {}
    for a, t in d.items():
        try:
            addr = ipaddress.ip_address(str(a)).compressed
            t = float(t)
        except (ValueError, TypeError):
            continue
        if t != t or t > now + 300:              # NaN o reloj de la otra base adelantado
            continue
        out[addr] = t
    return out


def merge_sessions(local, peer):
    """Fusiona en sitio por la marca maxima. Devuelve cuantas fuentes no
    estaban en la memoria local."""
    new = 0
    for a, t in peer.items():
        prev = local.get(a)
        if prev is None:
            new += 1
        if prev is None or t > prev:
            local[a] = t
    return new


def fetch_peer_sessions(cfg, now=None):
    """Memoria de sesiones de la base par, o None si no se pudo leer (y se dice
    en el log). Nunca lanza."""
    peer = session_peer(cfg)
    if not peer:
        return None
    now = time.time() if now is None else now
    tmo = cfg["sessionPeerTimeout"]
    cmd = ["ssh", "-i", PEER_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", f"ConnectTimeout={max(1, tmo // 3)}", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={PEER_KNOWN_HOSTS}", "-o", "LogLevel=ERROR",
           f"root@{peer}", "sessions"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=tmo)
        if r.returncode != 0:
            raise RuntimeError(f"ssh rc={r.returncode}: "
                               f"{' '.join(r.stderr.decode(errors='replace').split())[:200]}")
        if len(r.stdout) > PEER_MAX_BYTES:
            raise RuntimeError(f"{len(r.stdout)} bytes, over {PEER_MAX_BYTES}")
        return parse_peer_sessions(r.stdout.decode(), now)
    except Exception as exc:
        log(f"sessions: peer {peer} unreadable ({exc}) — local memory only")
        return None


def sweep_with_session(addr, metric, sessions, cfg, now=None):
    """¿Se perdona a esta fuente el bloqueo por diversidad de puertos?

    Si demostro una sesion RDP (`proves_rdp_session`) en esta base o en la par
    dentro de `sessionMemorySeconds`, no es un barredor: es alguien que entra en
    muchas maquinas con credenciales buenas (un socio que administra VPS de
    clientes, una oficina tras un NAT). Un barredor no llega al UDP de RDP. Por
    encima de `sweepMaxWithSession` maquinas se bloquea igual: con una sesion
    buena tambien se puede barrer. Devuelve (perdonada, epoch de la sesion)."""
    memory = cfg["sessionMemorySeconds"]
    t = sessions.get(canon(addr)) if sessions else None
    if memory <= 0 or t is None:
        return False, None
    now = time.time() if now is None else now
    if now - t > memory or metric >= cfg["sweepMaxWithSession"]:
        return False, t
    return True, t


def hot_port_scan(entries, cfg, est_timeout, sessions=None, now=None, exempt=None):
    """Nucleo del detector hot-port, sin E/S: lo usan sweepguard y la
    herramienta de evaluacion en seco (hotport_dryrun.py).

    `entries`: dicts de parse_ct() ya filtrados (familia, rango, destino nuestro).
    `sessions`: memoria addr -> epoch de la ultima sesion RDP demostrada; se
    actualiza en sitio con lo que se vea ahora.
    Devuelve (abusers, skipped):
      abusers  addr -> (conexiones vivas, puerto)
      skipped  addr -> (conexiones vivas, puerto, motivo) — pasaban el umbral
               pero tienen una sesion demostrada
    `exempt`: pares (addr, puerto) de customer_allowed; cuentan para demostrar
    una sesion, no como conexiones.
    """
    now = time.time() if now is None else now
    max_idle = cfg["hotPortMaxIdle"]
    memory = cfg["sessionMemorySeconds"]
    sessions = {} if sessions is None else sessions
    exempt = exempt or set()
    pair = defaultdict(int)
    per_port = defaultdict(int)
    proven_now = set()
    for e in entries:
        if proves_rdp_session(e):
            proven_now.add(e["src"])
        if e["proto"] != "tcp" or not is_live(e, est_timeout, max_idle):
            continue
        if (e["src"], e["dport"]) in exempt:
            continue          # cliente hacia su propia VM (cust_allow): ni suma al puerto
        pair[(e["src"], e["dport"])] += 1
        per_port[e["dport"]] += 1
    if memory > 0:
        for a in proven_now:
            sessions[a] = now
        for a in [a for a, t in sessions.items() if now - t > memory]:
            del sessions[a]

    abusers, skipped = {}, {}
    for (addr, port), n in pair.items():
        if n < cfg["hotPortMinSrc"] or per_port[port] < cfg["hotPortMinTotal"]:
            continue
        if addr in proven_now or (memory > 0 and addr in sessions):
            why = "RDP session now" if addr in proven_now else "RDP session in memory"
            prev = skipped.get(addr)
            if prev is None or n > prev[0]:
                skipped[addr] = (n, port, why)
            continue
        prev = abusers.get(addr)
        if prev is None or n > prev[0]:
            abusers[addr] = (n, port)
    return abusers, skipped


def conntrack_entries(family, ours=None):
    """Entradas de conntrack de la familia, hacia el rango de forwards y hacia
    NUESTRAS direcciones (ver guarded_destinations). TCP y UDP."""
    want = "ipv4" if family == "ip" else "ipv6"
    out = []
    with open(CONNTRACK) as fh:
        for ln in fh:
            if not ln.startswith(want) or "dport=" not in ln:
                continue
            e = parse_ct(ln)
            if e is None or not in_range(e["dport"]):
                continue
            if ours is not None and not ours(e["dst"]):
                continue          # va a Internet, no a un forward nuestro
            e["src"] = canon(e["src"])
            if family == "ip6":
                try:
                    if ipaddress.ip_address(e["src"]) in NAT64:
                        continue
                except ValueError:
                    continue
            out.append(e)
    return out


def hot_port_abusers(family, cfg, ours=None, sessions=None, exempt=None):
    """(addr -> (conns, port)) for sources piling LIVE connections onto ONE attacked port.

    Detectors 1 and 2 both miss the slow DISTRIBUTED attack on a single VM:
    six sources at ~2 conns/min each touch one port (invisible to minPorts),
    hold ~12 connections each (invisible to maxConns) and stay far under the
    per-source rate limit. Measured on b1 (2026-07): the VM behind port 21845
    had 17936 failed logons in 24h and ZERO successful ones.

    Two conditions must BOTH hold: the port carries `hotPortMinTotal` live
    connections and this source contributes `hotPortMinSrc` of them.

    Tres clientes reales bloqueados (29/07, 30/08, 26-27/09) y lo que se midio
    el 27/09/2026 en b0+b1 cambiaron QUE se cuenta, no los umbrales:

    1. Solo conexiones VIVAS (`is_live`). Con el timeout de ESTABLISHED en el
       valor del kernel (5 dias), conntrack guarda durante dias conexiones sin
       un solo paquete. El detector las contaba, y el bloqueo las fabrica: al
       tirar los paquetes de una fuente, sus conexiones se quedan congeladas
       en ESTABLISHED. Cada hora caducaba el bloqueo y la pasada siguiente
       volvia a bloquear por esas mismas conexiones muertas. En b0, los 1681
       bloqueos hot-port del 20 al 27/09 fueron 34 fuentes; ninguna conexion
       ESTABLISHED fuera de la flowtable tenia menos de 30 min de silencio. El
       cliente del 27/09 (24 SSH a su propia VM) fue bloqueado 27 veces por
       conexiones cuyo ultimo paquete era de 1 minuto antes del PRIMER bloqueo.
       La fuerza bruta real deja rastro vivo por definicion: conexiones en la
       flowtable, SYN, TIME_WAIT. Un atacante que vuelve cuando caduca su
       bloqueo se detecta en la pasada siguiente, igual que antes.

    2. Nunca a una fuente con una sesion RDP demostrada (`proves_rdp_session`):
       un flujo UDP con respuesta hacia un 2xxxx solo existe despues de un
       inicio de sesion correcto. Es el arbitro que faltaba ("correlacionar el
       4624 con la fuente") y la base lo ve sin mirar dentro del invitado. Se
       recuerda `sessionMemorySeconds` (30 dias) para cubrir al dueño que
       reconecta cuando su VM, atacada, deja de responder (caso 29/07): en ese
       momento su UDP ya no esta, pero la sesion de hace un rato si. La
       fuente sigue sometida a los limites de ritmo, al detector 2 y al 1 a
       partir de `sweepMaxWithSession` maquinas.

    Los discriminantes que fallaron el 29/07 siguen descartados: bytes (el
    camino de datos no se contabiliza aqui; nf_conntrack_acct esta a 0),
    antiguedad TCP como umbral (sin nf_conntrack_timestamp no hay edad; lo
    que se mide aqui es SILENCIO, que es otra cosa) y memoria de fuentes
    "habituales" (aprendia a los atacantes). La memoria de sesiones no aprende
    atacantes porque exige el UDP de RDP, que un atacante no alcanza.
    """
    try:
        entries = conntrack_entries(family, ours)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"{family}: conntrack unreadable ({exc}) — hot-port detector skipped")
        return {}
    est = established_timeout()
    if est is None:
        log(f"{family}: {EST_TIMEOUT_SYSCTL} unreadable — hot-port counts every "
            f"ESTABLISHED connection, live or not")
    abusers, skipped = hot_port_scan(entries, cfg, est, sessions, exempt=exempt)
    for addr, (n, port, why) in sorted(skipped.items()):
        log(f"{family}: SKIP {addr} ({n} live connections onto port {port}, "
            f"VM {vm_slot(port)}) — {why}: not judged by hot-port")
    return abusers


def run_family(family, cfg, sessions=None):
    ours = guarded_destinations(family)
    if ours is None:
        log(f"{family}: set {GUARDED_SET[family]} not found — conntrack detectors "
            f"count EVERY destination (guest Internet egress included)")
    # tres detectores independientes; la razon se conserva para el log.
    # hot-port va primero porque es el que anota en `sessions` las sesiones RDP
    # que se ven ahora, y diversidad de puertos las consulta.
    exempt = customer_allowed(family)
    hot = hot_port_abusers(family, cfg, ours, sessions, exempt)
    cand = {}
    for a, n in sweepers(family, cfg, exempt).items():
        spared, t = sweep_with_session(a, n, sessions, cfg)
        if spared:
            log(f"{family}: SKIP {a} ({n} ports) — RDP session proven "
                f"{int((time.time() - t) // 60)} min ago (b0/b1 memory): "
                f"not judged by port diversity under {cfg['sweepMaxWithSession']}")
            continue
        if t is not None and n >= cfg["sweepMaxWithSession"]:
            log(f"{family}: NOTE {a} has a proven RDP session but touches {n} "
                f"machines (>= sweepMaxWithSession) — judged as usual")
        cand[a] = ("ports", n, None)
    for a, n in flooders(family, cfg, ours, sessions, exempt).items():
        if a not in cand:                      # diversidad de puertos manda
            cand[a] = ("conns", n, None)
    for a, (n, port) in hot.items():
        if a not in cand:
            cand[a] = ("hotport", n, port)
    if not cand:
        return 0
    allow = set_elements(family, "bf_allow")
    static = set_elements(family, "bf_static")
    already = {canon(a) for a in plain_addrs(family, "bf_auto")}

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
        # «live connections» se queda asi aunque cuente tambien las muertas:
        # es la cadena que buscan los runbooks y las notas en el journal.
        what = {"ports": "%d distinct ports" % metric,
                "conns": "%d live connections" % metric,
                "hotport": "%d live connections onto ONE attacked port (port %s, VM %s)"
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
                log(f"{family}: NOTE {addr} was blocked by the hot-port detector "
                    f"on VM {vm_slot(port) if port else '?'} (no RDP session seen "
                    f"from it in {cfg['sessionMemorySeconds']}s) — if this is a "
                    f"customer they just lost ALL their servers and the VNC "
                    f"console; check before assuming it is an attacker")
        else:
            log(f"{family}: FAILED to block {addr}: {r.stderr.strip()[:120]}")
    return len(picked)


def report_smb_rate_limit():
    """Telemetria del limite de ritmo SMB VM<->VM (deploy-smb-rate-limit.sh).

    No es un detector de sweepguard: es un contador nftables inline
    (`smb_rl_drops`) que cuenta los SYN de SMB entre VMs descartados por pasar
    del umbral por origen. Se reporta aqui, cada pasada, para que salga en el
    mismo journal que los bloqueos y no haga falta otra telemetria. Silencioso
    si la regla no esta desplegada o si no ha descartado nada nuevo."""
    for obj in nft_json(["list", "counter", "inet", "filter", "smb_rl_drops"]):
        c = obj.get("counter")
        if c and c.get("name") == "smb_rl_drops":
            pk = c.get("packets", 0)
            if pk:
                log(f"smb-rate-limit: {pk} SMB SYN entre VMs descartados por exceso de ritmo (acumulado)")
            return


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        log(f"no {CONFIG} — using defaults (dryRun={cfg['dryRun']})")
    except Exception as exc:
        log(f"config unreadable ({exc}) — refusing to act")
        return 0
    if "--dry-run" in argv:
        # para probar una version nueva en una base sin bloquear nada; tampoco
        # escribe la memoria de sesiones
        cfg["dryRun"] = True
    if not cfg.get("enabled"):
        log("disabled by config — nothing to do")
        return 0

    sessions = load_sessions()
    peer = fetch_peer_sessions(cfg)
    if peer is not None:
        new = merge_sessions(sessions, peer)
        log(f"sessions: {len(peer)} from peer {session_peer(cfg)}, {new} new here, "
            f"{len(sessions)} in shared memory")
    total = 0
    for family in ("ip", "ip6"):
        try:
            total += run_family(family, cfg, sessions)
        except Exception as exc:            # nunca romper el timer
            log(f"{family}: ERROR {exc}")
    if not cfg["dryRun"]:
        save_sessions(sessions)
    if total:
        log(f"done: {total} sweeper(s) {'identified' if cfg['dryRun'] else 'blocked'}")
    report_smb_rate_limit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
