#!/usr/bin/env python3
"""neuravps-mt-freemem-guard — globo de las MT (AX102): proteger a quien va justo
y devolver con el tiempo lo que se subió.

Por qué existe (medido 2026-09-17, 25 nodos AX102, ~1.000 VMs MT):
  * En los AX102 NO corre el reconciliador (first_boot.sh lo instala solo en
    AX162). El globo de las MT lo mueve UNICAMENTE el autoballooning de
    pvestatd, que lleva el nodo a memused = MemTotal - MemAvailable = 80 %.
  * pvestatd es ciego a si el invitado va justo: encoge primero a quien tiene
    free_mem > 25 % del suelo (512 MB = 12,5 % en una MT de 4 GB) y, si no
    basta, a todos por shares. Y lee free_mem sin mirar last_update: un
    invitado congelado con un libre alto rancio se estruja el primero. En
    Windows el globo resta commit 1:1 (vm215, vm719).
  * Welcome-boost / boot guard suben el suelo a `memory` y en los AX102 NADIE
    lo baja: 192 MT por encima del suelo de plan (161 con registro en
    floors.json, 31 sin él).

Por qué NO se reutiliza el reconciliador v10 de los AX162: decide por tasa de
major_page_faults (que cuenta aciertos de zswap), sube/baja en pasos de 4 GB
(el doble de TODO el rango de globo de una MT de 4 GB), su tabla floor_target
no conoce 4096/8192 (caería al 60 %), no lee free_mem ni sesiones, y es bash
sin tests. Este guard decide con free_mem fresco, que es lo que pvestatd
ignora, en pasos de 512 MB.

Una pasada por minuto (timer), solo nodos -AX102, solo VMs <= MAX_VM_MB:
  STALE  stats ya vistas frescas y congeladas > STALE_S con globo retenido
         -> suelo = memory (firma del commit agotado).
  PIN    libre fresco < LOW_FREE_PCT con >= MIN_HELD_MB retenidos -> sube el
         suelo lo justo para volver a TARGET_FREE_PCT (min STEP_MIN_MB) y
         empuja el objetivo vivo por QMP.
  LOWER  (decay) suelo por encima de max(original, suelo de plan):
           - original = floors.json (lo siembran welcome-boost/ram-guard y
             este guard); sin registro se siembra el suelo de plan
             (memory/2, ram_min de mt y mt-plus) — criterio v8;
           - sin sesión RDP: conntrack del nodo (dport 3389 ESTABLISHED con
             paquete reciente hacia <prefijo>::<vmid hex>) y ninguna en
             NO_SESSION_S; si conntrack no está disponible, solo cuenta el
             tiempo desde la última subida (FALLBACK_AFTER_RAISE_S);
           - stats frescas, y tras bajar el paso seguiría >= RELEASE_FREE_PCT
             libre: crédito con fuga (+1 / -1, a 0 si baja de TARGET o hay
             sesión) hasta DECAY_CREDIT_RUNS, y AFTER_RAISE_S desde la última
             subida de quien sea (PIN_MIN_AGE_S si la subió este guard);
           - nunca por debajo de lo que garantiza TARGET_FREE_PCT aunque
             pvestatd estruje hasta el suelo nuevo;
           - paso STEP_DOWN_MB, luego DECAY_PACE_RUNS de crédito (1 h);
           - sin boost pendiente ni boot guard abierto; y solo si el nodo
             podría devolver el paso (MemAvailable >= reserva + paso).
         Un PIN/STALE dentro de REBOUND_WINDOW_S tras un LOWER = rebote ->
         decay bloqueado BACKOFF_BASE_S * 2^(n-1), máx BACKOFF_MAX_S.
  HOST   ninguna subida deja MemAvailable - pendientes < HOST_FREE_MIN_MB.

v3 (2026-09-17, canario 0000147): PROTECT (PIN/STALE) APAGADO por defecto.
  41 subidas en 9 min devolvieron ~40 GB de globo en un nodo donde casi todas
  las MT ya estaban en su suelo (2048): pvestatd no tenía a quién quitárselo y
  el HOST lo sacó a swap — swap usado 77 -> 95 GB, swap-in 2.979 pág/s (pares
  1-37) con MemAvailable estable, así que la reserva de 12 GB no lo vio.
  Revertido (42 suelos a su valor previo) y el swap-in cayó a 127/s en 2 min.
  Si se reactiva (PROTECT_ENABLED=1), dos frenos nuevos: ninguna subida si el
  host sacó a swap > HOST_SWAPOUT_MAX_PS pág/s en la pasada anterior, y como
  mucho RAISE_MB_PER_RUN por pasada para que la reacción llegue a tiempo.
  Pero la cuestión de fondo (en un nodo sin nadie estrujable, devolver globo
  = swap del host) necesita rediseño, no solo frenos.

Kill switch: /etc/default/neuravps-mt-freemem-guard
  ENABLED=0 no hace nada · DRY_RUN=1 (por defecto) decide y registra sin tocar
  nada · DECAY_ENABLED=0 sin decay · PROTECT_ENABLED=1 reactiva PIN/STALE
  (0 por defecto desde v3) · EXCLUDE_VMIDS=1,2 fuera del guard.
Telemetría: journal (SyslogIdentifier neuravps-mt-freemem-guard) y
/var/run/neuravps-mt-freemem-guard.json.
"""
import copy
import fcntl
import ipaddress
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

