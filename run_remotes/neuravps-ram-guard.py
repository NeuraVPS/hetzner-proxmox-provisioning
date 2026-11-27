#!/usr/bin/env python3
"""Serialize demand boosts with the balloon reconciler; never trust old capacity."""
import errno
import fcntl
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

CONF_DIR = Path('/etc/pve/qemu-server')
STATE_DIR = Path('/var/lib/neuravps-balloon')
RESERVE_MB = 12288
RECONCILER_TIMER = Path('/etc/systemd/system/neuravps-balloon-reconciler.timer')
# v11 entitlement band (2026-11-27, approved by Ivan): shared knobs with the
# balloon reconciler, same file, same defaults — see the reconciler header.
RECONCILER_DEFAULTS = Path('/etc/default/neuravps-balloon-reconciler')
ENTITLE_DEFAULTS = {'ENTITLE_ENABLED': 0, 'ENTITLE_AVAIL_MIN_MB': 28672}


LOCK_PATH = '/run/neuravps-ram.lock'
# Bounded wait per action. Callers over SSH have their own timeouts
# (welcome-boost: hold 15 s, boost 30 s; migrate_vm retries state 12x), so
# those waits stay below them. The boot restore runs detached from a
# transient systemd unit, so it can wait out a whole reconciler pass.
LOCK_WAIT_S = {'boot': 60, 'restore': 120, 'hold': 10, 'boost': 25,
               'state': 30}
LOCK_POLL_S = (0.2, 0.5, 1.0, 2.0)  # backoff steps; the last one repeats


def log(message):
    print(f'neuravps-ram-guard: {message}', file=sys.stderr, flush=True)


def acquire(lock, action):
    """Take the shared RAM lock, waiting a bounded time instead of failing.

    The balloon reconciler holds this lock for a whole pass and SKIPS its own
    pass when it is busy (correct for a periodic job). The actions in this
    file are one-shot: 2026-11-15 a boot restore collided with a reconciler
    pass (EAGAIN) and the VM stayed at full balloon until someone re-ran it.
    On timeout we raise (-> JSON error + exit 1) and log; we never hang.
    """
    wait = LOCK_WAIT_S[action]
    deadline = time.monotonic() + wait
    attempt = 0
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if attempt:
                log(f'{action}: got {LOCK_PATH} after {attempt} retries')
            return
        except (BlockingIOError, OSError) as exc:
            if getattr(exc, 'errno', None) not in (errno.EAGAIN, errno.EACCES):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log(f'{action}: gave up on {LOCK_PATH} after {wait}s (lock busy)')
                raise RuntimeError(f'ram lock busy after {wait}s')
            time.sleep(min(LOCK_POLL_S[min(attempt, len(LOCK_POLL_S) - 1)], remaining))
            attempt += 1


def config(vmid):
    fields = {}
    for line in (CONF_DIR / f'{vmid}.conf').read_text().splitlines():
        if line.startswith('['):
            break
        key, sep, value = line.partition(':')
        if sep:
            fields[key] = value.strip()
    return fields


