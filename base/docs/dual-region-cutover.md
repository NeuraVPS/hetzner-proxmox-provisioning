# Dual-region BASE cutover (Helsinki + Falkenstein)

Runbook for the move to **2 BASEs, one per region**, so every customer reaches
NeuraVPS through the BASE closest to *their* node. Originally prepared
2026-07-03; **updated 2026-07-04 as-built** — the new German BASE (`b0`) is
built and verified, phases 1–2 are DONE, the failover swing and the `b1`
refresh remain.

## Naming (DNS applied 2026-07-04, serial 2026070400)

| Name | What | A / AAAA |
|---|---|---|
| `b00.neuravps.com` | OLD Helsinki base-0 (hostname `0000000-BASE`) — **retiring** | 46.62.188.207 / 2a01:4f9:3090:2488::2 |
| `b0.neuravps.com` | NEW German base (hostname `0000000-BASE`, FSN, 10G uplink) | 188.40.153.120 / 2a01:4f8:2b03:18a9::2 |
| `b1.neuravps.com` | Helsinki base-1 (hostname `0000001-BASE`) | 37.27.135.250 / 2a01:4f9:3070:3984::2 |
| `sqx-hel` / `trading-hel` | Helsinki failover VIP pair (customers) | 77.42.49.79 / 2a01:4f9:fff1:5f::2 |
| `sqx-fsn` / `trading-fsn` | Falkenstein failover VIP pair (customers) | 94.130.3.118 / 2a01:4f8:fff2:95::2 |
| `sqx` / `trading` / `*.pve` | compat CNAMEs → `sqx-hel` (unchanged resolution for every existing customer) | — |
| `*.pve-hel` / `*.pve-fsn` | region-pinned PVE console hosts | CNAME → sqx-hel / sqx-fsn |

The German box was re-IP'd by the 10G-rack move (old note said 94.130.143.26 —
obsolete; 188.40.153.120 is final). Failover routing today: HEL pair → `b1`,
FSN pair → `b0`.

## State as of 2026-07-04

**DONE — `b0` is a fully functional BASE** (built per `base_setup.sh` +
`netns-jool-nat46-nat66-guide.md`, Debian 13):

- Network: all four failover IPs bound with `preferred_lft 0` (FSN pair
  routed here today; HEL pair pre-bound so the future swing is purely a
  Hetzner-side switch). Router sysctls (forwarding, conntrack, BBR).
- NAT stack: `veth-host.service` → nftables (maps + flowtable; ip nat
  accepts main + BOTH failover v4s) → `jool-nat46.service`
  (EAMT `2a01:4f8:2b03:18a9::2/128 ↔ 10.0.0.3`) → `base-nat-boot.service`.
- `sync-base-nat.py` (with the state-lock Firestore read + **named
  `BASE_HOSTS`**: `b0=…,b00=…,b1=…`) synced 1679 VMs into the maps;
  `sync nodes` wrote 210 nginx backends + `/etc/hosts` (`pN` + `b0/b00/b1`).
- nginx PVE proxy with the **extended names** (`*.pve`, `*.pve-hel`,
  `*.pve-fsn` in the map regex + server_name; `sqx/trading` + `-hel/-fsn`
  redirect blocks), noVNC inject served, `pve-set-ticket.service` on :5000
  (env copied from b1).
- TLS: `/etc/letsencrypt` cloned from b1 → valid for the compat names
  (`*.pve`, `sqx`, `trading`). **Pending: DNS-01 cert covering the new
  `-hel`/`-fsn` names** (needs a Hetzner DNS API token; see remaining steps).
- Fleet firewall: `[IPSET base]` in `cluster.fw` (Storage Box) now includes
  `2a01:4f8:2b03:18a9::/64 # BASE 0 GERMANY (b0) IPv6`, pushed to all
  reachable nodes via `sync nodes sync-firewall`.
- `/root/migrate_vm.sh` + `migrate_vms_batch.sh` installed (md5 = repo =
  b1). The `PEER_BASES` case maps hostname `0000000-BASE` → b1 — correct
  for b0 as-is.
- E2E verified: IPv4 NAT46 + IPv6 NAT66 customer path through the FSN VIP
  reaches VMs on Helsinki nodes; PVE console via `{node}.pve-fsn` serves
  from the internet (HTTP 200 ~130 ms).

**Operating rule during the transition (operator):** `b00` and `b1` are
FROZEN — they serve production; read-only access allowed (that is how
secrets/certs were cloned), no config changes. All new ops run from `b1`
(or `b0` for its own build) — `ssh b0|b00|b1` by name.

## Remaining phases

