#!/usr/bin/env python3
"""Stabilize the vmbr0 MAC and persist it without reloading networking.

Dry-run by default. Run --apply on a Proxmox node after reviewing the output.
Active bridges keep their current MAC; empty zero-address bridges get a valid one.
A port-less bridge otherwise inherits a TAP MAC and changes when that VM leaves,
invalidating the gateway neighbor entries of other guests on the same node.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import socket
import subprocess
import tempfile


def valid_mac(mac):
    return bool(re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac)
                and mac != "00:00:00:00:00:00" and not int(mac[:2], 16) & 1)


def select_mac(current, ports, identity):
    if valid_mac(current):
        return current
    if current != '00:00:00:00:00:00' or ports:
        raise ValueError('invalid live MAC on a bridge with ports; inspect manually')
    # Linux can clear an automatic bridge MAC when its last port leaves.
    # No attached guest exists in this case; initialize a stable local address.
    digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
    return '02:' + ':'.join(digest[i:i + 2] for i in range(0, 10, 2))


def configured_text(text, mac):
    """Add one attribute; preserve every unrelated line and reject conflicting intent."""
    if not valid_mac(mac):
        raise ValueError("invalid unicast MAC")
    headers = list(re.finditer(
        r"(?m)^iface[ \t]+(\S+)[ \t]+(\S+)[ \t]+[^\n]+\n", text))
    inet = [h for h in headers if h[1] == 'vmbr0' and h[2] == 'inet']
    if len(inet) != 1:
        raise ValueError("expected one vmbr0 inet stanza in /etc/network/interfaces")
    explicit = []
    for i, header in enumerate(headers):
        if header[1] != 'vmbr0':
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[header.end():end]
        for line in block.splitlines():
            words = line.split('#', 1)[0].split()
            if words and words[0] == 'hwaddress':
                value = words[1:]
                if value[:1] == ['ether']:
                    value = value[1:]
                if len(value) != 1 or value[0].lower() != mac:
                    raise ValueError("existing hwaddress differs from the live MAC")
                explicit.append(value[0])
    if explicit:
        return text
    at = inet[0].end()
    return text[:at] + f"    hwaddress {mac}\n" + text[at:]


def addresses():
    link = json.loads(subprocess.check_output(
        ['ip', '-j', 'address', 'show', 'dev', 'vmbr0']))[0]
    return {'flags': sorted(link['flags']), 'addresses': sorted(
        (a['family'], a['local'], a['prefixlen'], a.get('scope', ''))
        for a in link['addr_info'])}


def protected_addresses(state):
    # Initializing an empty, zero-MAC bridge may create its automatic IPv6
    # link-local address. Preserve all configured gateways, including fe80::1.
    return {'flags': state['flags'], 'addresses': [a for a in state['addresses']
            if not (a[0] == 'inet6' and a[3] == 'link' and a[1] != 'fe80::1')]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    net = Path('/sys/class/net/vmbr0')
    config = Path('/etc/network/interfaces')
    if not (net / 'bridge').is_dir() or config.is_symlink():
        raise RuntimeError("require a bridge and a regular, non-symlink interfaces file")
    old = config.read_text()
    old_stat = config.stat()
    current = (net / 'address').read_text().strip().lower()
    ports = [p.name for p in (net / 'brif').iterdir()]
    identity = Path('/etc/machine-id').read_text().strip() + ':' + socket.gethostname()
    mac = select_mac(current, ports, identity)
    initialize_empty = mac != current
    new = configured_text(old, mac)
    # Include files may also define hwaddress. Respect them, never overwrite
    # a value that differs from the currently active address.
    effective = json.loads(subprocess.check_output(['ifquery', '--format=json', 'vmbr0']))
    for iface in effective:
        value = iface.get('config', {}).get('hwaddress')
        for address in value if isinstance(value, list) else ([value] if value else []):
            if address.lower().removeprefix('ether ') != mac:
                raise RuntimeError('effective hwaddress conflicts with the live MAC')
    before = addresses()
    if 'UP' not in before['flags']:
        raise RuntimeError('vmbr0 is not administratively up')
    result = {'mac': mac, 'macBefore': current, 'emptyBridgeInitialization': initialize_empty,
              'assignmentBefore': (net / 'addr_assign_type').read_text().strip(),
              'configChange': old != new, 'apply': args.apply}
    if args.apply:
        if (net / 'address').read_text().strip().lower() != current:
            raise RuntimeError('bridge MAC changed during inspection; retry with current state')
        if initialize_empty and any((net / 'brif').iterdir()):
            raise RuntimeError('a port appeared on the empty bridge; retry with current state')
        # Active bridges retain the same bytes, now explicitly assigned. No down/up,
        # ifreload, address flush, service restart, or guest operation.
        subprocess.run(['ip', 'link', 'set', 'dev', 'vmbr0', 'address', mac], check=True)
        after = addresses()
        same = protected_addresses(after) == protected_addresses(before) if initialize_empty else after == before
        if (net / 'address').read_text().strip().lower() != mac or not same:
            raise RuntimeError('network state changed during pin; inspect before continuing')
        if (net / 'addr_assign_type').read_text().strip() != '3':
            raise RuntimeError('kernel did not pin the MAC')
        if old != new:
            if config.read_text() != old:
                raise RuntimeError('interfaces changed concurrently; runtime pin retained')
            backup = config.with_name(config.name + '.before-neuravps-bridge-mac')
            try:
                with backup.open('x') as f:
                    os.fchmod(f.fileno(), 0o600)
                    f.write(old)
            except FileExistsError:
                pass
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=config.parent,
                                                 prefix='.bridge-mac-', delete=False) as f:
                    tmp = Path(f.name)
                    os.fchmod(f.fileno(), stat.S_IMODE(old_stat.st_mode))
                    os.fchown(f.fileno(), old_stat.st_uid, old_stat.st_gid)
                    f.write(new); f.flush(); os.fsync(f.fileno())
                if config.read_text() != old:
                    raise RuntimeError('interfaces changed concurrently; runtime pin retained')
                os.replace(tmp, config); tmp = None
            finally:
                if tmp is not None:
                    tmp.unlink()
        parsed = json.loads(subprocess.check_output(['ifquery', '--format=json', 'vmbr0']))
        persisted = []
        for iface in parsed:
            value = iface.get('config', {}).get('hwaddress')
            persisted.extend(value if isinstance(value, list) else ([value] if value else []))
        assert persisted and all(v.lower().removeprefix('ether ') == mac for v in persisted)
        result['assignmentAfter'] = (net / 'addr_assign_type').read_text().strip()
        result['addressesAndFlagsUnchanged'] = addresses() == before
        result['existingGatewayAddressesUnchanged'] = protected_addresses(addresses()) == protected_addresses(before)
        assert result['existingGatewayAddressesUnchanged']
        if not initialize_empty:
            assert result['addressesAndFlagsUnchanged']
    print(json.dumps(result))


if __name__ == '__main__':
    main()