VERSION = 3
CONF_DIR = Path('/etc/pve/qemu-server')
STATE_DIR = Path('/var/lib/neuravps-balloon')
STATE_PATH = STATE_DIR / 'memguard.json'
FLOORS_PATH = STATE_DIR / 'floors.json'
PENDING_PATH = STATE_DIR / 'pending-boosts.json'
BOOT_GUARDS_PATH = STATE_DIR / 'boot-guards.json'
DRY_LOG_PATH = STATE_DIR / 'memguard-dry-log.json'
STATUS_PATH = Path('/var/run/neuravps-mt-freemem-guard.json')
LOCK_PATH = '/run/neuravps-ram.lock'
MIB = 1048576

DEFAULTS = {
    'ENABLED': 1,
    'DRY_RUN': 1,
    'DECAY_ENABLED': 1,
    'PROTECT_ENABLED': 0,
    'HOST_SWAPOUT_MAX_PS': 64,
    'RAISE_MB_PER_RUN': 1024,
    'LOW_FREE_PCT': 15,
    'TARGET_FREE_PCT': 25,
    'RELEASE_FREE_PCT': 35,
    'MIN_HELD_MB': 256,
    'STEP_MIN_MB': 512,
    'STEP_DOWN_MB': 512,
    'ROUND_MB': 256,
    'STALE_S': 180,
    'MAX_VM_MB': 8192,
    'HOST_FREE_MIN_MB': 12288,
    'MAX_CHANGES_PER_RUN': 8,
    'DECAY_CREDIT_RUNS': 120,
    'DECAY_PACE_RUNS': 60,
    'AFTER_RAISE_S': 7200,
    'FALLBACK_AFTER_RAISE_S': 21600,
    'PIN_MIN_AGE_S': 86400,
    'NO_SESSION_S': 7200,
    'SESSION_IDLE_S': 600,
    'REBOUND_WINDOW_S': 86400,
    'BACKOFF_BASE_S': 172800,
    'BACKOFF_MAX_S': 1209600,
    'FORCE_SCOPE': 0,
}
OBSERVED = ('uuid', 'fresh_seen', 'last_floor', 'raised_at', 'last_session', 'credit')


def load_cfg(path='/etc/default/neuravps-mt-freemem-guard', env=None):
    cfg = dict(DEFAULTS)
    cfg['EXCLUDE_VMIDS'] = set()
    env = os.environ if env is None else env
    raw = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                raw[k.strip()] = v.strip().strip('"\'')
    except FileNotFoundError:
        pass
    raw.update({k: v for k, v in env.items() if k in cfg})
    for k, v in raw.items():
        if k == 'EXCLUDE_VMIDS':
            cfg[k] = {x.strip() for x in v.split(',') if x.strip()}
        elif k in DEFAULTS:
            cfg[k] = int(v)
    return cfg


