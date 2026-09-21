"""Direct incoming IPv6 is enforced on BASE, independently of PVE VM rules.

The public address is the BASE /64 plus VMID. Only explicit ipv6Enabled=true
opens it. The service switches still restrict SSH/RDP/SMB; outbound IPv6 and
VIP/port-based access are separate. No guest or node firewall is changed.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

TABLE = 'neura_ipv6'
IDENT = ipaddress.IPv6Network('2a01:4f9:c01f:e::/64')
SETS = ('public6', 'enabled6', 'no_rdp6', 'no_smb6', 'no_ssh6')
DISABLED_POLICY = '# Direct IPv6 BASE policy is disabled.\n'


def plan(desired: dict, main_ipv6: str) -> dict:
    main = ipaddress.IPv6Address(main_ipv6)
    public = ipaddress.IPv6Network(f'{main}/64', strict=False)
    if public == IDENT or main.is_unspecified or main.is_link_local:
        raise ValueError('BASE public IPv6 must be a routed public prefix, distinct from IDENT')
    entries = []
    seen = set()
    for raw, entry in sorted(desired.items(), key=lambda pair: int(pair[0])):
        vmid = int(raw)
        if not 100 <= vmid <= 9999:
            raise ValueError(f'Unsafe public IPv6 VMID: {vmid}')
        target = ipaddress.IPv6Address(entry['ipv6'])
        if target not in IDENT or int(target) - int(IDENT.network_address) != vmid:
            raise ValueError(f'VM {vmid}: identity address does not match VMID')
        addr = ipaddress.IPv6Address(int(public.network_address) + vmid)
        if addr == main or str(addr) in seen:
            raise ValueError('Public IPv6 address collision')
        seen.add(str(addr))
        entries.append({'vmid': vmid, 'public': str(addr), 'target': str(target),
                        'enabled': entry.get('ipv6Enabled') is True,
                        'rdp': entry.get('rdp', True) is True,
                        'smb': entry.get('samba', True) is True,
                        'ssh': entry.get('ssh', True) is True})
    return {'schemaVersion': 1, 'main': str(main), 'entries': entries}


def render(policy: dict, uplink: str = 'enp2s0') -> str:
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', uplink):
        raise ValueError('Invalid uplink interface')
    entries = policy['entries']
    def elements(values):
        values = list(values)
        return f'elements = {{ {", ".join(values)} }};' if values else ''
    out = [f'table inet {TABLE} {{',
           ' map public_to_ident { type ipv6_addr : ipv6_addr; ' + elements(f'{e["public"]} : {e["target"]}' for e in entries) + ' }',
           ' set public6 { type ipv6_addr; ' + elements(e['public'] for e in entries) + ' }',
           ' set enabled6 { type ipv6_addr; ' + elements(e['target'] for e in entries if e['enabled']) + ' }',
           ' set direct_rate6 { type ipv6_addr . ipv6_addr; flags dynamic; timeout 10m; size 65536; }',
           ' counter direct_rate_drops {}']
    for service, name in [('rdp', 'no_rdp6'), ('smb', 'no_smb6'), ('ssh', 'no_ssh6')]:
        out.append(f' set {name} {{ type ipv6_addr; ' + elements(e['target'] for e in entries if not e[service]) + ' }')
    out += [
        ' chain direct_pre {',
        '  type filter hook prerouting priority -150; policy accept;',
        f'  iifname "{uplink}" ip6 daddr @public6 tcp flags & (syn | ack) == syn add @direct_rate6 {{ ip6 saddr . ip6 daddr limit rate over 12/minute burst 6 packets }} counter name direct_rate_drops drop',
        ' }',
        ' chain direct_in {',
        '  type nat hook prerouting priority -110; policy accept;',
        f'  iifname "{uplink}" ip6 daddr @public6 dnat ip6 to ip6 daddr map @public_to_ident',
        ' }',
        ' chain guard {',
        '  type filter hook forward priority -10; policy accept;',
        f'  iifname "{uplink}" ct original ip6 daddr @public6 jump direct_guard',
        ' }',
        ' chain direct_guard {',
        '  ip6 daddr != @enabled6 counter drop',
        '  ip6 daddr @no_rdp6 meta l4proto { tcp, udp } th dport 3389 counter drop',
        '  ip6 daddr @no_smb6 tcp dport { 135, 139, 445 } counter drop',
        '  ip6 daddr @no_smb6 udp dport { 137, 138 } counter drop',
        '  ip6 daddr @no_ssh6 tcp dport 22 counter drop',
        ' }', '}', '']
    return '\n'.join(out)


def render_element_update(policy: dict) -> str:
    """Preserve anti-abuse buckets/counters during unrelated panel changes."""
    entries = policy['entries']
    data = [
        ('map', 'public_to_ident', [f'{e["public"]} : {e["target"]}' for e in entries]),
        ('set', 'public6', [e['public'] for e in entries]),
        ('set', 'enabled6', [e['target'] for e in entries if e['enabled']]),
        ('set', 'no_rdp6', [e['target'] for e in entries if not e['rdp']]),
        ('set', 'no_smb6', [e['target'] for e in entries if not e['smb']]),
        ('set', 'no_ssh6', [e['target'] for e in entries if not e['ssh']]),
    ]
    lines = []
    for kind, name, values in data:
        lines.append(f'flush {kind} inet {TABLE} {name}')
        if values:
            lines.append(f'add element inet {TABLE} {name} {{ {", ".join(values)} }}')
    return '\n'.join(lines) + '\n'


def revoked(before: dict, after: dict) -> list[list[str]]:
    """Only original destinations of public incoming flows; never guest egress.

    Removing a conntrack entry also invalidates its flowtable offload. Restrict
    service revocations to that protocol/port, keeping all unrelated sessions.
    """
    current = {e['public']: e for e in after['entries']}
    commands = []
    for old in before.get('entries', []):
        if not old.get('enabled'):
            continue
        new = current.get(old['public'])
        base = ['conntrack', '-D', '-f', 'ipv6', '--orig-dst', old['public']]
        if not new or not new['enabled'] or old['target'] != new['target']:
            commands.append(base)
            continue
        ports = {'rdp': [('tcp', 3389), ('udp', 3389)],
                 'smb': [('tcp', 135), ('tcp', 139), ('tcp', 445), ('udp', 137), ('udp', 138)],
                 'ssh': [('tcp', 22)]}
        for service, pairs in ports.items():
            if old.get(service) and not new[service]:
                commands.extend(base + ['-p', proto, '--dport', str(port)] for proto, port in pairs)
    return commands


def run(args, *, data=None, allowed=(0,)):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=30)
    if result.returncode not in allowed:
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()[:500]}')
    return result


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_receipt(state: Path) -> dict | None:
    """Read a receipt strictly: disable must know every flow it removes."""
    if not state.exists():
        return None
    receipt = json.loads(state.read_text())
    if not isinstance(receipt, dict) or not isinstance(receipt.get('entries'), list):
        raise ValueError('Invalid IPv6 policy receipt')
    return receipt


def revoke(commands: list[list[str]]):
    """Delete selected original inbound conntracks, including their offload.

    The kernel tears down the associated flow entries; no shared flowtable
    reset or pre-conntrack packet filter is needed. Guest-initiated traffic
    has another ORIGINAL destination and is deliberately never selected.
    """
    for command in commands:
        run(command, allowed=(0, 1))  # 1 = no matching connection


def render_disabled_guard(before: dict, uplink: str) -> str:
    """Retain a narrow live guard until direct conntracks have disappeared."""
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', uplink):
        raise ValueError('Invalid uplink interface')
    public = [entry['public'] for entry in before['entries'] if entry.get('enabled')]
    elements = f'elements = {{ {", ".join(public)} }};' if public else ''
    return '\n'.join([
        f'table inet {TABLE} {{',
        ' set public6 { type ipv6_addr; ' + elements + ' }',
        ' chain guard {',
        '  type filter hook forward priority -10; policy accept;',
        f'  iifname "{uplink}" ct original ip6 daddr @public6 counter drop',
        ' }', '}', ''])


def disable_policy(path: Path, state: Path):
    """Fail closed while withdrawing direct IPv6 policy.

    The inert boot file is published before runtime state is removed, so a
    reboot cannot restore an older map. A live table without its receipt is
    retained: deleting it could leave offloaded direct sessions unrevoked.
    """
    before = load_receipt(state)
    present = run(['nft', 'list', 'table', 'inet', TABLE], allowed=(0, 1)).returncode == 0
    if present and before is None:
        raise RuntimeError('IPv6 policy receipt is missing; refusing unsafe disable')
    before = before or {'entries': []}
    atomic_write(path, DISABLED_POLICY)
    # Publish a live guard before resetting the flow cache. It is restricted
    # by conntrack's original public destination, so NAT66 reply traffic from
    # guest-initiated sessions remains untouched.
    transaction = (f'delete table inet {TABLE}\n' if present else '') + render_disabled_guard(
        before, os.environ.get('BASE_POLICY_UPLINK', 'enp2s0'))
    run(['nft', '-c', '-f', '-'], data=transaction)
    run(['nft', '-f', '-'], data=transaction)
    revoke(revoked(before, {'entries': []}))
    state.unlink(missing_ok=True)


def reconcile(desired: dict):
    """Called under sync-base-nat's lock; errors propagate to the firewall API."""
    if os.environ.get('BASE_IPV6_POLICY_ENABLED', '').lower() not in ('1', 'true'):
        return
    policy = plan(desired, os.environ['MAIN_IPV6'])
    policy['uplink'] = os.environ.get('BASE_POLICY_UPLINK', 'enp2s0')
    path = Path(os.environ.get('BASE_IPV6_POLICY_FILE', '/etc/nftables.d/base-ipv6-policy.nft'))
    state = Path(os.environ.get('BASE_IPV6_POLICY_STATE', '/var/lib/base-nat/ipv6-policy.json'))
    # A corrupt previous receipt must not bypass the targeted revocation step.
    before = load_receipt(state) or {'entries': []}
    rendered = render(policy, os.environ.get('BASE_POLICY_UPLINK', 'enp2s0'))
    old_text = path.read_text() if path.exists() else None
    present = run(['nft', 'list', 'table', 'inet', TABLE], allowed=(0, 1)).returncode == 0
    same_structure = (present and before.get('schemaVersion') == policy['schemaVersion']
                      and before.get('uplink') == policy['uplink'])
    if same_structure:
        # A failed disable may leave its deny-only table with the previous
        # receipt. Reconstruct the full table rather than updating absent maps.
        same_structure = run(['nft', 'list', 'map', 'inet', TABLE, 'public_to_ident'],
                             allowed=(0, 1)).returncode == 0
    transaction = (
        render_element_update(policy) if same_structure else
        (f'delete table inet {TABLE}\n' if present else '') + rendered)
    run(['nft', '-c', '-f', '-'], data=transaction)
    # Save boot policy before switching live rules; restore it if the atomic
    # kernel update is rejected. This never reloads the global ruleset.
    atomic_write(path, rendered)
    try:
        run(['nft', '-f', '-'], data=transaction)
    except Exception:
        if old_text is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, old_text)
        raise
    revoke(revoked(before, policy))
    atomic_write(state, json.dumps(policy, sort_keys=True) + '\n')
