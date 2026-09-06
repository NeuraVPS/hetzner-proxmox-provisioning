#!/usr/bin/env python3
"""Serialize demand boosts with the balloon reconciler; never trust old capacity."""
import fcntl
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


def boost(vmid, expected_node):
    # Also protects floors.json against concurrent bases and reconciler writes.
    with open('/run/neuravps-ram.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        maximum, floor, actual, avail = inspect(vmid, expected_node)
        if actual >= maximum - 1024:
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
        qmp(vmid, 'balloon', {'value': maximum * 1048576})
        return {'status': 'boosted', 'before': actual, 'target': maximum}


def main():
    action, raw_vmid, node = sys.argv[1:]
    vmid = int(raw_vmid)
    if vmid <= 0 or action not in ('query', 'boost'):
        raise ValueError('invalid action or VMID')
    result = inspect(vmid, node) if action == 'query' else boost(vmid, node)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        sys.exit(1)
