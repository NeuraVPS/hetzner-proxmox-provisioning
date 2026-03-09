#!/usr/bin/env python3
"""
Migrate cluster from internal IPs (fd00:4000::x) to public IPv6.

Run on BASE as root. Requires:
- /etc/firebase-credentials.json
- SSH key that can reach all nodes (e.g. ~/.ssh/neuravps_id)
- Firestore proxmox_nodes with document id = hostname, field ip = public IPv6

Steps:
1. Read proxmox_nodes from Firestore; get BASE and all nodes' public IPv6.
2. Derive VM prefix (/64) from each node's IP (e.g. 2a01:4f9:xxxx::2 -> 2a01:4f9:xxxx::/64), build nat64-routes.conf.
3. Write /etc/sync-dnat/nat64-routes.conf on BASE and run apply-nat64-routes.sh.
4. Build cluster.fw (simplified template, hosts-ipv6 from node IPs) and write to BASE.
5. Push cluster.fw to every node via SCP.
6. On each non-BASE node: write /etc/sync-dnat/base_public_ipv6, install if-up.d
   for 64:ff9b::/96 return route, pve-firewall restart.
"""
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("Install firebase_admin (pip install firebase_admin)", file=sys.stderr)
    sys.exit(1)

SSH_KEY = os.environ.get("NEURAVPS_SSH_KEY", "/root/.ssh/neuravps_id")
CREDS_FILE = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
CLUSTER_FW_PATH = "/etc/pve/firewall/cluster.fw"
NAT64_ROUTES = "/etc/sync-dnat/nat64-routes.conf"
BASE_IPV6_FILE = "/etc/sync-dnat/base_public_ipv6"


def run(cmd, check=True, capture=True):
    kwargs = {"capture_output": capture, "text": True, "timeout": 60}
    if capture:
        kwargs["capture_output"] = True
    r = subprocess.run(cmd, **kwargs, check=check)
    return (r.stdout or "").strip() if capture else None


def ssh(host_ipv6, command, check=True):
    """Run command on host via SSH. host_ipv6 is the node's public IPv6."""
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10", "-i", SSH_KEY,
        f"root@[{host_ipv6}]",
        command,
    ]
    return run(cmd, check=check)


def ipv6_to_64_prefix(ipv6: str) -> str:
    """Derive /64 prefix from node's public IPv6 (e.g. 2a01:4f9:xxxx::2 -> 2a01:4f9:xxxx::/64)."""
    try:
        import ipaddress
        net = ipaddress.IPv6Network(ipv6 + "/64", strict=False)
        return str(net)
    except Exception:
        return ""


