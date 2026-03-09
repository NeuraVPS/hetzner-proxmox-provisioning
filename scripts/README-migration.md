# Migration to public IPv6

## Migrating existing nodes (run on BASE)

1. Ensure BASE has:
   - `/etc/firebase-credentials.json` (Firestore service key)
   - SSH key that can log in to all nodes as root (e.g. `~/.ssh/neuravps_id`); set `NEURAVPS_SSH_KEY` if different.
   - Firestore collection `proxmox_nodes` with one document per node (document id = hostname), each with an `ip` field set to that node's **public IPv6**.

2. Copy the migration script to BASE (or clone the repo), then run as root:
   ```bash
   python3 /path/to/scripts/migrate_to_public_ipv6.py
   ```
   The script will:
   - Read all nodes and BASE from Firestore.
   - Derive each node's VM prefix (/64) from its public IPv6 in Firestore (e.g. 2a01:4f9:xxxx::2 → 2a01:4f9:xxxx::/64).
   - Write `/etc/sync-dnat/nat64-routes.conf` on BASE (VM_PREFIX NODE_PUBLIC_IPV6 per line) and run `apply-nat64-routes.sh`.
   - Generate and write the simplified `cluster.fw` on BASE (with `hosts-ipv6` built from node prefixes).
   - SCP the new `cluster.fw` to every node.
   - On each non-BASE node: write `/etc/sync-dnat/base_public_ipv6`, install `/etc/network/if-up.d/nat64-return-route`, add the route immediately, and restart the firewall.

3. After the script:
   - Update the PVE proxy on BASE so backends use node public IPv6 instead of `fd00:4000::<hex>` (see `docs/pve-proxy-base-server-setup.md`).
   - Ensure `sync-dnat.py` on BASE is the updated version (SNAT to BASE public IPv6). Restart or re-run `sync-dnat.py update_base restore` if needed.

## Node list on BASE

On BASE, the single source of truth is **`/etc/nginx/pve-node-backends.conf`** (nginx map: `node_id https://[ipv6]:8006;`). It is created by `generate-pve-node-backends.py` at setup and updated by first_boot when a new node runs (add/replace that node's line only). To push cluster.fw to all nodes, run **`run_remotes/update_firewall.sh`** from BASE (it reads this file).

`migrate_to_public_ipv6.py` writes BASE's public IPv6 to `/etc/sync-dnat/base_public_ipv6` on each non-BASE node (used by the if-up.d return-route script).

## New installations (install.sh + first_boot.sh)

- **install.sh**: No longer configures VLAN 4000 or internal IPs; only the main interface (Hetzner public IPs) and vmbr0 (NAT + VM IPv6) are configured.
- **first_boot.sh**: Uses `BASE_PUBLIC_IPV6` (default `2a01:4f9:3070:3984::2`; override by exporting before run) for fetching credentials/cluster.fw from BASE and for NAT64 registration. Non-BASE nodes add the return route `64:ff9b::/96 via BASE` on the main interface via if-up.d. The simplified cluster.fw (no hetzner-internal, +dc/base) is written on all nodes. On BASE, `first_boot_base.sh` is run at the end (NAT64 routes, apply-nat64-routes.sh, nat64-boot-restore.service).
- For a different BASE IP, set before running first_boot: `export BASE_PUBLIC_IPV6=2a01:4f9:xxxx::2`.
