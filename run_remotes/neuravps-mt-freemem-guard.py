#!/usr/bin/env python3
"""neuravps-mt-freemem-guard — no retener globo a un invitado MT que ya va justo.

Por qué existe (medido 2026-09-17, 25 nodos AX102, 1.012 VMs MT):
  * En los AX102 NO corre el reconciliador de globo (first_boot.sh lo instala
    solo en AX162). El globo de las MT lo mueve UNICAMENTE el autoballooning de
    pvestatd, que lleva cada nodo a `memused = MemTotal - MemAvailable` = 80 %
    (ballooning-target por defecto). Los 25 nodos estaban en 72-80 %.
  * pvestatd no mira si el invitado va justo: al encoger prioriza a quien tiene
    `free_mem > 25 % del suelo` (512 MB en una MT de 4 GB con suelo 2048, o sea
    un 12,5 % libre) y, si no basta, estruja a TODOS por shares. Y lee
    `free_mem` sin mirar `last_update`: un invitado atascado con estadísticas
    congeladas en un valor alto se estruja el primero.
  * En Windows el globo resta commit 1:1 (vm215: r=0,984). 87 MT tenían <15 %
    libre con globo retenido (41,5 GB) y 5 nodos (45/48/49/51/142) concentraban
    casi todo.

Qué hace (timer de 1 min, solo en nodos -AX102, solo VMs <= MAX_VM_MB):
  PIN     libre fresco < LOW_FREE_PCT y globo retenido >= MIN_HELD_MB
          -> sube el SUELO (`balloon:`) lo justo para volver a TARGET_FREE_PCT
             (mínimo STEP_MIN_MB, nunca por encima de `memory`) y empuja el
             objetivo vivo por QMP. pvestatd ya no puede bajar de ese suelo.
  STALE   estadísticas que YA vimos frescas y llevan > STALE_S congeladas con
          globo retenido (firma del commit agotado) -> suelo = memory.
  RELEASE solo lo que subió ESTE guard y nadie ha tocado después: crédito
          ocioso con fuga (+1 si tras soltar RELEASE_STEP_MB seguiría con
          >= RELEASE_FREE_PCT libre, -1 si no, 0 si baja de TARGET) y además
          RELEASE_MIN_AGE_S desde el último PIN. Baja de RELEASE_STEP_MB en
          RELEASE_STEP_MB hasta el suelo original, con 1 h entre pasos.
          Un PIN dentro de REBOUND_WINDOW_S tras soltar = rebote -> bloqueo de
          suelta exponencial (48 h, x2, máx 14 d). Es la autopsia de v5: no se
          devuelve lo que se sabe necesario sin memoria de rebotes.
  HOST    nunca deja MemAvailable por debajo de HOST_FREE_MIN_MB (mismo 12 GB
          que el reconciliador y ram-guard), descontando boosts pendientes.

Qué NO hace: no toca invitados por dentro, no toca VMs sin globo, no pelea con
welcome-boost/boot guard (si el suelo ya no es el que puso el guard, suelta la
propiedad y no lo baja nunca), no corre en AX162/EX44.

Kill switch: /etc/default/neuravps-mt-freemem-guard  ENABLED=0 (no hace nada)
             DRY_RUN=1 (por defecto: decide, registra y escribe el estado, sin
             tocar nada). Telemetría: journal SyslogIdentifier
             neuravps-mt-freemem-guard + /var/run/neuravps-mt-freemem-guard.json
"""
import fcntl
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1
CONF_DIR = Path('/etc/pve/qemu-server')
STATE_DIR = Path('/var/lib/neuravps-balloon')
STATE_PATH = STATE_DIR / 'memguard.json'
STATUS_PATH = Path('/var/run/neuravps-mt-freemem-guard.json')
PENDING_PATH = STATE_DIR / 'pending-boosts.json'
LOCK_PATH = '/run/neuravps-ram.lock'
MIB = 1048576

