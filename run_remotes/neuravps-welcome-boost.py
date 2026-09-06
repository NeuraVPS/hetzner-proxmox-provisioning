#!/usr/bin/env python3
#NWBVER=2
"""neuravps-welcome-boost — infla el balloon de una VM estrujada en el momento
en que su cliente se conecta por RDP (daemon en cada BASE).

El problema que resuelve: el estrujado por idle es invisible, pero la VUELTA
duele — el cliente aterriza sobre el suelo (p.ej. 5,8 GB de un plan de 19),
arranca SQX/MT y sufre 30-60 min de paging hasta que la escalera de faults del
reconciler le devuelve la RAM (caso Gerard vm1022, 2026-08-01). Las bases ven
el instante exacto del login: la conexión del cliente a su puerto RDP
(20000+vmid) atraviesa nuestro DNAT. Este daemon la detecta y devuelve la RAM
del plan ANTES de que el guest la pida por las malas.

Detección (anti-scanner): poll de conntrack cada POLL_S. Un flujo cuenta como
login real solo si sigue ESTABLISHED en DOS polls consecutivos (≥POLL_S vivo)
— los brute-forcers que martillean RDP 24/7 (razón de ser de rdpguard) cortan
en <2 s tras el banner/NLA; una sesión real se mantiene. Cada flujo se
atiende UNA vez (cache handled con TTL): las sesiones largas no re-disparan.

Acción (misma semántica que el arreglo manual validado): en el nodo,
  1. sembrar el suelo ORIGINAL en floors.json si no existe — sin esto el
     decay del reconciler haría "hands off" y la VM quedaría a tope PARA
     SIEMPRE (fuga de capacidad);
  2. `qm set <vmid> --balloon <memory>` (suelo=plan → pvestatd no puede
     estrujar por debajo);
  3. empujón HMP `balloon <memory>` (el target vivo no sigue solo al suelo).
El decay de idle-credit del reconciler des-hace el boost días después si la
VM vuelve a quedarse quieta: el modelo de capacidad no cambia, solo CUÁNDO se
des-estruja (al abrir la puerta, no tras el dolor).

Guardas: headroom del nodo (MemAvailable >= delta + HOST_FREE_MIN_MB, el
mismo margen del reconciler); rate-limit por vmid; tope de consultas por
ciclo. Kill-switch Firestore `config/welcomeboost` {enabled, dryRun} — doc
ausente o enabled!=true = APAGADO.
"""
import json
import ipaddress
import os
import re
import socket
import shlex
import subprocess
import sys
import time

STATE_FILE = os.environ.get("BASE_NAT_STATE", "/var/lib/base-nat/state.json")
NODES_FILE = os.environ.get("BASE_NAT_NODES", "/var/lib/base-nat/pve_nodes.json")
CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
POLL_S = int(os.environ.get("POLL_S", "12"))
CONFIG_EVERY = 10          # ciclos entre relecturas de config/welcomeboost
PORT_MIN = 20000
HANDLED_TTL_S = 3600       # un flujo se atiende una vez por hora
BOOST_COOLDOWN_S = 1800    # no re-evaluar una vmid boosteada en 30 min
FULL_TTL_S = 1800          # vmid comprobada "ya a tope": no re-consultar en 30 min
BLOCKED_TTL_S = 300        # sin headroom: reintentar en 5 min
HOST_FREE_MIN_MB = 12288   # mismo margen que el reconciler
MAX_QUERIES_PER_CYCLE = 15
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
       "-o", "StrictHostKeyChecking=no"]

# Primera tupla de la línea = dirección ORIGINAL (dport pre-DNAT = 20000+vmid).
TUPLE_RE = re.compile(r"src=(\S+) dst=\S+ sport=(\d+) dport=(\d+)")
# Los flujos vivos aparecen como ESTABLISHED **o** [OFFLOAD] (la flowtable de
# nftables se lleva el fast-path y esas entradas ni siquiera llevan estado —
# medido en b0 2026-08-01; el binario `conntrack` NO está instalado en las
# bases, de ahí leer /proc directamente). Estados de cierre/apertura fuera.
BAD_STATES = ("TIME_WAIT", "CLOSE", "SYN_", "FIN_", "LAST_ACK", "UNREPLIED")
CONNTRACK_PROC = "/proc/net/nf_conntrack"


