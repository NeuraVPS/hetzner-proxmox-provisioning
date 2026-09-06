#!/usr/bin/env python3
"""Serialize demand boosts with the balloon reconciler; never trust old capacity."""
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
    with open('/run/neuravps-ram.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        budget = info['MemTotal'] * (90 if '-AX162' in expected_node else 100) // 100
        if (info['MemAvailable'] < need + reserved + RESERVE_MB
                or floor_total() + maximum - floor > budget):
            return {'status': 'blocked', 'reason': 'capacity'}
        floors_path = STATE_DIR / 'floors.json'
        floors = json.loads(floors_path.read_text()) if floors_path.exists() else {}
        floors.setdefault(str(vmid), floor)
        atomic_json(floors_path, floors)
        subprocess.run(['qm', 'set', str(vmid), '--balloon', str(maximum)],
                       check=True, capture_output=True, timeout=15)
        pending[str(vmid)] = {'mb': need, 'until': now + 120}
        atomic_json(pending_path, pending)
        if boot_token is not None:
            path = STATE_DIR / 'boot-guards.json'
            guards = json.loads(path.read_text()) if path.exists() else {}
            guards[str(vmid)] = {'token':boot_token, 'floor':floor, 'target':maximum,
                                 'uuid':config(vmid).get('smbios1'), 'held':False}
            atomic_json(path, guards)
        qmp(vmid, 'balloon', {'value': maximum * 1048576})
        return {'status': 'boosted', 'before': actual, 'target': maximum}


def boot_control(vmid, expected_node, token=None):
    """Hold on a real RDP return; otherwise restore only this boot's own floor."""
    with open('/run/neuravps-ram.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    with open('/run/neuravps-ram.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
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
