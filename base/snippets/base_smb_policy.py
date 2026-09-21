#!/usr/bin/env python3
"""Plan the BASE guest-to-guest SMB policy from a complete Firestore snapshot.

This module deliberately does not read Firestore or run ``nft``.  Its pure
``build_policy`` entry point is intended to be called by the existing BASE
sync path after it has fetched *complete* ``servers``, ``users`` and
``config/smbPolicy`` snapshots.  Keeping that boundary explicit prevents a
partial read from turning into an empty allow-list.

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
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_MODE = "audit"
MODES = frozenset(("audit", "enforce"))
TRANSIT_V6 = ip_network("2a01:4f9:c01f:e:ffff::/112")
SMB_PORT = 445


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

    def snapshot(self) -> dict[str, Any]:
        """Stable, non-PII serialisation suitable for a last-good file."""
        return {
            "schemaVersion": self.schema_version,
            "configVersion": self.config_version,
            "mode": self.mode,
            "guestV4": list(self.guest_v4),
            "guestV6": list(self.guest_v6),
            "allowedV4": [list(pair) for pair in self.allowed_v4],
            "allowedV6": [list(pair) for pair in self.allowed_v6],
            "allowedServerPairs": [list(pair) for pair in self.allowed_server_pairs],
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


def _samba_enabled(server: Mapping[str, Any]) -> bool:
    firewall = server.get("firewall")
    if not isinstance(firewall, Mapping):
        return True
    enabled = firewall.get("sambaEnabled")
    return enabled if isinstance(enabled, bool) else True


def _servers(
    raw_servers: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    raw_users: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> tuple[tuple[Server, ...], dict[str, frozenset[str]]]:
    users = _collection_by_id(raw_users, "users")
    raw = _collection_by_id(raw_servers, "servers")
    if not raw:
        raise InputUnavailable("servers snapshot is empty; refusing to erase a fleet policy")

    links: dict[str, frozenset[str]] = {}
    for uid, user in users.items():
        linked = user.get("linkedAccountIds", [])
        if linked is None:
            linked = []
        if not isinstance(linked, list) or not all(isinstance(x, str) for x in linked):
            raise InventoryValidationError(f"users/{uid}.linkedAccountIds is not a string list")
        links[uid] = frozenset(x for x in linked if x and x != uid)

    seen_vmids: set[int] = set()
    seen_v4: set[str] = set()
    seen_v6: set[str] = set()
    result: list[Server] = []
    for server_id, item in raw.items():
        owner = _nonempty_string(item.get("userId"), f"servers/{server_id}.userId", InventoryValidationError)
        if owner not in users:
            raise InventoryValidationError(f"servers/{server_id} owner is absent from users snapshot")
        try:
            vmid = int(item.get("proxmoxId"))
        except (TypeError, ValueError) as exc:
            raise InventoryValidationError(f"servers/{server_id}.proxmoxId is invalid") from exc
        if vmid < 0 or vmid > 9999 or vmid in seen_vmids:
            raise InventoryValidationError(f"duplicate or out-of-range VMID {vmid}")
        seen_vmids.add(vmid)

        v4 = _normalise_guest_ip(item.get("ipv4"), 4, f"servers/{server_id}.ipv4")
        v6 = _normalise_guest_ip(item.get("ipv6"), 6, f"servers/{server_id}.ipv6")
        if not v4 and not v6:
            raise InventoryValidationError(f"servers/{server_id} has no registered guest address")
        if v4 and v4 in seen_v4:
            raise InventoryValidationError(f"duplicate guest IPv4 {v4}")
        if v6 and v6 in seen_v6:
            raise InventoryValidationError(f"duplicate guest IPv6 {v6}")
        if v4:
            seen_v4.add(v4)
        if v6:
            seen_v6.add(v6)

        # A server with Samba disabled cannot need an SMB peer permission.
        if _samba_enabled(item):
            result.append(Server(server_id, owner, vmid, v4, v6))
    return tuple(sorted(result)), links


def _partner_pairs(config: Mapping[str, Any], servers: Iterable[Server]) -> frozenset[frozenset[str]]:
    pairs = config.get("partnerPairs", [])
    if pairs is None:
        pairs = []
    if not isinstance(pairs, list):
        raise ConfigValidationError("partnerPairs must be a list")
    by_id = {server.server_id: server for server in servers}
    out: set[frozenset[str]] = set()
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
            raise ConfigValidationError(f"partnerPairs[{index}] left server/owner binding is stale")
        if by_id.get(right_id) is None or by_id[right_id].owner_uid != right_owner:
            raise ConfigValidationError(f"partnerPairs[{index}] right server/owner binding is stale")
        canonical = frozenset((left_id, right_id))
        if canonical in out:
            raise ConfigValidationError(f"partnerPairs[{index}] duplicates a reviewed pair")
        out.add(canonical)
    return frozenset(out)


def validate_config(config: Mapping[str, Any] | None, servers: Iterable[Server]) -> tuple[int, str, frozenset[frozenset[str]]]:
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
    return version, mode, _partner_pairs(config, servers)


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
    known, links = _servers(servers, users)
    version, mode, partners = validate_config(config, known)
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
        guest_v4=tuple(sorted(server.ipv4 for server in known if server.ipv4)),
        guest_v6=tuple(sorted(server.ipv6 for server in known if server.ipv6)),
        allowed_v4=tuple(sorted(v4)),
        allowed_v6=tuple(sorted(v6)),
        allowed_server_pairs=tuple(sorted(allowed_servers)),
    )


def _set_elements(values: Iterable[str] | Iterable[tuple[str, str]]) -> str:
    rendered = []
    for value in values:
        rendered.append(" . ".join(value) if isinstance(value, tuple) else value)
    return ", ".join(rendered)


def _candidate_rules(plan: SmbPolicyPlan, action: str) -> list[str]:
    """Rules for both requests and safe SMB replies; never use conntrack NEW."""
    rules: list[str] = []
    for family, guests, allowed in (("ip", "smb_guests_v4", "smb_allowed_v4"), ("ip6", "smb_guests_v6", "smb_allowed_v6")):
        for direction in (f"tcp dport {SMB_PORT}", f"tcp sport {SMB_PORT} tcp flags & (syn | ack) != syn"):
            base = f'iifname "tun-*" oifname "tun-*" {direction} {family} saddr @{guests} {family} daddr @{guests}'
            rules.append(f'{base} {family} saddr . {family} daddr @{allowed} counter comment "smb-policy known pair"')
            suffix = "counter drop" if action == "drop" else "counter"
            rules.append(f'{base} {family} saddr . {family} daddr != @{allowed} {suffix} comment "smb-policy unknown candidate"')
    return rules


def render_nft_bootstrap(plan: SmbPolicyPlan) -> str:
    """Render a self-contained table.  It is safe to validate with ``nft -c``.

    The policy hook is before the legacy forward accept rule.  Known traffic is
    only counted, never accepted here, so the existing SMB SYN rate limiter and
    any later BASE policy still run.  In audit mode unknown candidates are also
    only counted; in enforce mode they are dropped.
    """
    action = "drop" if plan.mode == "enforce" else "audit"
    lines = [
        "# Generated by base_smb_policy.py; do not hand-edit.",
        "# This table is intentionally independent: no flush ruleset.",
        "table inet nvx_smb_policy {",
        "  set smb_guests_v4 { type ipv4_addr; elements = { " + _set_elements(plan.guest_v4) + " } }",
        "  set smb_guests_v6 { type ipv6_addr; elements = { " + _set_elements(plan.guest_v6) + " } }",
        "  set smb_allowed_v4 { type ipv4_addr . ipv4_addr; elements = { " + _set_elements(plan.allowed_v4) + " } }",
        "  set smb_allowed_v6 { type ipv6_addr . ipv6_addr; elements = { " + _set_elements(plan.allowed_v6) + " } }",
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


def write_last_good(path: str | Path, plan: SmbPolicyPlan) -> None:
    """Atomically persist a plan only after the caller applied its nft update."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(plan.snapshot(), indent=2, sort_keys=True) + "\n")
    os.replace(temporary, target)


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