def log(msg):
    print(f"welcome-boost: {msg}", flush=True)


def conntrack_flows(ports):
    """Set de (src, sport, dport) de flujos VIVOS con dport en el rango RDP."""
    flows = set()
    try:
        with open(CONNTRACK_PROC) as fh:
            for line in fh:
                if " tcp " not in line:
                    continue
                if "ESTABLISHED" not in line and "[OFFLOAD]" not in line:
                    continue
                if any(s in line for s in BAD_STATES):
                    continue
                m = TUPLE_RE.search(line)
                if not m:
                    continue
                dport = int(m.group(3))
                if dport in ports:
                    flows.add((m.group(1), int(m.group(2)), dport))
    except OSError as e:
        log(f"no puedo leer {CONNTRACK_PROC}: {e}")
    return flows


class StateFile:
    def __init__(self):
        self.mtime = 0.0
        self.data = {}

    def refresh(self):
        try:
            mt = os.stat(STATE_FILE).st_mtime
            if mt != self.mtime:
                with open(STATE_FILE) as fh:
                    self.data = {int(k): v for k, v in json.load(fh).items()}
                self.mtime = mt
        except Exception as e:  # noqa: BLE001
            log(f"state.json ilegible: {e}")
            self.data = {}  # stale ownership is not safe to act on
            self.mtime = 0.0

    def get(self, vmid):
        self.refresh()
        return self.data.get(vmid)


def node_addr(state, nodes):
    """Identity IPv6 is portable: only nodeId determines the current host."""
    addr = nodes.get(state.get('nodeId'))
    try:
        return str(ipaddress.ip_address(addr))
    except (ValueError, TypeError):
        return None