def log(msg):
    print(msg, flush=True)


def round_up(mb, step):
    return int(math.ceil(mb / step) * step)


def plan_floor(memory):
    return memory // 2  # ram_min of mt (4 -> 2 GB) and mt-plus (8 -> 4 GB)


def new_entry(vm):
    return {'uuid': vm.get('uuid'), 'fresh_seen': False, 'last_floor': None,
            'raised_at': 0, 'last_session': 0, 'credit': 0,
            'last_pin': 0, 'last_lower': 0, 'bounces': 0, 'block_until': 0}


def observe(vm, entry, now, session, cfg):
    """Update the passive facts about a VM. Safe to keep in dry-run."""
    if entry.get('uuid') != vm.get('uuid'):  # VMID reused by a different VM
        entry.clear()
        entry.update(new_entry(vm))
    floor = vm['floor']
    if entry['last_floor'] is None:
        # First sight: whatever raised it happened at an unknown time — start
        # the clock now (conservative), never in the past.
        entry['raised_at'] = now
    elif floor > entry['last_floor']:
        entry['raised_at'] = now
        entry['credit'] = 0
    entry['last_floor'] = floor
    fresh = vm['total'] > 0 and vm['age'] is not None and vm['age'] <= cfg['STALE_S']
    if fresh:
        entry['fresh_seen'] = True
    if session:
        entry['last_session'] = now
    return fresh


def decide(vm, entry, now, cfg, ctx):
    """One VM, one tick. ctx: {orig, session, session_known, fresh, pending,
    boot_guard}. Returns (action, new_floor, reason); action in
    stale|pin|lower|None. Mutates entry (action fields + credit)."""
    mem, floor, actual = vm['memory'], vm['floor'], vm['actual']
    held = max(0, mem - actual)

    def raise_to(new, reason, kind):
        if entry['last_lower'] and now - entry['last_lower'] <= cfg['REBOUND_WINDOW_S']:
            entry['bounces'] += 1
            backoff = min(cfg['BACKOFF_MAX_S'], cfg['BACKOFF_BASE_S'] << min(entry['bounces'] - 1, 8))
            entry['block_until'] = now + backoff
            reason += f"; REBOTE #{entry['bounces']} ({(now - entry['last_lower']) // 60} min tras bajar), decay bloqueado {backoff // 3600}h"
            entry['last_lower'] = 0
        entry['last_pin'] = now
        entry['credit'] = 0
        return kind, new, reason

    if not ctx['fresh']:
        if entry['fresh_seen'] and vm['total'] > 0 and held >= cfg['MIN_HELD_MB'] and floor < mem:
            return raise_to(mem, f"stats congeladas {vm['age']}s con {held}MB retenidos", 'stale')
        return None, floor, 'sin stats frescas'

    free_pct = 100.0 * vm['free'] / vm['total']
    if free_pct < cfg['LOW_FREE_PCT'] and held >= cfg['MIN_HELD_MB'] and floor < mem:
        deficit = cfg['TARGET_FREE_PCT'] / 100.0 * vm['total'] - vm['free']
        grow = max(cfg['STEP_MIN_MB'], round_up(deficit, cfg['ROUND_MB']))
        new = min(mem, max(floor, round_up(actual + grow, cfg['ROUND_MB'])))
        if new > floor:
            return raise_to(new, f"libre {free_pct:.1f}% ({vm['free']}MB) con {held}MB retenidos", 'pin')

    target = max(ctx['orig'], plan_floor(mem))
    if not cfg['DECAY_ENABLED'] or floor <= target:
        return None, floor, ''
    after_pct = 100.0 * (vm['free'] - cfg['STEP_DOWN_MB']) / vm['total']
    if ctx['session'] or free_pct < cfg['TARGET_FREE_PCT']:
        entry['credit'] = 0
    elif after_pct >= cfg['RELEASE_FREE_PCT']:
        entry['credit'] += 1
    elif entry['credit'] > 0:
        entry['credit'] -= 1
    if entry['credit'] < cfg['DECAY_CREDIT_RUNS'] or now < entry['block_until']:
        return None, floor, 'credito'
    if ctx['pending'] or ctx['boot_guard']:
        return None, floor, 'boost en curso'
    if ctx['session_known']:
        if entry['last_session'] and now - entry['last_session'] < cfg['NO_SESSION_S']:
            return None, floor, 'sesion reciente'
        if now - entry['raised_at'] < cfg['AFTER_RAISE_S']:
            return None, floor, 'subida reciente'
    elif now - entry['raised_at'] < cfg['FALLBACK_AFTER_RAISE_S']:
        return None, floor, 'subida reciente (sin deteccion de sesion)'
    if entry['last_pin'] and now - entry['last_pin'] < cfg['PIN_MIN_AGE_S']:
        return None, floor, 'protegida hace poco'
    # Even if pvestatd squeezes it right down to the new floor, keep TARGET free.
    keep = round_up(actual - (vm['free'] - cfg['TARGET_FREE_PCT'] / 100.0 * vm['total']), cfg['ROUND_MB'])
    new = max(target, floor - cfg['STEP_DOWN_MB'], keep)
    if new >= floor:
        return None, floor, 'sin margen'
    entry['credit'] = max(0, cfg['DECAY_CREDIT_RUNS'] - cfg['DECAY_PACE_RUNS'])
    entry['last_lower'] = now
    return 'lower', new, f"libre {free_pct:.1f}%, sin sesion; objetivo {target}MB"