DEFAULTS = {
    'ENABLED': 1,
    'DRY_RUN': 1,
    'LOW_FREE_PCT': 15,
    'TARGET_FREE_PCT': 25,
    'RELEASE_FREE_PCT': 35,
    'MIN_HELD_MB': 256,
    'STEP_MIN_MB': 512,
    'ROUND_MB': 256,
    'STALE_S': 180,
    'MAX_VM_MB': 8192,
    'HOST_FREE_MIN_MB': 12288,
    'MAX_CHANGES_PER_RUN': 8,
    'RELEASE_ENABLED': 1,
    'RELEASE_CREDIT_RUNS': 360,
    'RELEASE_PACE_RUNS': 60,
    'RELEASE_STEP_MB': 512,
    'RELEASE_MIN_AGE_S': 86400,
    'REBOUND_WINDOW_S': 86400,
    'BACKOFF_BASE_S': 172800,
    'BACKOFF_MAX_S': 1209600,
    'FORCE_SCOPE': 0,
}


def load_cfg(path='/etc/default/neuravps-mt-freemem-guard', env=None):
    cfg = dict(DEFAULTS)
    env = os.environ if env is None else env
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                if k.strip() in cfg:
                    cfg[k.strip()] = int(v.strip().strip('"\''))
    except FileNotFoundError:
        pass
    for k in cfg:
        if k in env:
            cfg[k] = int(env[k])
    return cfg


def log(msg):
    print(msg, flush=True)


def round_up(mb, step):
    return int(math.ceil(mb / step) * step)


def new_entry(vm):
    return {'uuid': vm.get('uuid'), 'owned': False, 'orig': None, 'pinned': None,
            'last_pin': 0, 'last_release': 0, 'credit': 0, 'bounces': 0,
            'block_until': 0, 'fresh_seen': False}


def decide(vm, entry, now, cfg):
    """Pure decision for one running VM.

    vm: {vmid, memory, floor, actual, total, free, age, uuid}
        (MB; total/free = virtio-balloon guest stats, age = now - last_update,
        None when the guest never reported stats)
    entry: this guard's per-VM state (mutated in place and returned).
    Returns (action, new_floor, reason) with action in pin|stale|release|None.
    """
    if entry.get('uuid') != vm.get('uuid'):  # VMID reused by a different VM
        entry.clear()
        entry.update(new_entry(vm))
    mem, floor, actual = vm['memory'], vm['floor'], vm['actual']
    held = max(0, mem - actual)

    # Someone else moved the floor since we pinned it (welcome-boost, boot
    # guard, operator, reconciler after a migration): it is theirs now.
    if entry['owned'] and floor != entry['pinned']:
        entry['owned'] = False
        entry['credit'] = 0

    fresh = vm['total'] > 0 and vm['age'] is not None and vm['age'] <= cfg['STALE_S']
    if fresh:
        entry['fresh_seen'] = True

    def pin(new, reason, kind):
        if entry['last_release'] and now - entry['last_release'] <= cfg['REBOUND_WINDOW_S']:
            entry['bounces'] += 1
            backoff = min(cfg['BACKOFF_MAX_S'], cfg['BACKOFF_BASE_S'] << min(entry['bounces'] - 1, 8))
            entry['block_until'] = now + backoff
            entry['last_release'] = 0
            reason += f"; rebote #{entry['bounces']}, suelta bloqueada {backoff // 3600}h"
        if not entry['owned']:
            entry['orig'] = floor
        entry['owned'] = True
        entry['pinned'] = new
        entry['last_pin'] = now
        entry['credit'] = 0
        return kind, new, reason

    if not fresh:
        if entry['fresh_seen'] and vm['total'] > 0 and held >= cfg['MIN_HELD_MB'] and floor < mem:
            return pin(mem, f"stats congeladas {vm['age']}s con {held}MB retenidos", 'stale')
        return None, floor, 'sin stats frescas'

    free_pct = 100.0 * vm['free'] / vm['total']
    if free_pct < cfg['LOW_FREE_PCT'] and held >= cfg['MIN_HELD_MB'] and floor < mem:
        deficit = cfg['TARGET_FREE_PCT'] / 100.0 * vm['total'] - vm['free']
        grow = max(cfg['STEP_MIN_MB'], round_up(deficit, cfg['ROUND_MB']))
        new = min(mem, max(floor, round_up(actual + grow, cfg['ROUND_MB'])))
        if new > floor:
            return pin(new, f"libre {free_pct:.1f}% ({vm['free']}MB) con {held}MB retenidos", 'pin')

    if not (entry['owned'] and cfg['RELEASE_ENABLED']):
        return None, floor, ''
    after_pct = 100.0 * (vm['free'] - cfg['RELEASE_STEP_MB']) / vm['total']
    if free_pct < cfg['TARGET_FREE_PCT']:
        entry['credit'] = 0
    elif after_pct >= cfg['RELEASE_FREE_PCT']:
        entry['credit'] += 1
    elif entry['credit'] > 0:
        entry['credit'] -= 1
    if (entry['credit'] >= cfg['RELEASE_CREDIT_RUNS'] and now >= entry['block_until']
            and now - entry['last_pin'] >= cfg['RELEASE_MIN_AGE_S']):
        new = max(entry['orig'], floor - cfg['RELEASE_STEP_MB'])
        if new < floor:
            entry['credit'] = max(0, cfg['RELEASE_CREDIT_RUNS'] - cfg['RELEASE_PACE_RUNS'])
            entry['last_release'] = now
            entry['pinned'] = new
            if new <= entry['orig']:
                entry['owned'] = False
            return 'release', new, f"libre {free_pct:.1f}% sostenido; credito {cfg['RELEASE_CREDIT_RUNS']}"
    return None, floor, ''


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