def ssh_out(node, script, timeout=15):
    r = subprocess.run(SSH + [f"root@{node}", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def query_vm(node, vmid, expected_node):
    """(memory_mb, floor_mb, actual_mb, avail_mb) o None si algo falla."""
    script = shlex.join(['python3', '/usr/local/sbin/neuravps-ram-guard.py',
                         'query', str(vmid), expected_node])
    try:
        rc, out = ssh_out(node, script)
    except Exception:  # noqa: BLE001
        return None
    if rc != 0:
        return None
    try:
        result = json.loads(out)
        return tuple(result) if isinstance(result, list) and len(result) == 4 else None
    except (ValueError, TypeError):
        return None


def boost_vm(node, vmid, expected_node):
    """Sembrar floor original + suelo=plan + empujón HMP. True si aplicado."""
    script = shlex.join(['python3', '/usr/local/sbin/neuravps-ram-guard.py',
                         'boost', str(vmid), expected_node])
    try:
        rc, out = ssh_out(node, script, timeout=30)
        result = json.loads(out)
        if rc == 0 and result.get('status') in ('boosted', 'full'):
            return True
        log(f"vm {vmid}: boost no aplicado: {result}")
        return False
    except Exception:  # noqa: BLE001
        return False


def main():
    hostname = socket.gethostname()
    if hostname not in ("0000000-BASE", "0000001-BASE"):
        log(f"{hostname} no es una BASE — saliendo")
        return 0

    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(CREDS))
    db = firestore.client()

    cfg = {}
    state = StateFile()
    prev_flows = set()
    handled = {}       # flow -> expiry
    skip_until = {}    # vmid -> (expiry, motivo)
    cycle = 0
    log(f"arrancado en {hostname} (poll {POLL_S}s)")

    while True:
        if cycle % CONFIG_EVERY == 0:
            try:
                cfg = (db.collection("config").document("welcomeboost")
                       .get().to_dict() or {})
            except Exception as e:  # noqa: BLE001
                log(f"config ilegible ({e}); mantengo la anterior")
        cycle += 1
        time.sleep(POLL_S)

        if cfg.get("enabled") is not True:
            prev_flows = set()
            continue
        dry = bool(cfg.get("dryRun"))

        now = time.time()
        for k in [k for k, exp in handled.items() if exp < now]:
            del handled[k]
        for v in [v for v, (exp, _) in skip_until.items() if exp < now]:
            del skip_until[v]

        state.refresh()
        ports = {PORT_MIN + v for v in state.data if 0 < v <= 65535 - PORT_MIN}
        try:
            with open(NODES_FILE) as fh:
                nodes = json.load(fh)
        except (OSError, ValueError) as exc:
            log(f"mapa de nodos ilegible: {exc}")
            prev_flows = set()
            continue
        flows = conntrack_flows(ports)
        qualified = (flows & prev_flows) - set(handled)
        prev_flows = flows

        queries = 0
        for flow in sorted(qualified):
            if queries >= MAX_QUERIES_PER_CYCLE:
                break  # el resto re-cualifica el ciclo siguiente
            vmid = flow[2] - PORT_MIN
            if vmid in skip_until:
                continue
            st = state.get(vmid)
            if not st:
                continue
            if not any(family in str(st.get('nodeId')) for family in ('-AX162', '-AX102')):
                handled[flow] = now + HANDLED_TTL_S
                continue  # dedicated EX44 guests have no dynamic RAM policy
            node = node_addr(st, nodes)
            if not node:
                skip_until[vmid] = (now + BLOCKED_TTL_S, "unresolved-node")
                log(f"vm {vmid}: nodeId no resoluble")
                continue
            queries += 1
            info = query_vm(node, vmid, st['nodeId'])
            if info is None:
                skip_until[vmid] = (now + BLOCKED_TTL_S, "query-fail")
                log(f"vm {vmid}: consulta fallida en {st['nodeId']}")
                continue
            memory, floor, actual, avail = info
            if actual >= memory - 1024:
                # A login during the boot window takes ownership of the boost;
                # the delayed boot restore must not undo a demand guarantee.
                try:
                    hold_rc, _ = ssh_out(node, shlex.join(['python3','/usr/local/sbin/neuravps-ram-guard.py',
                                                         'hold',str(vmid),st['nodeId']]))
                    if hold_rc:
                        continue  # retry next poll while the boot window is open
                except Exception:
                    continue
                handled[flow] = now + FULL_TTL_S
                skip_until[vmid] = (now + FULL_TTL_S, "ya-a-tope")
                continue
            need = memory - actual
            if avail < need + HOST_FREE_MIN_MB:
                skip_until[vmid] = (now + BLOCKED_TTL_S, "sin-headroom")
                log(f"vm {vmid}: login detectado pero nodo sin headroom "
                    f"(necesita {need}MB, avail {avail}MB) — no boost")
                continue
            if dry:
                handled[flow] = now + BOOST_COOLDOWN_S
                skip_until[vmid] = (now + BOOST_COOLDOWN_S, "dry")
                log(f"DRY-RUN: boostearía vm {vmid} en {node} "
                    f"({actual}->{memory}MB, suelo original {floor}MB)")
                continue
            if state.get(vmid) != st:
                continue  # ownership changed while querying
            if boost_vm(node, vmid, st['nodeId']):
                handled[flow] = now + HANDLED_TTL_S
                skip_until[vmid] = (now + BOOST_COOLDOWN_S, "boosted")
                log(f"BOOST vm {vmid}: {actual}->{memory}MB al detectar login "
                    f"(suelo original {floor}MB sembrado; nodo avail {avail}MB)")
            else:
                skip_until[vmid] = (now + BLOCKED_TTL_S, "boost-fail")
                log(f"vm {vmid}: boost FALLÓ en {node} — reintentable en 5 min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
