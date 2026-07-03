# Dual-region BASE cutover (Finland + Germany)

Runbook to stand up a **German BASE** and retire one of the two Finnish ones,
so every customer reaches NeuraVPS through the BASE closest to *their* node
(lower NAT-path latency). Prepared 2026-07-03 while the infra is being staged
with Hetzner — **not yet executed**.

## Target state

- **2 BASEs total** (cost), one per region: keep one Finnish BASE, add a German
  one. The German BASE **replaces `0000000-BASE`** (base-0) — chosen because it
  is the one *without* the primary failover IP associated in the Hetzner panel.
  ⚠️ Verify before cutover: base-0 currently *carries* `77.42.49.79` +
  `2a01:4f9:fff1:5f::2` live on its interface, so the failover must be swung
  off it first.
- **German BASE hardware** = Hetzner **#2982184**, `94.130.143.26`, subnet
  `2a01:4f8:13b:2d03::`, dc FSN1 — the former `0000148-EX44`, emptied
  2026-07-03 (VM 1648 live-migrated to node 26) and renamed to a BASE. As of
  2026-07-03 it is **powered off while Hetzner moves it to a 10G-Uplink rack**
  (a BASE must have the 10G uplink like the current ones for the NAT traffic of
  a whole region).
- **Second failover IPv4 + IPv6** added. DNS today: `sqx.neuravps.com` +
  `trading.neuravps.com` → the single failover. New:
  `sqx-de.neuravps.com` + `trading-de.neuravps.com` → the new failover.
  Normal ops: `-de` names → German failover → German BASE; current names →
  Finnish BASE. Maintenance on one BASE: point BOTH failovers at the survivor
  (swing is by DNS / failover-IP reassignment, never by touching customer
  config). Keep low DNS TTLs on these records for a fast swing.

## What makes a BASE (rebuild the German one from these)

Everything a BASE needs is in this `base/` folder — build the German box with
the same steps, then apply the cutover-specific items below.

| Piece | Source |
|---|---|
| Bootstrap runbook (executable) | `base/base_setup.sh` |
| NAT46/NAT66 (netns + Jool) | `base/docs/netns-jool-nat46-nat66-guide.md` + `snippets/nftables-base-nat.conf`, `base-nat-boot.*` |
| nginx `*.pve.neuravps.com` proxy + SSO set-ticket + wildcard TLS | `base/docs/pve-proxy-base-server-setup.md` + `snippets/neuravps-redirects.conf`, `pve-set-ticket.*`, `pve-proxy-*` |
| Dynamic NAT sync from Firestore | `snippets/sync-base-nat.py` (→ `/usr/local/sbin/`), `base-nat-boot.service` |
| noVNC send-string inject | `snippets/novnc-send-string-inject.js` |
| sshd burst limits for fleet sweeps | `snippets/50-neuravps-maxstartups.conf` |

Live-vs-repo audited 2026-07-03: **zero drift** — the snippets match the
running BASE byte-for-byte, so a fresh build from this folder is faithful.

## Cutover-specific steps (NOT covered by base_setup.sh)

1. **`/root/migrate_vm.sh` is copied by hand, NOT auto-deployed** (canonical:
   repo `scripts/migrate_vm.sh`). Copy it to the German BASE, and **update the
   `PEER_BASES` hostname `case`** (near the top of the script) — it maps each
   BASE hostname → the *other* BASE's IPv6 so rollback/success NAT reconcile
   reaches the peer. With base-0 retired the two entries become
   `<Finnish-BASE-hostname>` ↔ German IPv6 and `<German-hostname>` ↔ Finnish
   IPv6. `migrate_vms_batch.sh` inherits it (shells out per-VM).
2. **`/etc/hosts` `sync-base-nat` block** (managed; `# BEGIN/END sync-base-nat`)
   + the per-BASE static lines — `sync-base-nat.py sync nodes` regenerates the
   node lines; the BASE self-lines are static. Ensure the German BASE resolves
   itself and the peer.
3. **NAT state**: run `sync-base-nat.py sync` on the German BASE after DNS/
   failover are live (rebuilds `/var/lib/base-nat/state.json` + nft maps from
   Firestore). Both BASEs hold the *same* NAT state — the VIP is what moves.
4. **SSH access / ops hop**: base-0 (`2a01:4f9:3090:2488::2`) is the default
   jump host for Cloud Functions and all ops tooling (fleet sweeps, migrate
   orchestrators, monitors, `/root/*.sh`). If base-0 is the one retired,
   **repoint the ops entry point to the survivor** and re-home any orchestrator
   scripts / logs living under base-0 `/root` first. Cloud Functions read the
   jump host from `secrets_config.BASE_SSH_HOST_IPS` — update that list.
5. **connectionUrl by node location** (NeuraVPS app, separate repo): today the
   customer URL is a fixed `sqx.neuravps.com:2<vmid>`; it must choose `-de` vs
   current by the node's `location` field (Helsinki/Falkenstein, already
   backfilled on `proxmox_nodes`). `trading.*` is the MetaTrader equivalent.
6. **Retire base-0** in Hetzner only after 1–5 verified and the failover is off
   it.
