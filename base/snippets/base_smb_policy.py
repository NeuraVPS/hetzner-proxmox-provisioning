#!/usr/bin/env python3
"""Plan the BASE guest-to-guest SMB policy from a complete Firestore snapshot.

The pure ``build_policy`` entry point accepts complete ``servers``, ``users``
and ``config/smbPolicy`` snapshots.  The explicit full-sync runtime at the
bottom of this file reads those snapshots and applies nft; keeping that I/O
boundary separate prevents a partial read from becoming an empty allow-list.

There are only three sources of permission:

* servers with the same current ``userId``;
* a mutual, direct ``linkedAccountIds`` edge (never a graph traversal); and
* a reviewed ``partnerPairs`` entry that pins both server document IDs and
  their current owners.

The output contains directed IPv4 and IPv6 address pairs.  It includes both
directions because BASE routing can be asymmetric: conntrack state is not a
safe way to recognise the SMB reply path.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address, ip_network
import argparse
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import re
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 2
DEFAULT_MODE = "audit"
MODES = frozenset(("audit", "enforce"))
TRANSIT_V6 = ip_network("2a01:4f9:c01f:e:ffff::/112")
IDENT_V6 = ip_network("2a01:4f9:c01f:e::/64")
VMID_MIN = 100
VMID_MAX = 9999
# These are deliberately ranges rather than the addresses returned by the
# inventory read.  A deleted document, a document being provisioned, and a
# reused VMID must still be a candidate for enforcement.  Only an explicitly
# built address pair receives an allow-list entry.
GUEST_V4_RANGE = "10.64.0.100-10.64.39.15"
GUEST_V6_RANGE = "2a01:4f9:c01f:e::64-2a01:4f9:c01f:e::270f"
SMB_PORT = 445
SERVER_PROJECTION = ("userId", "proxmoxId", "ipv4", "ipv6")
USER_PROJECTION = ("linkedAccountIds",)


class PolicyError(ValueError):
    """A policy must not be changed when a fresh input cannot be trusted."""


class InputUnavailable(PolicyError):
    """The caller could not obtain a complete current snapshot."""


class InventoryValidationError(PolicyError):
    """The supplied snapshot is internally inconsistent or incomplete."""


class ConfigValidationError(PolicyError):
    """``config/smbPolicy`` is not a version supported by this planner."""


@dataclass(frozen=True, order=True)
class Server:
    server_id: str
    owner_uid: str
    vmid: int
    ipv4: str | None
    ipv6: str | None


@dataclass(frozen=True)
class SmbPolicyPlan:
    schema_version: int
    config_version: int
    mode: str
    guest_v4: tuple[str, ...]
    guest_v6: tuple[str, ...]
    allowed_v4: tuple[tuple[str, str], ...]
    allowed_v6: tuple[tuple[str, str], ...]
    allowed_server_pairs: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        """Stable, non-PII serialisation suitable for a last-good file."""
        return {
            "schemaVersion": self.schema_version,
            "runtimeSchemaVersion": RUNTIME_SCHEMA_VERSION,
            "configVersion": self.config_version,
            "mode": self.mode,
            "guestV4": list(self.guest_v4),
            "guestV6": list(self.guest_v6),
            "allowedV4": [list(pair) for pair in self.allowed_v4],
            "allowedV6": [list(pair) for pair in self.allowed_v6],
            "allowedServerPairs": [list(pair) for pair in self.allowed_server_pairs],
            "warnings": list(self.warnings),
        }


def default_config(version: int = 1) -> dict[str, Any]:
    """Return an explicit initial audit configuration for ``config/smbPolicy``."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "mode": DEFAULT_MODE,
        "partnerPairs": [],
    }