def mem_available_mb():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return 0


def pending_boosts_mb(now):
    try:
        data = json.loads(PENDING_PATH.read_text())
        return sum(v['mb'] for v in data.values() if v['until'] > now)
    except FileNotFoundError:
        return 0
    except Exception:
        return 1 << 30  # unreadable reservations are never free capacity


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


def apply_change(vm, new_floor, push):
    """Write the floor under the shared RAM lock; re-verify the config first."""
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conf = read_conf(vm['vmid'])
        if conf.get('lock') or int(conf.get('balloon', 0) or 0) != vm['floor']:
            raise RuntimeError('config changed or locked')
        subprocess.run(['qm', 'set', vm['vmid'], '--balloon', str(new_floor)],
                       check=True, capture_output=True, timeout=20)
    if push:
        qmp_balloon(vm['vmid'], new_floor)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def run(cfg, node, now, vms, state, avail_mb, pending_mb, apply=apply_change, dry_seen=None):
    """One tick. Returns the status dict; mutates state."""
    dry_seen = {} if dry_seen is None else dry_seen
    status = {'ts': now, 'version': VERSION, 'dryRun': bool(cfg['DRY_RUN']), 'node': node,
              'availMb': avail_mb, 'pendingMb': pending_mb, 'reserveMb': cfg['HOST_FREE_MIN_MB'],
              'pinned': [], 'released': [], 'blockedHost': [], 'errors': [],
              'noStats': 0, 'stale': 0, 'lowFreeFixed': 0}
    seen = set()
    decisions = []
    for vm in vms:
        if vm['lock'] or vm['memory'] <= 0 or vm['memory'] > cfg['MAX_VM_MB'] or vm['floor'] <= 0:
            continue
        if vm['shares'] is not None and str(vm['shares']) == '0':
            continue  # manual ballooning on purpose (tests, operator)
        seen.add(vm['vmid'])
        if vm['total'] <= 0:
            status['noStats'] += 1
        elif vm['age'] is None or vm['age'] > cfg['STALE_S']:
            status['stale'] += 1
        elif vm['floor'] >= vm['memory'] and 100.0 * vm['free'] / vm['total'] < cfg['LOW_FREE_PCT']:
            status['lowFreeFixed'] += 1  # all its RAM and still tight: plan load, not us
        entry = state.setdefault(vm['vmid'], new_entry(vm))
        snapshot = json.dumps(entry, sort_keys=True)
        action, new, reason = decide(vm, entry, now, cfg)
        if action:
            decisions.append((0 if action == 'stale' else 1 if action == 'pin' else 2,
                              (vm['free'] / vm['total']) if vm['total'] else 0,
                              vm, entry, snapshot, action, new, reason))
    decisions.sort(key=lambda d: (d[0], d[1]))
    granted = 0
    changes = 0
    for _, _, vm, entry, snapshot, action, new, reason in decisions:
        vmid = vm['vmid']
        rollback = lambda: (entry.clear(), entry.update(json.loads(snapshot)))
        if changes >= cfg['MAX_CHANGES_PER_RUN']:
            rollback()
            continue
        if action in ('pin', 'stale'):
            delta = max(0, new - vm['actual'])
            if not host_allows(granted + delta, avail_mb, pending_mb, cfg):
                rollback()
                status['blockedHost'].append({'vmid': int(vmid), 'wantMb': new, 'deltaMb': delta})
                log(f"BLOCKED vm {vmid} {action} {vm['floor']}->{new}MB: host avail {avail_mb}MB "
                    f"- pendiente {pending_mb + granted}MB < reserva {cfg['HOST_FREE_MIN_MB']}MB + {delta}MB ({reason})")
                continue
        tag = 'DRY ' if cfg['DRY_RUN'] else ''
        quiet = False
        if cfg['DRY_RUN']:
            key = f"{vmid}:{action}:{new}"
            quiet = now - dry_seen.get(key, 0) < 3600
            if not quiet:
                dry_seen[key] = now
        if not cfg['DRY_RUN']:
            try:
                apply(vm, new, push=action in ('pin', 'stale'))
            except Exception as exc:  # lock busy, config changed, qm/QMP failure
                rollback()
                status['errors'].append({'vmid': int(vmid), 'action': action, 'error': str(exc)[:200]})
                log(f"ERROR vm {vmid} {action} {vm['floor']}->{new}MB: {exc}")
                continue
        changes += 1
        if action in ('pin', 'stale'):
            granted += max(0, new - vm['actual'])
            status['pinned'].append({'vmid': int(vmid), 'kind': action, 'fromMb': vm['floor'],
                                     'toMb': new, 'actualMb': vm['actual'], 'freeMb': vm['free'],
                                     'totalMb': vm['total'], 'bounces': entry['bounces']})
            quiet or log(f"{tag}{action.upper()} vm {vmid} suelo {vm['floor']}->{new}MB ({reason})")
        else:
            status['released'].append({'vmid': int(vmid), 'fromMb': vm['floor'], 'toMb': new})
            quiet or log(f"{tag}RELEASE vm {vmid} suelo {vm['floor']}->{new}MB ({reason})")
    if cfg['DRY_RUN']:
        # A dry run must not accumulate ownership it never exercised: keep only
        # the passive facts (uuid, fresh_seen) so enabling later starts clean.
        for vmid, e in list(state.items()):
            keep = new_entry({'uuid': e.get('uuid')})
            keep['fresh_seen'] = e.get('fresh_seen', False)
            state[vmid] = keep
    for vmid in list(state):
        if vmid not in seen and not state[vmid].get('owned') and not state[vmid].get('bounces'):
            del state[vmid]  # gone/stopped and nothing worth remembering
    owned = [(k, e) for k, e in state.items() if e.get('owned')]
    status['ownedVms'] = len(owned)
    status['ownedMb'] = sum(max(0, (e['pinned'] or 0) - (e['orig'] or 0)) for _, e in owned)
    return status


def main():
    cfg = load_cfg()
    if not cfg['ENABLED']:
        return 0
    node = socket.gethostname()
    if '-AX102' not in node and not cfg['FORCE_SCOPE']:
        return 0
    now = int(time.time())
    try:
        state = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        state = {}
    vms = collect(node, now)
    dry_path = STATE_DIR / 'memguard-dry-log.json'
    try:
        dry_seen = {k: v for k, v in json.loads(dry_path.read_text()).items() if now - v < 3600}
    except (FileNotFoundError, ValueError):
        dry_seen = {}
    status = run(cfg, node, now, vms, state, mem_available_mb(), pending_boosts_mb(now),
                 dry_seen=dry_seen)
    if cfg['DRY_RUN']:
        atomic_json(dry_path, dry_seen)
    atomic_json(STATE_PATH, state)
    atomic_json(STATUS_PATH, status)
    os.chmod(STATUS_PATH, 0o644)
    return 0


if __name__ == '__main__':
    sys.exit(main())