def decay_step(vm, ctx, cfg):
    """Instantaneous decay check, clocks aside: the floor a LOWER would set now,
    or None. Needs fresh stats, no live session, >= RELEASE_FREE_PCT free after
    the step, and never under what keeps TARGET_FREE_PCT if squeezed to it."""
    mem, floor = vm['memory'], vm['floor']
    target = max(ctx['orig'], plan_floor(mem))
    if floor <= target or not ctx['fresh'] or ctx['session'] or vm['total'] <= 0:
        return None
    if 100.0 * (vm['free'] - cfg['STEP_DOWN_MB']) / vm['total'] < cfg['RELEASE_FREE_PCT']:
        return None
    keep = round_up(vm['actual'] - (vm['free'] - cfg['TARGET_FREE_PCT'] / 100.0 * vm['total']), cfg['ROUND_MB'])
    new = max(target, floor - cfg['STEP_DOWN_MB'], keep)
    return new if new < floor else None


def host_allows(delta_mb, avail_mb, pending_mb, cfg):
    return avail_mb - pending_mb - delta_mb >= cfg['HOST_FREE_MIN_MB']


# ---------------------------------------------------------------- node I/O --

def read_conf(vmid):
    fields = {}
    for line in (CONF_DIR / f'{vmid}.conf').read_text().splitlines():
        if line.startswith('['):
            break
        k, sep, v = line.partition(':')
        if sep:
            fields[k.strip()] = v.strip()
    return fields


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def mem_available_mb():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return 0


def pending_boosts(now):
    """({vmid: mb} of live demand-boost reservations, total mb)."""
    try:
        data = json.loads(PENDING_PATH.read_text())
        live = {k: v['mb'] for k, v in data.items() if v['until'] > now}
        return live, sum(live.values())
    except FileNotFoundError:
        return {}, 0
    except Exception:
        return {}, 1 << 30  # unreadable reservations are never free capacity


def parse_rdp_sessions(text, established_timeout, idle_s):
    """VMIDs with a live RDP flow in `conntrack -L -p tcp --dport 3389` output.

    Guests are reached at <prefix>::<vmid in hex>; a flow counts when it is
    ESTABLISHED and saw a packet in the last idle_s (its timeout is re-armed to
    nf_conntrack_tcp_timeout_established on every packet)."""
    vmids = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != 'tcp' or parts[3] != 'ESTABLISHED':
            continue
        try:
            remaining = int(parts[2])
        except ValueError:
            continue
        if remaining < established_timeout - idle_s:
            continue
        dst = next((p[4:] for p in parts if p.startswith('dst=')), None)
        dport = next((p[6:] for p in parts if p.startswith('dport=')), None)
        if not dst or dport != '3389':
            continue
        try:
            iid = int(ipaddress.ip_address(dst)) & 0xFFFFFFFFFFFFFFFF
        except ValueError:
            continue
        if 0 < iid < 1 << 20:
            vmids.add(str(iid))
    return vmids