def _nonempty_string(value: Any, field: str, error: type[PolicyError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} must be a non-empty string")
    return value.strip()


def _collection_by_id(
    source: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    label: str,
) -> dict[str, dict[str, Any]]:
    if source is None:
        raise InputUnavailable(f"{label} snapshot is unavailable")
    if isinstance(source, Mapping):
        items = source.items()
    elif isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        items = (
            (entry.get("id") or entry.get("serverId") or entry.get("uid"), entry)
            for entry in source
        )
    else:
        raise InputUnavailable(f"{label} snapshot has an unsupported type")

    out: dict[str, dict[str, Any]] = {}
    for identifier, value in items:
        item_id = _nonempty_string(identifier, f"{label} id", InventoryValidationError)
        if not isinstance(value, Mapping):
            raise InventoryValidationError(f"{label}/{item_id} is not an object")
        if item_id in out:
            raise InventoryValidationError(f"duplicate {label} id {item_id}")
        out[item_id] = dict(value)
    return out


def _normalise_guest_ip(value: Any, family: int, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = ip_address(str(value).strip())
    except ValueError as exc:
        raise InventoryValidationError(f"{field} is not an IP address") from exc
    if parsed.version != family:
        raise InventoryValidationError(f"{field} has the wrong address family")
    if parsed.is_unspecified or parsed.is_multicast or parsed.is_loopback or parsed.is_link_local:
        raise InventoryValidationError(f"{field} is not a routable guest address")
    if isinstance(parsed, IPv6Address) and parsed in TRANSIT_V6:
        raise InventoryValidationError(f"{field} is BASE tunnel transit, not a guest")
    return str(parsed)


def _canonical_guest_addresses(vmid: int) -> tuple[str, str]:
    """Return the only routable guest addresses assigned to ``vmid``."""
    return f"10.64.{vmid // 256}.{vmid % 256}", str(IPv6Address(int(IDENT_V6.network_address) + vmid))


def _servers(
    raw_servers: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    raw_users: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> tuple[tuple[Server, ...], dict[str, frozenset[str]], tuple[str, ...]]:
    users = _collection_by_id(raw_users, "users")
    raw = _collection_by_id(raw_servers, "servers")
    links: dict[str, frozenset[str]] = {}
    invalid_users: set[str] = set()
    warnings: list[str] = []
    for uid, user in users.items():
        if user.get("_smb_incomplete"):
            invalid_users.add(uid)
            warnings.append(f"users/{uid} skipped: document is incomplete")
            continue
        linked = user.get("linkedAccountIds", [])
        if linked is None:
            linked = []
        if not isinstance(linked, list) or not all(isinstance(x, str) for x in linked):
            invalid_users.add(uid)
            warnings.append(f"users/{uid} skipped: linkedAccountIds is incomplete")
            continue
        links[uid] = frozenset(x for x in linked if x and x != uid)

    candidates: list[Server] = []
    result: list[Server] = []
    for server_id, item in raw.items():
        try:
            if item.get("_smb_incomplete"):
                raise InventoryValidationError("document is incomplete")
            owner = _nonempty_string(item.get("userId"), f"servers/{server_id}.userId", InventoryValidationError)
            if owner not in users or owner in invalid_users:
                raise InventoryValidationError("owner is absent or incomplete")
            vmid = int(item.get("proxmoxId"))
            if vmid < VMID_MIN or vmid > VMID_MAX:
                raise InventoryValidationError(f"VMID is outside {VMID_MIN}..{VMID_MAX}")
            v4 = _normalise_guest_ip(item.get("ipv4"), 4, f"servers/{server_id}.ipv4")
            v6 = _normalise_guest_ip(item.get("ipv6"), 6, f"servers/{server_id}.ipv6")
            expected_v4, expected_v6 = _canonical_guest_addresses(vmid)
            if (v4, v6) != (expected_v4, expected_v6):
                raise InventoryValidationError("guest addresses do not match the canonical VMID allocation")
        except (InventoryValidationError, TypeError, ValueError) as exc:
            # A document can temporarily lack fields during provisioning, or
            # be the stale half of a move/reuse.  Omitting it removes any old
            # permit; the static candidate ranges still cover its packets.
            warnings.append(f"servers/{server_id} skipped: {exc}")
            continue
        candidates.append(Server(server_id, owner, vmid, v4, v6))

    by_vmid: dict[int, list[Server]] = {}
    for candidate in candidates:
        by_vmid.setdefault(candidate.vmid, []).append(candidate)
    for vmid, same_vmid in by_vmid.items():
        if len(same_vmid) != 1:
            warnings.append(f"VMID {vmid} skipped: duplicate server documents")
            continue
        result.extend(same_vmid)
    return tuple(sorted(result)), links, tuple(warnings)


def _partner_pairs(
    config: Mapping[str, Any], servers: Iterable[Server]
) -> tuple[frozenset[frozenset[str]], tuple[str, ...]]:
    pairs = config.get("partnerPairs", [])
    if pairs is None:
        pairs = []
    if not isinstance(pairs, list):
        raise ConfigValidationError("partnerPairs must be a list")
    by_id = {server.server_id: server for server in servers}
    out: set[frozenset[str]] = set()
    warnings: list[str] = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, Mapping):
            raise ConfigValidationError(f"partnerPairs[{index}] must be an object")
        left_id = _nonempty_string(pair.get("serverIdA"), f"partnerPairs[{index}].serverIdA", ConfigValidationError)
        left_owner = _nonempty_string(pair.get("ownerUidA"), f"partnerPairs[{index}].ownerUidA", ConfigValidationError)
        right_id = _nonempty_string(pair.get("serverIdB"), f"partnerPairs[{index}].serverIdB", ConfigValidationError)
        right_owner = _nonempty_string(pair.get("ownerUidB"), f"partnerPairs[{index}].ownerUidB", ConfigValidationError)
        if left_id == right_id:
            raise ConfigValidationError(f"partnerPairs[{index}] cannot name one server twice")
        if by_id.get(left_id) is None or by_id[left_id].owner_uid != left_owner:
            warnings.append(f"partnerPairs[{index}] skipped: left server/owner binding is stale")
            continue
        if by_id.get(right_id) is None or by_id[right_id].owner_uid != right_owner:
            warnings.append(f"partnerPairs[{index}] skipped: right server/owner binding is stale")
            continue
        canonical = frozenset((left_id, right_id))
        if canonical in out:
            raise ConfigValidationError(f"partnerPairs[{index}] duplicates a reviewed pair")
        out.add(canonical)
    return frozenset(out), tuple(warnings)


def validate_config(
    config: Mapping[str, Any] | None, servers: Iterable[Server]
) -> tuple[int, str, frozenset[frozenset[str]], tuple[str, ...]]:
    if config is None:
        raise InputUnavailable("config/smbPolicy is unavailable")
    if not isinstance(config, Mapping):
        raise ConfigValidationError("config/smbPolicy must be an object")
    if config.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigValidationError(f"unsupported schemaVersion {config.get('schemaVersion')!r}")
    version = config.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ConfigValidationError("version must be a positive integer")
    mode = config.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise ConfigValidationError(f"mode must be one of {sorted(MODES)}")
    partners, warnings = _partner_pairs(config, servers)
    return version, mode, partners, warnings


def _directly_linked(a: Server, b: Server, links: Mapping[str, frozenset[str]]) -> bool:
    # linked_accounts.py makes this edge symmetric atomically.  Requiring both
    # sides makes a partial/failed write fail closed, and deliberately does not
    # treat A->C->B as permission for A->B after an A<->B unlink.
    return b.owner_uid in links.get(a.owner_uid, frozenset()) and a.owner_uid in links.get(b.owner_uid, frozenset())


def build_policy(
    servers: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    users: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    config: Mapping[str, Any] | None,
) -> SmbPolicyPlan:
    """Return a deterministic, complete policy plan without performing I/O."""
    known, links, inventory_warnings = _servers(servers, users)
    version, mode, partners, partner_warnings = validate_config(config, known)
    v4: set[tuple[str, str]] = set()
    v6: set[tuple[str, str]] = set()
    allowed_servers: set[tuple[str, str]] = set()

    for offset, left in enumerate(known):
        for right in known[offset + 1:]:
            server_pair = frozenset((left.server_id, right.server_id))
            permitted = (
                left.owner_uid == right.owner_uid
                or _directly_linked(left, right, links)
                or server_pair in partners
            )
            if not permitted:
                continue
            allowed_servers.add(tuple(sorted(server_pair)))
            if left.ipv4 and right.ipv4:
                v4.update(((left.ipv4, right.ipv4), (right.ipv4, left.ipv4)))
            if left.ipv6 and right.ipv6:
                v6.update(((left.ipv6, right.ipv6), (right.ipv6, left.ipv6)))

    return SmbPolicyPlan(
        schema_version=SCHEMA_VERSION,
        config_version=version,
        mode=mode,
        guest_v4=(GUEST_V4_RANGE,),
        guest_v6=(GUEST_V6_RANGE,),
        allowed_v4=tuple(sorted(v4)),
        allowed_v6=tuple(sorted(v6)),
        allowed_server_pairs=tuple(sorted(allowed_servers)),
        warnings=inventory_warnings + partner_warnings,
    )


def _set_elements(values: Iterable[str] | Iterable[tuple[str, str]]) -> str:
    rendered = []
    for value in values:
        rendered.append(" . ".join(value) if isinstance(value, tuple) else value)
    return ", ".join(rendered)


def _set_declaration(name: str, set_type: str, values: Iterable[str] | Iterable[tuple[str, str]], interval: bool = False) -> str:
    rendered = _set_elements(values)
    fields = [f"type {set_type}"]
    if interval:
        fields.append("flags interval")
    if rendered:
        fields.append(f"elements = {{ {rendered} }}")
    return f"  set {name} {{ {'; '.join(fields)}; }}"


def _candidate_rules(plan: SmbPolicyPlan, action: str) -> list[str]:
    """Rules for SMB/RPC/NetBIOS requests and replies; never use conntrack NEW."""
    rules: list[str] = []
    for family, guests, allowed in (("ip", "smb_guests_v4", "smb_allowed_v4"), ("ip6", "smb_guests_v6", "smb_allowed_v6")):
        directions = (
            # Cover every request tuple accepted by the legacy BASE chain,
            # including uncommon TCP/UDP combinations; changing protocol or
            # selecting source port 137 must not bypass account permission.
            "meta l4proto { tcp, udp } th dport { 135, 137, 138, 139, 445 }",
            "tcp sport { 135, 139, 445 } tcp flags & (syn | ack) != syn",
            "udp sport 137 udp dport 137",
            "udp sport 138 udp dport 138",
        )
        for direction in directions:
            base = f'iifname "tun-*" oifname "tun-*" {direction} {family} saddr @{guests} {family} daddr @{guests}'
            rules.append(f'{base} {family} saddr . {family} daddr @{allowed} counter comment "smb-policy known pair"')
            suffix = "counter drop" if action == "drop" else "counter"
            rules.append(f'{base} {family} saddr . {family} daddr != @{allowed} {suffix} comment "smb-policy unknown candidate"')
    return rules


def render_nft_bootstrap(plan: SmbPolicyPlan) -> str:
    """Render a self-contained table.  It is safe to validate with ``nft -c``.

    The policy hook is before the legacy forward accept rule. Known traffic is
    only counted, never accepted here, so the existing SMB SYN rate limiter and
    any later BASE policy still run. In audit mode unknown candidates are also
    only counted; in enforce mode they are dropped.
    """
    action = "drop" if plan.mode == "enforce" else "audit"
    lines = [
        "# Generated by base_smb_policy.py; do not hand-edit.",
        "# This table is intentionally independent: no flush ruleset.",
        "table inet nvx_smb_policy {",
        _set_declaration("smb_guests_v4", "ipv4_addr", plan.guest_v4, interval=True),
        _set_declaration("smb_guests_v6", "ipv6_addr", plan.guest_v6, interval=True),
        _set_declaration("smb_allowed_v4", "ipv4_addr . ipv4_addr", plan.allowed_v4),
        _set_declaration("smb_allowed_v6", "ipv6_addr . ipv6_addr", plan.allowed_v6),
        "  chain forward {",
        "    type filter hook forward priority -5; policy accept;",
    ]
    lines.extend(f"    {rule}" for rule in _candidate_rules(plan, action))
    lines.extend(["  }", "}", ""])
    return "\n".join(lines)


def render_nft_set_update(plan: SmbPolicyPlan) -> str:
    """Atomically replace only set elements, preserving rule counters.

    This assumes the bootstrap table already exists.  The integration caller
    must execute this as one ``nft -f`` transaction *after* a successful fresh
    plan; a failed fresh read must not call it.
    """
    lines = ["# Generated set update; no table/ruleset flush."]
    for name, values in (
        ("smb_guests_v4", plan.guest_v4),
        ("smb_guests_v6", plan.guest_v6),
        ("smb_allowed_v4", plan.allowed_v4),
        ("smb_allowed_v6", plan.allowed_v6),
    ):
        lines.append(f"flush set inet nvx_smb_policy {name}")
        if tuple(values):
            lines.append(f"add element inet nvx_smb_policy {name} {{ {_set_elements(values)} }}")
    lines.append("")
    return "\n".join(lines)


def render_nft_chain_update(plan: SmbPolicyPlan) -> str:
    """Replace only this table's hook chain for a reviewed mode change.

    nft parses the delete/recreate plus every new rule as one transaction.  The
    guest and allowed sets stay in the table, and callers combine this output
    with :func:`render_nft_set_update` for one apply.  Routine inventory syncs
    must not call this function, so audit counters survive those updates.
    """
    action = "drop" if plan.mode == "enforce" else "audit"
    lines = [
        "# Reviewed audit/enforce mode transition; no global ruleset flush.",
        "delete chain inet nvx_smb_policy forward",
        "add chain inet nvx_smb_policy forward { type filter hook forward priority -5; policy accept; }",
    ]
    lines.extend(
        f"add rule inet nvx_smb_policy forward {rule}"
        for rule in _candidate_rules(plan, action)
    )
    lines.append("")
    return "\n".join(lines)


def render_nft_update(plan: SmbPolicyPlan, installed_mode: str) -> str:
    """Render one atomic update, replacing the hook chain only on mode change."""
    if installed_mode not in MODES:
        raise PolicyError("installed_mode must be audit or enforce")
    parts = [render_nft_set_update(plan)]
    if installed_mode != plan.mode:
        parts.append(render_nft_chain_update(plan))
    return "\n".join(parts)


def render_nft_rebuild(plan: SmbPolicyPlan) -> str:
    """Replace only our table when the on-disk runtime schema changed."""
    return "\n".join((
        "# SMB runtime schema migration; this is not a global ruleset reload.",
        "delete table inet nvx_smb_policy",
        render_nft_bootstrap(plan),
    ))


def write_last_good(path: str | Path, plan: SmbPolicyPlan) -> None:
    """Atomically persist a plan only after the caller applied its nft update."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(plan.snapshot(), indent=2, sort_keys=True) + "\n")
    os.replace(temporary, target)


def _pairs_from_receipt(value: Any, family: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise PolicyError(f"SMB last-good receipt has invalid {family}")
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(part, str) for part in entry):
            raise PolicyError(f"SMB last-good receipt has invalid {family}")
        pairs.append((entry[0], entry[1]))
    return tuple(sorted(set(pairs)))


def _receipt_plan(path: str | Path) -> SmbPolicyPlan | None:
    receipt = Path(path)
    if not receipt.exists():
        return None
    try:
        data = json.loads(receipt.read_text())
    except Exception as exc:
        raise PolicyError("SMB last-good receipt is unreadable") from exc
    if not isinstance(data, Mapping) or data.get("mode") not in MODES:
        raise PolicyError("SMB last-good receipt has no valid mode")
    try:
        return SmbPolicyPlan(
            schema_version=int(data["schemaVersion"]),
            config_version=int(data["configVersion"]),
            mode=data["mode"],
            guest_v4=tuple(str(value) for value in data.get("guestV4", [])),
            guest_v6=tuple(str(value) for value in data.get("guestV6", [])),
            allowed_v4=_pairs_from_receipt(data.get("allowedV4"), "allowedV4"),
            allowed_v6=_pairs_from_receipt(data.get("allowedV6"), "allowedV6"),
            allowed_server_pairs=tuple(),
            warnings=tuple(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError("SMB last-good receipt is incomplete") from exc


def _receipt_runtime_schema(path: str | Path) -> int | None:
    receipt = Path(path)
    try:
        data = json.loads(receipt.read_text())
    except Exception as exc:
        raise PolicyError("SMB last-good receipt is unreadable") from exc
    return data.get("runtimeSchemaVersion") if isinstance(data, Mapping) else None


def _is_candidate_address(value: str, family: int) -> bool:
    try:
        address = ip_address(value)
    except ValueError:
        return False
    if address.version != family:
        return False
    if family == 4:
        return int(ip_address("10.64.0.100")) <= int(address) <= int(ip_address("10.64.39.15"))
    return int(IPv6Address("2a01:4f9:c01f:e::64")) <= int(address) <= int(IPv6Address("2a01:4f9:c01f:e::270f"))


_CONNTRACK_TUPLE = re.compile(
    r"^(?:(?:ipv4|ipv6)\s+\d+\s+)?(tcp|udp)\s+\d+(?:\s+\d+)?(?:\s+[A-Z_]+)?\s+src=(\S+)\s+dst=(\S+)\s+sport=(\d+)\s+dport=(\d+)\b"
)
_TCP_SMB_PORTS = frozenset((135, 137, 138, 139, 445))
_UDP_SMB_DEST_PORTS = frozenset((135, 137, 138, 139, 445))


def _smb_conntrack_tuples(listing: str, family: int) -> tuple[tuple[str, str, str, int, int], ...]:
    """Parse original conntrack tuples only; reply tuples are never guessed."""
    entries: list[tuple[str, str, str, int, int]] = []
    for line in listing.splitlines():
        match = _CONNTRACK_TUPLE.match(line)
        if not match:
            continue
        proto, source, destination, sport, dport = match.groups()
        try:
            source, destination = str(ip_address(source)), str(ip_address(destination))
        except ValueError:
            continue
        sport_i, dport_i = int(sport), int(dport)
        service = (
            dport_i in (_TCP_SMB_PORTS if proto == "tcp" else _UDP_SMB_DEST_PORTS)
            or (proto == "tcp" and sport_i in _TCP_SMB_PORTS)
            or (proto == "udp" and (sport_i, dport_i) in {(137, 137), (138, 138)})
        )
        if service and _is_candidate_address(source, family) and _is_candidate_address(destination, family):
            entries.append((proto, source, destination, sport_i, dport_i))
    return tuple(entries)


def _conntrack_delete_command(family: int, item: tuple[str, str, str, int, int]) -> list[str]:
    proto, source, destination, sport, dport = item
    return [
        "conntrack", "-D", "-f", "ipv4" if family == 4 else "ipv6",
        "--orig-src", source, "--orig-dst", destination, "-p", proto,
        "--orig-port-src", str(sport), "--orig-port-dst", str(dport),
    ]


def _conntrack_run(command: list[str]) -> None:
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode == 0:
        return
    # Exit 1 is also used for a tuple that ended between list and delete.  Any
    # other exit-1 error means enforcement could leave an offloaded session.
    if result.returncode == 1 and "0 flow entries have been deleted" in (result.stderr or ""):
        return
    raise RuntimeError("SMB conntrack revocation failed: " + (result.stderr or "").strip()[:500])


def revoke_removed_smb_conntracks(
    before: SmbPolicyPlan | None, after: SmbPolicyPlan, *, force: bool = False,
) -> None:
    """Delete only live SMB tuples that lost an account permission.

    The selected conntrack entry owns any flowtable offload, so this tears down
    a persistent session without flushing conntrack or touching the shared
    flowtable.  On an audit->enforce transition every currently unknown
    candidate tuple is selected; while already enforcing we run only when a
    previously allowed pair was removed.
    """
    if after.mode != "enforce":
        return
    removed = {
        4: set(before.allowed_v4) - set(after.allowed_v4) if before else set(),
        6: set(before.allowed_v6) - set(after.allowed_v6) if before else set(),
    }
    if not force and before and before.mode == "enforce" and not removed[4] and not removed[6]:
        return
    for family in (4, 6):
        result = subprocess.run(
            ["conntrack", "-L", "-f", "ipv4" if family == 4 else "ipv6", "-o", "extended"],
            text=True, capture_output=True, check=False, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("SMB conntrack listing failed: " + (result.stderr or "").strip()[:500])
        allowed = set(after.allowed_v4 if family == 4 else after.allowed_v6)
        for item in _smb_conntrack_tuples(result.stdout, family):
            _proto, source, destination, _sport, _dport = item
            if (source, destination) not in allowed:
                _conntrack_run(_conntrack_delete_command(family, item))


def reconcile_fresh(
    servers: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    users: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    config: Mapping[str, Any] | None,
    apply_transaction: Callable[[str], None],
    last_good_path: str | Path,
    installed_mode: str,
) -> SmbPolicyPlan:
    """Build, apply, then save last-good.  Errors leave the prior file intact."""
    plan = build_policy(servers, users, config)
    apply_transaction(render_nft_update(plan, installed_mode))
    write_last_good(last_good_path, plan)
    return plan


def _snapshot_data(snapshot: Any, label: str) -> tuple[str, dict[str, Any]]:
    """Extract one Firestore snapshot without depending on firebase_admin types."""
    if snapshot is None or not getattr(snapshot, "exists", False):
        raise InputUnavailable(f"{label} document is unavailable")
    identifier = _nonempty_string(getattr(snapshot, "id", None), f"{label} id", InputUnavailable)
    data = snapshot.to_dict()
    if not isinstance(data, Mapping):
        raise InputUnavailable(f"{label}/{identifier} is not an object")
    return identifier, dict(data)


def _collection_snapshot_data(snapshot: Any, label: str) -> tuple[str, dict[str, Any]]:
    """Keep a present-but-unusable document as an explicit fail-closed entry."""
    if snapshot is None or not getattr(snapshot, "exists", False):
        raise InputUnavailable(f"{label} document is unavailable")
    identifier = _nonempty_string(getattr(snapshot, "id", None), f"{label} id", InputUnavailable)
    try:
        data = snapshot.to_dict()
    except Exception as exc:
        raise InputUnavailable(f"{label}/{identifier} could not be read") from exc
    if not isinstance(data, Mapping):
        return identifier, {"_smb_incomplete": True}
    return identifier, dict(data)


def firestore_snapshots(
    db: Any, *, missing_config_is_error: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Read only the fields required for a full policy reconciliation.

    A failed stream is deliberately allowed to raise: callers must keep the
    installed policy rather than treating a failed read as an empty fleet.
    Missing ``config/smbPolicy`` is the explicit, safe initial configuration:
    audit with no partner exception. A present but malformed document still
    fails validation.
    """
    if db is None:
        raise InputUnavailable("Firestore client is unavailable")
    try:
        server_snaps = db.collection("servers").select(SERVER_PROJECTION).stream()
        user_snaps = db.collection("users").select(USER_PROJECTION).stream()
        config_snap = db.collection("config").document("smbPolicy").get()
        servers = dict(_collection_snapshot_data(snap, "servers") for snap in server_snaps)
        users = dict(_collection_snapshot_data(snap, "users") for snap in user_snaps)
    except PolicyError:
        raise
    except Exception as exc:
        raise InputUnavailable("Firestore policy snapshot failed") from exc

    if config_snap is None or not getattr(config_snap, "exists", False):
        if missing_config_is_error:
            raise InputUnavailable("config/smbPolicy disappeared after enforcement; refusing audit fallback")
        config = default_config()
    else:
        try:
            _config_id, config = _snapshot_data(config_snap, "config/smbPolicy")
        except PolicyError:
            raise
        except Exception as exc:
            raise InputUnavailable("Firestore smbPolicy snapshot failed") from exc
    return servers, users, config


def _atomic_write_text(path: str | Path, contents: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(contents)
    os.replace(temporary, target)


def reconcile_from_firestore(
    db: Any,
    apply_transaction: Callable[[str], None],
    include_path: str | Path,
    last_good_path: str | Path,
    installed_mode: str | None = None,
    logger: logging.Logger | None = None,
    required: bool = False,
) -> SmbPolicyPlan:
    """Perform the one explicit full-sync policy reconciliation on BASE.

    This is intentionally not a per-VM operation. It fetches complete,
    projected collections, applies one nft transaction, and only after that
    atomically writes the complete persistent table include and the last-good
    snapshot. ``installed_mode=None`` bootstraps a new table in the reviewed
    configuration mode after the complete authoritative read.  Audit remains
    the rollout default, while a replacement BASE can safely join an already
    approved enforce configuration.
    """
    previous = _receipt_plan(last_good_path)
    # Missing configuration is only the initial audit bootstrap default.  Once
    # enforcement has been recorded, a deleted/unreadable config must retain
    # the live table rather than reopening cross-account SMB in audit mode.
    servers, users, config = firestore_snapshots(
        db, missing_config_is_error=required or bool(previous and previous.mode == "enforce"),
    )
    plan = build_policy(servers, users, config)
    log = logger or logging.getLogger(__name__)
    for warning in plan.warnings:
        log.warning("SMB policy v%s: %s", plan.config_version, warning)

    if installed_mode is None:
        # A cold/replacement BASE receives the reviewed live configuration.
        # The audit-only rule was an initial rollout control, not a reason to
        # leave a new BASE unprotected after the global policy is enforce.
        effective_plan = plan
        transaction = render_nft_bootstrap(effective_plan)
    elif previous is None:
        raise PolicyError("SMB policy table exists without a readable last-good receipt")
    elif _receipt_runtime_schema(last_good_path) != RUNTIME_SCHEMA_VERSION:
        # v1 stored enumerated registered addresses.  v2's interval candidate
        # sets are a different nft type, so update them by replacing only this
        # table in one nft transaction.
        effective_plan = plan
        transaction = render_nft_rebuild(effective_plan)
    else:
        effective_plan = plan
        transaction = render_nft_update(effective_plan, installed_mode)

    # Stage the boot include first: a reboot after the live transaction must
    # reload at least the same restrictive policy.  A rejected nft transaction
    # restores the exact old include.  The receipt stays last, after targeted
    # conntrack teardown, so it never certifies an enforcement change whose
    # offloaded sessions might still be active.
    include_target = Path(include_path)
    include_existed = include_target.exists()
    previous_include = include_target.read_text() if include_existed else None
    _atomic_write_text(include_target, render_nft_bootstrap(effective_plan))
    try:
        apply_transaction(transaction)
    except Exception:
        if include_existed:
            _atomic_write_text(include_target, previous_include or "")
        else:
            include_target.unlink(missing_ok=True)
        raise
    revoke_removed_smb_conntracks(previous, effective_plan, force=installed_mode is None)
    write_last_good(last_good_path, effective_plan)
    return effective_plan


def _runtime_firestore_db() -> Any:
    """Initialise the on-BASE Firebase client only for the explicit CLI command."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError as exc:
        raise RuntimeError("firebase_admin is required for sync-policy") from exc
    if not firebase_admin._apps:
        credential_path = os.environ.get("FIREBASE_CREDENTIALS", "/etc/firebase-credentials.json")
        firebase_admin.initialize_app(credentials.Certificate(credential_path))
    return firestore.client()


def _nft_apply(transaction: str) -> None:
    result = subprocess.run(
        ["nft", "-f", "-"], input=transaction, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"nft rejected SMB policy transaction: {result.stderr.strip()}")


@contextlib.contextmanager
def shared_sync_lock(path: str | Path):
    """Use BASE's existing full-sync lock for the standalone CLI only.

    ``sync-base-nat`` already holds this lock around its full sync and calls
    :func:`reconcile_from_firestore` directly, so that function deliberately
    does not acquire it again.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _receipt_mode(last_good_path: str | Path) -> str | None:
    plan = _receipt_plan(last_good_path)
    return plan.mode if plan else None


def nft_policy_table_exists() -> bool:
    """Return table presence, refusing to mistake an nft failure for absence."""
    result = subprocess.run(
        ["nft", "list", "table", "inet", "nvx_smb_policy"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if "No such file or directory" in (result.stderr or ""):
        return False
    raise RuntimeError(f"cannot determine SMB policy table state: {result.stderr.strip()}")


def installed_mode_from_runtime(
    last_good_path: str | Path, table_exists: Callable[[], bool] = nft_policy_table_exists
) -> str | None:
    """Derive safe installed state from the table and its last-good receipt.

    ``None`` means a bootstrap is required.  The reconciler renders the
    reviewed Firestore mode after a complete read, including enforce on a
    cold/replacement BASE.
    """
    receipt_mode = _receipt_mode(last_good_path)
    table_present = table_exists()
    if table_present:
        if receipt_mode is None:
            raise PolicyError("SMB policy table exists but last-good receipt is missing")
        return receipt_mode
    if receipt_mode is None:
        return None
    return None


def _checked_compatibility_mode(requested_mode: str | None, detected_mode: str | None) -> str | None:
    """Keep the old CLI flag only as an assertion, never as state input."""
    if requested_mode is None:
        return detected_mode
    if detected_mode is None:
        raise PolicyError("--installed-mode cannot assume a missing SMB policy table")
    if requested_mode != detected_mode:
        raise PolicyError(
            f"--installed-mode={requested_mode} disagrees with installed receipt mode {detected_mode}"
        )
    return detected_mode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile BASE guest SMB policy from Firestore")
    subcommands = parser.add_subparsers(dest="command", required=True)
    sync = subcommands.add_parser("sync-policy", aliases=["fullsync"])
    sync.add_argument("--installed-mode", choices=sorted(MODES))
    sync.add_argument("--include-path", default="/etc/nftables.d/nvx-smb-policy.nft")
    sync.add_argument("--last-good-path", default="/var/lib/base-nat/smb-policy-last-good.json")
    sync.add_argument("--lock-path", default="/var/lib/base-nat/.sync.lock")
    args = parser.parse_args(argv)
    if args.command not in {"sync-policy", "fullsync"}:
        return 2
    try:
        with shared_sync_lock(args.lock_path):
            installed_mode = _checked_compatibility_mode(
                args.installed_mode,
                installed_mode_from_runtime(args.last_good_path),
            )
            plan = reconcile_from_firestore(
                _runtime_firestore_db(),
                _nft_apply,
                args.include_path,
                args.last_good_path,
                installed_mode,
            )
    except (PolicyError, RuntimeError) as exc:
        logging.error("SMB policy unchanged: %s", exc)
        return 1
    logging.info(
        "SMB policy reconciled: configVersion=%s mode=%s guests=%s/%s pairs=%s/%s",
        plan.config_version,
        plan.mode,
        len(plan.guest_v4),
        len(plan.guest_v6),
        len(plan.allowed_v4),
        len(plan.allowed_v6),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