def qmp(vmid, command, arguments=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.connect(f'/var/run/qemu-server/{vmid}.qmp')
        stream = sock.makefile('rb')
        json.loads(stream.readline())
        for ident, execute, args in [('cap', 'qmp_capabilities', None),
                                     ('request', command, arguments)]:
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
        return reply['return']


def meminfo():
    return {line.split(':')[0]: int(line.split()[1]) // 1024
            for line in Path('/proc/meminfo').read_text().splitlines()
            if line.startswith(('MemTotal:', 'MemAvailable:'))}


def entitle_cfg(path=None):
    """ENTITLE_* knobs from the reconciler's /etc/default (shell KEY=VALUE).

    Anything unreadable or malformed keeps the default (disabled): a broken
    file must never widen what a boost may take."""
    cfg = dict(ENTITLE_DEFAULTS)
    try:
        text = Path(path or RECONCILER_DEFAULTS).read_text()
    except OSError:
        return cfg
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ').strip()
        value = value.split('#', 1)[0].strip().strip('"\'')   # 'X=1  # note' like bash
        if key in cfg:
            try:
                cfg[key] = int(value)
            except ValueError:
                return dict(ENTITLE_DEFAULTS)
    return cfg


def entitle_mb(maximum):
    """Canonical SQX heap + 4 GB: -Xmx = RAM - 7 GB -> memory - 3 GB; the
    60 GB grandfathered vps-e runs -Xmx46 -> 50 GB. Mirrors the reconciler."""
    ent = 51200 if maximum >= 61440 else maximum - 3072
    return max(0, min(ent, maximum))


def floor_total():
    total = 0
    for path in CONF_DIR.glob('*.conf'):
        fields = config(int(path.stem))
        maximum = int(fields['memory'])
        floor = int(fields.get('balloon', 0))
        total += min(floor, maximum) if floor > 0 else maximum
    return total


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def inspect(vmid, expected_node):
    if socket.gethostname() != expected_node:
        raise RuntimeError('node identity mismatch')
    fields = config(vmid)
    if fields.get('lock'):
        raise RuntimeError('VM config locked')
    maximum = int(fields['memory'])
    floor = int(fields.get('balloon', 0))
    if floor <= 0:
        raise RuntimeError('balloon disabled')
    actual = int(qmp(vmid, 'query-balloon')['actual']) // 1048576
    return maximum, floor, actual, meminfo()['MemAvailable']


def boost(vmid, expected_node, boot_token=None):
    # Also protects floors.json against concurrent bases and reconciler writes.
    with open(LOCK_PATH, 'a') as lock:
        acquire(lock, 'boost' if boot_token is None else 'boot')
        maximum, floor, actual, avail = inspect(vmid, expected_node)
        if actual >= maximum - 1024 and boot_token is None:
            return {'status': 'full'}
        info = meminfo()
        need = max(0, maximum - actual)
        # Account for grants not yet consumed by QEMU. Re-reading MemAvailable
        # alone could lend the same free pages to several clients in a burst.
        pending_path = STATE_DIR / 'pending-boosts.json'
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
        now = time.time()
        pending = {k: v for k, v in pending.items() if v['until'] > now}
        reserved = sum(v['mb'] for v in pending.values())
        # The 90 % floor budget is the BALLOON RECONCILER's (FLOOR_BUDGET_PCT):
        # a node that runs it must not be pushed past it by boosts, or the
        # reconciler sees NO BUDGET. Keyed on the reconciler being installed,
        # not on the node's name (2026-11-04: an AX102 assigned to SQX runs it
        # too). Measured 04/11: installed on 86/86 SQX nodes, 0/36 MT nodes —
        # identical to the old '-AX162' test for the whole fleet.
        has_reconciler = RECONCILER_TIMER.exists()
        budget = info['MemTotal'] * (90 if has_reconciler else 100) // 100
        target = maximum
        capped = False
        if floor_total() + maximum - floor > budget:
            # v11 entitlement band: over the PAPER budget the boost may still
            # bring the floor up to memory - 3 GB (never to the full plan) when
            # the host really has the RAM. Only where the reconciler runs (SQX):
            # the band is defined by the SQX heap and the reconciler is what
            # walks the floor back down afterwards.
            cfg = entitle_cfg() if has_reconciler else ENTITLE_DEFAULTS
            ent = entitle_mb(maximum)
            if cfg['ENTITLE_ENABLED'] == 1 and ent <= floor:
                # Already at its entitlement: nothing more is allowed over the
                # paper budget. 'full' (not 'blocked') so welcome-boost marks
                # the login as handled instead of retrying every 5 min.
                return {'status': 'full', 'reason': 'entitlement'}
            need_e = max(0, ent - actual)
            avail_e = info['MemAvailable'] - reserved
            if not (cfg['ENTITLE_ENABLED'] == 1 and ent > floor
                    and avail_e >= cfg['ENTITLE_AVAIL_MIN_MB']
                    and avail_e - need_e >= RESERVE_MB
                    # absolute cap: the guaranteed minimums never exceed
                    # physical RAM minus the reserve (same as the reconciler)
                    and floor_total() + ent - floor <= info['MemTotal'] - RESERVE_MB):
                return {'status': 'blocked', 'reason': 'capacity'}
            target, need, capped = ent, need_e, True
        elif info['MemAvailable'] < need + reserved + RESERVE_MB:
            return {'status': 'blocked', 'reason': 'capacity'}
        floors_path = STATE_DIR / 'floors.json'
        floors = json.loads(floors_path.read_text()) if floors_path.exists() else {}
        floors.setdefault(str(vmid), floor)
        atomic_json(floors_path, floors)
        subprocess.run(['qm', 'set', str(vmid), '--balloon', str(target)],
                       check=True, capture_output=True, timeout=15)
        pending[str(vmid)] = {'mb': need, 'until': now + 120}
        atomic_json(pending_path, pending)
        if boot_token is not None:
            path = STATE_DIR / 'boot-guards.json'
            guards = json.loads(path.read_text()) if path.exists() else {}
            guards[str(vmid)] = {'token':boot_token, 'floor':floor, 'target':target,
                                 'uuid':config(vmid).get('smbios1'), 'held':False}
            atomic_json(path, guards)
        # Always push the live target (qm set alone never moves it — and at
        # boot `qm start` has seeded it at the old floor while `actual` still
        # reads the full RAM), but never BELOW what the guest already has: a
        # band target can sit under it (ent < actual < memory).
        qmp(vmid, 'balloon', {'value': max(target, actual) * 1048576})
        result = {'status': 'boosted', 'before': actual, 'target': target}
        if capped:
            result['capped'] = 'entitlement'
        return result


def boot_control(vmid, expected_node, token=None):
    """Hold on a real RDP return; otherwise restore only this boot's own floor."""
    with open(LOCK_PATH, 'a') as lock:
        acquire(lock, 'hold' if token is None else 'restore')
        if socket.gethostname() != expected_node:
            raise RuntimeError('node identity mismatch')
        path = STATE_DIR / 'boot-guards.json'
        guards = json.loads(path.read_text()) if path.exists() else {}
        entry = guards.get(str(vmid))
        if not entry:
            return {'status':'no-boot-guard'}
        fields = config(vmid)
        if fields.get('lock') or fields.get('smbios1') != entry.get('uuid'):
            raise RuntimeError('VM changed or locked')
        if token is None:
            entry['held'] = True
        elif entry['token'] == token:
            if not entry['held'] and int(fields.get('balloon',0)) == entry['target']:
                subprocess.run(['qm','set',str(vmid),'--balloon',str(entry['floor'])],
                               check=True,capture_output=True,timeout=15)
            del guards[str(vmid)]
        atomic_json(path,guards)
        return {'status':'boot-guard-updated'}


def transfer_state(vmid, expected_node, encoded=None):
    """Carry working-set history across migration, tied to the VM's UUID."""
    with open(LOCK_PATH, 'a') as lock:
        acquire(lock, 'state')
        if socket.gethostname() != expected_node:
            raise RuntimeError('node identity mismatch')
        uuid = config(vmid).get('smbios1')
        if not uuid:
            raise RuntimeError('no VM UUID for state transfer')
        floors_path = STATE_DIR / 'floors.json'
        samples_path = STATE_DIR / 'samples.tsv'
        floors = json.loads(floors_path.read_text()) if floors_path.exists() else {}
        rows = samples_path.read_text().splitlines() if samples_path.exists() else []
        sample = next((r.split('\t') for r in rows if r.split('\t')[0] == str(vmid)),None)
        if encoded is None:
            return base64.b64encode(json.dumps({'uuid':uuid,'floor':floors.get(str(vmid)),
                                               'sample':sample}).encode()).decode()
        state = json.loads(base64.b64decode(encoded,validate=True))
        if state['uuid'] != uuid:
            raise RuntimeError('VM UUID changed during migration')
        STATE_DIR.mkdir(parents=True,exist_ok=True)
        if state.get('floor') is not None:
            floors[str(vmid)] = max(int(state['floor']),int(floors.get(str(vmid),0)))
            atomic_json(floors_path,floors)
        source = state.get('sample') or [str(vmid),0,0,0,0,0,0,0]
        source = list(source) + [0] * (8-len(source))
        prior = list(sample or []) + [0] * (8-len(sample or []))
        # New rate baseline and no immediate decay at the new host. Preserve
        # bounce/backoff learning; old blocked counters belong to the old host.
        restored = [vmid,0,0,0,0,source[5],max(int(source[6]),int(prior[6])),
                    max(int(source[7]),int(prior[7]),int(time.time())+86400)]
        rows = [r for r in rows if r.split('\t')[0] != str(vmid)]
        rows.append('\t'.join(map(str,restored)))
        tmp = samples_path.with_name(f'.samples.{os.getpid()}.tmp')
        tmp.write_text('\n'.join(rows)+'\n');os.replace(tmp,samples_path)
        return {'status':'state-imported'}


def main():
    action, raw_vmid, node, *extra = sys.argv[1:]
    vmid = int(raw_vmid)
    if vmid <= 0 or action not in ('query', 'boost', 'boot', 'restore', 'hold', 'export-state', 'import-state'):
        raise ValueError('invalid action or VMID')
    if action == 'export-state':
        print(transfer_state(vmid,node))
        return
    if action == 'import-state':
        result = transfer_state(vmid,node,extra[0])
    elif action == 'query':
        result = inspect(vmid,node)
    elif action in ('hold','restore'):
        result = boot_control(vmid,node,extra[0] if action == 'restore' else None)
    else:
        result = boost(vmid,node,extra[0] if action == 'boot' else None)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        sys.exit(1)