def rdp_sessions(cfg):
    """(set of vmids, detection_available)."""
    try:
        est = int(Path('/proc/sys/net/netfilter/nf_conntrack_tcp_timeout_established').read_text())
        out = subprocess.run(['conntrack', '-L', '-p', 'tcp', '--dport', '3389'],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return set(), False
        return parse_rdp_sessions(out.stdout, est, cfg['SESSION_IDLE_S']), True
    except Exception:
        return set(), False


def host_swapout_rate(now):
    """pswpout pages/s since the previous run (0 on the first run). An unreadable
    previous sample counts as swapping: a raise is never the default."""
    path = STATE_DIR / 'memguard-vmstat.json'
    cur = 0
    for line in Path('/proc/vmstat').read_text().splitlines():
        if line.startswith('pswpout '):
            cur = int(line.split()[1])
    try:
        prev = json.loads(path.read_text())
        rate = (cur - prev['pswpout']) / max(1, now - prev['ts'])
        rate = int(rate) if 0 < now - prev['ts'] <= 600 and cur >= prev['pswpout'] else 1 << 30
    except FileNotFoundError:
        rate = 1 << 30
    except Exception:
        rate = 1 << 30
    atomic_json(path, {'pswpout': cur, 'ts': now})
    return rate


def collect(node, now):
    out = subprocess.run(['pvesh', 'get', f'/nodes/{node}/qemu', '--full', '1',
                          '--output-format', 'json'], capture_output=True, text=True,
                         timeout=90, check=True).stdout
    vms = []
    for v in json.loads(out or '[]'):
        if v.get('status') != 'running':
            continue
        vmid = str(v['vmid'])
        try:
            conf = read_conf(vmid)
            mem = int(conf.get('memory', 0))
            floor = int(conf.get('balloon', 0) or 0)
        except (FileNotFoundError, ValueError):
            continue
        bi = v.get('ballooninfo') or {}
        vms.append({'vmid': vmid, 'memory': mem, 'floor': floor, 'lock': conf.get('lock'),
                    'shares': conf.get('shares'), 'uuid': conf.get('smbios1'),
                    'actual': int(bi.get('actual') or 0) // MIB,
                    'total': int(bi.get('total_mem') or 0) // MIB,
                    'free': int(bi.get('free_mem') or 0) // MIB,
                    'age': (now - int(bi['last_update'])) if bi.get('last_update') else None})
    return vms


def qmp_balloon(vmid, value_mb):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.connect(f'/var/run/qemu-server/{vmid}.qmp')
        stream = sock.makefile('rb')
        json.loads(stream.readline())
        for ident, execute, args in (('cap', 'qmp_capabilities', None),
                                     ('req', 'balloon', {'value': value_mb * MIB})):
            msg = {'execute': execute, 'id': ident}
            if args is not None:
                msg['arguments'] = args
            sock.sendall((json.dumps(msg) + '\n').encode())
            while True:
                reply = json.loads(stream.readline())
                if reply.get('id') == ident:
                    if 'error' in reply:
                        raise RuntimeError(str(reply['error']))
                    break


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def apply_change(vm, new_floor, action, orig):
    """Write the floor under the shared RAM lock (welcome-boost/ram-guard use
    the same one), re-verifying the config first. Raises on any doubt."""
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conf = read_conf(vm['vmid'])
        if conf.get('lock') or int(conf.get('balloon', 0) or 0) != vm['floor']:
            raise RuntimeError('config changed or locked')
        if action == 'lower':
            if read_json(PENDING_PATH, {}).get(vm['vmid'], {}).get('until', 0) > time.time():
                raise RuntimeError('boost pendiente')
            if vm['vmid'] in read_json(BOOT_GUARDS_PATH, {}):
                raise RuntimeError('boot guard abierto')
        floors = read_json(FLOORS_PATH, {})
        if vm['vmid'] not in floors:  # remember the original before the first touch
            floors[vm['vmid']] = orig
            atomic_json(FLOORS_PATH, floors)
        subprocess.run(['qm', 'set', vm['vmid'], '--balloon', str(new_floor)],
                       check=True, capture_output=True, timeout=20)
    if action in ('pin', 'stale'):
        qmp_balloon(vm['vmid'], new_floor)


def run(cfg, node, now, vms, state, avail_mb, pending, floors, sessions, session_known,
        boot_guards, apply=apply_change, dry_seen=None, swapout_ps=0):
    """One tick. Mutates state (and dry_seen); returns the status dict."""
    pending_map, pending_mb = pending
    dry_seen = {} if dry_seen is None else dry_seen
    status = {'ts': now, 'version': VERSION, 'dryRun': bool(cfg['DRY_RUN']), 'node': node,
              'decayEnabled': bool(cfg['DECAY_ENABLED']), 'availMb': avail_mb,
              'pendingMb': pending_mb, 'reserveMb': cfg['HOST_FREE_MIN_MB'],
              'sessionDetection': session_known, 'rdpSessions': len(sessions),
              'protectEnabled': bool(cfg['PROTECT_ENABLED']), 'hostSwapoutPs': swapout_ps,
              'blockedSwap': 0,
              'raised': [], 'lowered': [], 'blockedHost': [], 'errors': [],
              'noStats': 0, 'stale': 0, 'lowFreeFixed': 0, 'aboveTarget': 0,
              'aboveTargetMb': 0, 'bounces': 0, 'decayWouldQualify': 0,
              'decayWouldQualifyMb': 0}
    seen = set()
    decisions = []
    for vm in vms:
        if (vm['lock'] or vm['memory'] <= 0 or vm['memory'] > cfg['MAX_VM_MB'] or vm['floor'] <= 0
                or vm['vmid'] in cfg['EXCLUDE_VMIDS']
                or (vm['shares'] is not None and str(vm['shares']) == '0')):
            continue
        seen.add(vm['vmid'])
        entry = state.setdefault(vm['vmid'], new_entry(vm))
        fresh = observe(vm, entry, now, vm['vmid'] in sessions, cfg)
        if vm['total'] <= 0:
            status['noStats'] += 1
        elif not fresh:
            status['stale'] += 1
        elif vm['floor'] >= vm['memory'] and 100.0 * vm['free'] / vm['total'] < cfg['LOW_FREE_PCT']:
            status['lowFreeFixed'] += 1  # all its RAM and still tight: plan load, not us
        orig = floors.get(vm['vmid'])
        orig = plan_floor(vm['memory']) if orig is None else int(orig)
        target = max(orig, plan_floor(vm['memory']))
        if vm['floor'] > target:
            status['aboveTarget'] += 1
            status['aboveTargetMb'] += vm['floor'] - target
        status['bounces'] += entry['bounces']
        ctx = {'orig': orig, 'session': vm['vmid'] in sessions, 'session_known': session_known,
               'fresh': fresh, 'pending': vm['vmid'] in pending_map,
               'boot_guard': vm['vmid'] in boot_guards}
        would = decay_step(vm, ctx, cfg)
        if would is not None:
            status['decayWouldQualify'] += 1
            status['decayWouldQualifyMb'] += vm['floor'] - would
        snapshot = copy.deepcopy(entry)
        action, new, reason = decide(vm, entry, now, cfg, ctx)
        if action:
            rank = {'stale': 0, 'pin': 1, 'lower': 2}[action]
            decisions.append((rank, (vm['free'] / vm['total']) if vm['total'] else 0,
                              vm, entry, snapshot, action, new, reason, orig))
    decisions.sort(key=lambda d: (d[0], d[1]))
    granted = 0
    changes = 0
    for _, _, vm, entry, snapshot, action, new, reason, orig in decisions:
        vmid = vm['vmid']

        def rollback():
            keep_credit = entry['credit'] if action != 'lower' else snapshot['credit']
            entry.clear()
            entry.update(snapshot)
            entry['credit'] = keep_credit

        if changes >= cfg['MAX_CHANGES_PER_RUN']:
            rollback()
            continue
        raising = action in ('stale', 'pin')
        delta = max(0, new - vm['actual']) if raising else cfg['STEP_DOWN_MB']
        if raising and (not cfg['PROTECT_ENABLED'] or swapout_ps > cfg['HOST_SWAPOUT_MAX_PS']
                        or (granted > 0 and granted + delta > cfg['RAISE_MB_PER_RUN'])):
            rollback()
            if cfg['PROTECT_ENABLED']:
                status['blockedSwap'] += 1
            continue
        # A raise must fit the reserve; a lower must leave room to undo itself.
        if not host_allows(granted + delta, avail_mb, pending_mb, cfg):
            rollback()
            if raising:
                status['blockedHost'].append({'vmid': int(vmid), 'wantMb': new, 'deltaMb': delta})
                log(f"BLOCKED vm {vmid} {action} {vm['floor']}->{new}MB: avail {avail_mb}MB - "
                    f"pendiente {pending_mb + granted}MB < reserva {cfg['HOST_FREE_MIN_MB']}+{delta}MB ({reason})")
            continue
        tag = ''
        quiet = False
        if cfg['DRY_RUN']:
            tag = 'DRY '
            key = f"{vmid}:{action}:{new}"
            quiet = now - dry_seen.get(key, 0) < 3600
            if not quiet:
                dry_seen[key] = now
        else:
            try:
                apply(vm, new, action, orig)
            except Exception as exc:  # lock busy, config changed, qm/QMP failure
                rollback()
                status['errors'].append({'vmid': int(vmid), 'action': action, 'error': str(exc)[:200]})
                log(f"ERROR vm {vmid} {action} {vm['floor']}->{new}MB: {exc}")
                continue
            entry['last_floor'] = new
        changes += 1
        item = {'vmid': int(vmid), 'kind': action, 'fromMb': vm['floor'], 'toMb': new,
                'actualMb': vm['actual'], 'freeMb': vm['free'], 'totalMb': vm['total'],
                'bounces': entry['bounces']}
        if raising:
            granted += delta
            status['raised'].append(item)
        else:
            status['lowered'].append(item)
        if not quiet:
            log(f"{tag}{action.upper()} vm {vmid} suelo {vm['floor']}->{new}MB ({reason})")
    if cfg['DRY_RUN']:
        # Keep what was OBSERVED (credit, sessions, raise times) so enabling
        # later does not restart the clocks; drop what only an action creates.
        for vmid, e in state.items():
            obs = {k: e.get(k) for k in OBSERVED}
            e.clear()
            e.update(new_entry({'uuid': obs['uuid']}))
            e.update(obs)
    for vmid in list(state):
        if vmid not in seen and not state[vmid].get('bounces'):
            del state[vmid]  # gone/stopped and nothing worth remembering
    return status


def main():
    cfg = load_cfg()
    if not cfg['ENABLED']:
        return 0
    node = socket.gethostname()
    if '-AX102' not in node and not cfg['FORCE_SCOPE']:
        return 0
    now = int(time.time())
    state = read_json(STATE_PATH, {})
    vms = collect(node, now)
    sessions, session_known = rdp_sessions(cfg)
    dry_seen = {k: v for k, v in read_json(DRY_LOG_PATH, {}).items() if now - v < 3600}
    swapout_ps = host_swapout_rate(now)
    status = run(cfg, node, now, vms, state, mem_available_mb(), pending_boosts(now),
                 read_json(FLOORS_PATH, {}), sessions, session_known,
                 read_json(BOOT_GUARDS_PATH, {}), dry_seen=dry_seen, swapout_ps=swapout_ps)
    if cfg['DRY_RUN']:
        atomic_json(DRY_LOG_PATH, dry_seen)
    atomic_json(STATE_PATH, state)
    atomic_json(STATUS_PATH, status)
    os.chmod(STATUS_PATH, 0o644)
    return 0


if __name__ == '__main__':
    sys.exit(main())