1. **[DONE 2026-07-04] Cert for the new names on b0** — issued as cert-name
   `neuravps-dual` (12 SANs: `*.pve`, `pve`, `*.pve-hel/-fsn` + apexes,
   `sqx`, `trading`, `sqx/trading-hel/-fsn`; expires rolling ~90d) via
   certbot DNS-01 with `snippets/certbot-hetzner-dns-hooks.py` against the
   **Hetzner Cloud API** (DNS moved into api.hetzner.cloud; the legacy
   dns.hetzner.com 301s; the old Namecheap §7 flow is obsolete). Token at
   `/opt/letsencrypt/hetzner.env` (600). Renewal is AUTOMATIC: the hooks
   are recorded in `renewal/neuravps-dual.conf` and Debian's certbot.timer
   drives it. Hook gotchas (learned): the Cloud DNS API cannot update an
   rrset's records in place (PUT touches metadata only, no
   actions/set-records) → the hook merges via DELETE+POST; wildcard+apex
   pairs share one `_acme-challenge` rrset (two TXT values); wait for ALL
   THREE authoritative NS (hydrogen/oxygen/helium) — the secondaries lag
   under bursts and LE hits transient NXDOMAIN if you only poll one.
   nginx on b0 points both server blocks at `live/neuravps-dual/`.
   (Also fixed on the way: pip's newer `cryptography` in /usr/local broke
   Debian's pyOpenSSL — `pip3 install --break-system-packages
   --ignore-installed pyopenssl` re-aligns it; certbot works after.)
2. **Swing the HEL failover pair to b0** (operator, Hetzner panel/API):
   both VIP pairs then route to b0, which already binds the IPs and
   accepts them in nftables. Customers notice nothing (same IPs).
   Immediately verify: fleet sweep of rdp/smb ports against BOTH VIPs
   from outside + a PVE console open via `*.pve`.
3. **Refresh b1** (its own phase, operator will schedule): update
   `sync-base-nat.py` (state-lock read + named BASE_HOSTS →
   `/etc/default/base-nat`), extend nginx names + map regex like b0,
   install the new-names cert, and **update `PEER_BASES` for hostname
   `0000001-BASE` → b0's IPv6** (`2a01:4f8:2b03:18a9::2`) in
   `/root/migrate_vm.sh` (repo case still points at b00 — change repo +
   b1 together in this phase). Then swing the HEL pair back to b1.
4. **Repoint ops + retire b00**: move any orchestrators/logs still under
   b00 `/root`, update `secrets_config.BASE_SSH_HOST_IPS` (Cloud Functions
   currently SSH b00+b1 for NAT sync — must become b0+b1), remove b00 from
   `BASE_HOSTS` on both survivors, drop its line from `[IPSET base]` in
   cluster.fw, then cancel the server in Hetzner.
5. **connectionUrl by node location** (NeuraVPS repo): panel/provisioning
   pick `sqx-hel`/`sqx-fsn` (`trading-*` for MT) from the node's
   `location` field. Compat names keep working for existing customers.

## Gotchas learned during the b0 build (2026-07-04)

- `pip3 install firebase-admin` on Debian 13 fails against the dpkg-owned
  `requests` — use `--break-system-packages --ignore-installed`.
- The nftables drop-in has `ReadWritePaths=/var/lib/base-nat` — create the
  directory BEFORE the first `systemctl restart nftables` or the unit dies
  with `status=226/NAMESPACE`.
- Install `pve-proxy-map.conf` before the first `sync nodes`, or its
  `nginx -t` validation fails on the unknown `$pve_node_from_host`
  variable (harmless but noisy).
- `BASE_HOSTS` named entries use `=` (`b0=2a01:…`) because `b0`/`b00`/`b1`
  are valid IPv6 hextets — a colon separator would parse as an address.
- Node PVE firewalls drop traffic from a base whose /64 is not yet in
  `[IPSET base]` — a brand-new base cannot reach nodes (nor push
  cluster.fw itself) until a peer base pushes the updated ipset:
  bootstrap order matters.

## fsn-v6 failover kick (2026-07-04)

A newly-**assigned** Hetzner v6 failover subnet may not route to its server
until a real `active_server_ip` **switch** happens (the initial assignment
doesn't trigger propagation). Symptom: 0 packets reach the base (tcpdump)
despite the IP being bound and the Robot API showing it assigned;
traceroute6 loops in the Hetzner backbone. Config is NOT the cause (it's
byte-identical to a working failover). **Fix = kick it:** POST the failover
to the *other* base, wait for it to apply, POST it back. Verified fix on
fsn-v6 (2a01:4f8:fff2:95::).