def main():
    if os.geteuid() != 0:
        print("Run as root", file=sys.stderr)
        sys.exit(1)
    if not Path(SSH_KEY).is_file():
        print(f"SSH key not found: {SSH_KEY}", file=sys.stderr)
        sys.exit(1)
    if not Path(CREDS_FILE).is_file():
        print(f"Credentials not found: {CREDS_FILE}", file=sys.stderr)
        sys.exit(1)

    if not firebase_admin._apps:
        cred = credentials.Certificate(CREDS_FILE)
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    coll = db.collection("proxmox_nodes")

    base_node_id = None
    base_ipv6 = None
    nodes = []  # (node_id, ipv6)
    node_ipv6_to_prefix = {}  # for building hosts-ipv6 /64s
    for doc in coll.stream():
        doc_id = doc.id
        data = doc.to_dict() or {}
        ip = (data.get("ip") or "").strip()
        if not ip:
            continue
        if "BASE" in doc_id.upper():
            base_node_id = doc_id
            base_ipv6 = ip
        else:
            nodes.append((doc_id, ip))

    if not base_ipv6:
        print("No BASE node (proxmox_nodes doc id containing 'BASE') with ip found", file=sys.stderr)
        sys.exit(1)
    print(f"BASE: {base_node_id} -> {base_ipv6}")
    print(f"Non-BASE nodes: {len(nodes)}")

    # 1. Derive VM_PREFIX (/64) from each node's public IPv6
    routes_lines = []
    for node_id, ipv6 in nodes:
        prefix = ipv6_to_64_prefix(ipv6)
        if prefix:
            routes_lines.append(f"{prefix} {ipv6}")
            node_ipv6_to_prefix[ipv6] = prefix
            print(f"  {node_id}: {prefix} -> {ipv6}")
        else:
            print(f"  {node_id}: invalid IPv6 {ipv6}, skip", file=sys.stderr)

    # 2. Write nat64-routes.conf on BASE
    Path(NAT64_ROUTES).parent.mkdir(parents=True, exist_ok=True)
    Path(NAT64_ROUTES).write_text("\n".join(routes_lines) + "\n")
    print(f"Wrote {NAT64_ROUTES}")
    run(["/usr/local/bin/apply-nat64-routes.sh"], check=False)
    print("Applied nat64 routes")

    # 3. Build cluster.fw: base ipset = BASE's IPv6 + IPv4 (we have IPv6; IPv4 from node or keep placeholder)
    base_ipv4 = "37.27.135.250"  # placeholder; update if different
    try:
        out = run(["ip", "-4", "route", "get", "8.8.8.8"], check=False)
        if out:
            m = re.search(r"src\s+(\S+)", out)
            if m:
                base_ipv4 = m.group(1)
    except Exception:
        pass
    hosts_ipv6_lines = [f"{prefix} # {nid}" for nid, ipv6 in nodes for prefix in [node_ipv6_to_prefix.get(ipv6)] if prefix]
    # Add BASE's /64
    base_prefix = ipv6_to_64_prefix(base_ipv6)
    if base_prefix:
        hosts_ipv6_lines.insert(0, f"{base_prefix} # {base_node_id}")
    hosts_ipv6_block = "\n".join(hosts_ipv6_lines)

    cluster_fw = f"""[OPTIONS]

policy_in: DROP
enable: 1
policy_out: ACCEPT
policy_forward: DROP

[ALIASES]

NAT-Gateway 10.0.0.1

[IPSET base]

{base_ipv6}
{base_ipv4}

[IPSET hosts-ipv6]

{hosts_ipv6_block}

[IPSET nat64-clients]

64:ff9b::/96

[RULES]

IN PMG(ACCEPT) -source +dc/base -log nolog
GROUP management
IN DHCPfwd(ACCEPT) -i vmbr0 -log nolog
IN DHCPv6(ACCEPT) -i vmbr0 -log nolog

[group management]

IN SSH(ACCEPT) -log nolog

[group vm-default]

IN SSH(ACCEPT) -source +dc/base -log nolog
IN SSH(ACCEPT) -source +dc/nat64-clients -log nolog
IN RDP(ACCEPT) -source +dc/base -log nolog
IN RDP(ACCEPT) -source +dc/nat64-clients -log nolog
IN SMB(ACCEPT) -source +dc/base -log nolog
IN SMB(ACCEPT) -source +dc/nat64-clients -log nolog
IN SMB(ACCEPT) -source +dc/hosts-ipv6 -log nolog

[group vm-no-internet]

IN SSH(ACCEPT) -source +dc/base -log nolog
IN SSH(ACCEPT) -source +dc/nat64-clients -log nolog
IN RDP(ACCEPT) -source +dc/base -log nolog
IN RDP(ACCEPT) -source +dc/nat64-clients -log nolog
IN SMB(ACCEPT) -source +dc/base -log nolog
IN SMB(ACCEPT) -source +dc/nat64-clients -log nolog
IN SMB(ACCEPT) -source +dc/hosts-ipv6 -log nolog
IN DROP -log nolog
OUT DROP -log nolog

[group vm-no-rdp]

IN RDP(DROP) -log nolog

[group vm-no-samba]

IN SMB(ACCEPT) -source +dc/hosts-ipv6 -log nolog
IN SMB(DROP) -log nolog

[group vm-no-ssh]

IN SSH(DROP) -log nolog

[group vm-public-ipv6]

IN ACCEPT -source ::/0 -log nolog
"""

    # 4. Write cluster.fw on BASE
    Path(CLUSTER_FW_PATH).write_text(cluster_fw)
    print(f"Wrote {CLUSTER_FW_PATH} on BASE")
    run(["pve-firewall", "restart"], check=False)

    # 5. Push cluster.fw to each node
    for node_id, ipv6 in nodes:
        run([
            "scp", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-i", SSH_KEY,
            CLUSTER_FW_PATH,
            f"root@[{ipv6}]:{CLUSTER_FW_PATH}",
        ], check=False)
        print(f"  Pushed cluster.fw to {node_id}")

    # 6. On each non-BASE node: write base_public_ipv6, install if-up.d, restart firewall
    if_up_script = f"""#!/bin/bash
# NAT64 return route: 64:ff9b::/96 via BASE (public IPv6)
[[ "$ADDRFAM" = inet6 ]] || exit 0
ip -6 addr show dev "$IFACE" 2>/dev/null | grep -q 'scope global' || exit 0
[[ -f {BASE_IPV6_FILE!r} ]] || exit 0
gw=$(cat {BASE_IPV6_FILE!r})
[[ -n "$gw" ]] || exit 0
ip -6 route add 64:ff9b::/96 via "$gw" 2>/dev/null || true
"""
    if_up_b64 = base64.b64encode(if_up_script.encode()).decode()
    for node_id, ipv6 in nodes:
        run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10", "-i", SSH_KEY, f"root@[{ipv6}]",
             f"mkdir -p /etc/sync-dnat && printf '%s' '{base_ipv6}' > {BASE_IPV6_FILE}"], check=False)
        run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10", "-i", SSH_KEY, f"root@[{ipv6}]",
             f"echo {if_up_b64} | base64 -d > /etc/network/if-up.d/nat64-return-route && chmod +x /etc/network/if-up.d/nat64-return-route"], check=False)
        ssh(ipv6, "pve-firewall restart", check=False)
        ssh(ipv6, f"ip -6 route add 64:ff9b::/96 via {base_ipv6} 2>/dev/null || true", check=False)
        print(f"  Configured return route + firewall on {node_id}")

    print("Migration done. Update PVE proxy on BASE to use node public IPv6 backends (see docs).")
    print("Update sync-dnat.py on BASE (SNAT to BASE public IPv6) and restart services if needed.")


if __name__ == "__main__":
    main()
