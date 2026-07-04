# BASE servers

The two BASE hosts are the fleet's front door: nftables + Jool NAT46/NAT66 for
customer RDP/SMB forwarding, the `*.pve.neuravps.com` nginx proxy (PVE GUI /
VNC consoles + SSO set-ticket), and the SSH jump used by Cloud Functions and
ops tooling to reach the (otherwise firewalled) Proxmox nodes.

| Piece | Where |
|---|---|
| Bootstrap runbook (executable) | `base_setup.sh` |
| NAT46/NAT66 (netns + Jool) guide | `docs/netns-jool-nat46-nat66-guide.md` |
| nginx PVE proxy / set-ticket / wildcard TLS guide | `docs/pve-proxy-base-server-setup.md` |
| **Dual-region (DE+FI) cutover runbook** | `docs/dual-region-cutover.md` |
| Deployed config snippets (curl'd by the runbook) | `snippets/` |

**`/root/migrate_vm.sh` (+ `migrate_vms_batch.sh`) are BASE-resident but
NOT built by `base_setup.sh`** — canonical is repo `scripts/migrate_vm.sh`,
copied to each BASE by hand (backup + `bash -n` + md5 before `mv`). Its
`PEER_BASES` hostname `case` must list the *other* BASE(s); update it whenever
the BASE set changes (see the cutover runbook).

## Operational changes log

- **2026-07-04 — German b0 built as-built + dual-region names + NAT-sync race
  fix.** The new Falkenstein BASE (`b0.neuravps.com`, hostname `0000000-BASE`)
  was stood up from this folder on Debian 13 and E2E-verified (NAT46/NAT66
  customer path via the FSN VIP, PVE console via `*.pve-fsn`, fleet SSH hop);
  see `docs/dual-region-cutover.md` for the full as-built state, remaining
  phases (HEL failover swing, b1 refresh, b00 retirement) and the build
  gotchas (pip `--ignore-installed` on Debian 13, `/var/lib/base-nat` before
  the first nftables restart, `[IPSET base]` bootstrap ordering). Snippets
  updated to the dual-region form: `pve-proxy-map.conf` +
  `neuravps-redirects.conf` now accept/serve `*.pve-hel` / `*.pve-fsn` and
  `sqx/trading-hel/-fsn` (b1 still runs the previous single-region copies
  until its refresh phase — intentional divergence, tracked in the runbook).
  `sync-base-nat.py` gained **named `BASE_HOSTS`** (`b0=…,b00=…,b1=…`) and,
  crucially, the arg-less per-VM sync now reads Firestore **inside the state
  lock**: paired with NeuraVPS PR #80 (cloud triggers stopped passing
  event-derived flags), this closes the out-of-order-trigger race that kept
  dropping VM 231's RDP forward while SMB survived (customer-visible on
  Jul 1 + Jul 4).

- **2026-07-03 — migrate_vm.sh hardening series** (canonical `scripts/migrate_vm.sh`,
  deployed to both BASEs by hand; audited zero-drift the same day). Four fixes
  from the fleet base-config conversion, all relevant to any BASE running
  migrations: (1) **in-guest IPv6 reconfig retry** — waits for the guest agent
  and retries up to 4× gated on the address actually binding (slow cutovers
  otherwise left the guest on the source-prefix IP, customer unreachable);
  (2) **NAT restore on rollback + peer push** — rollback re-runs
  `sync-base-nat.py sync <vmid>` locally *and* on `PEER_BASES`, and the success
  path pushes the new address to the peer (live migrations fire no VM
  start/stop event, so the peer otherwise stays stale — VM 1648 connectionUrl
  dead ~35h); (3) **memory config-vs-running pre-check** — a pending
  `qm set --memory` (un-rebooted vps-e) made remote_migrate build the dest at
  the config size and the RAM stream died with `kvm: Size mismatch: pc.ram`
  after ~40min; now auto-aligns before copying and re-applies the intended
  value as pending on dest; (4) **vps-e balloon adaptation** across
  EX44↔AX162 offline moves (AX162 shared → `balloon=18432`, EX44 dedicated →
  `balloon==memory`). New `PEER_BASES` (hostname-derived) — keep it current
  when the BASE set changes. Errors log now records only non-recoverable cases
  (recoverable retries are `_info`).

- **2026-06-12 — sshd rate limits for fleet sweeps** (`snippets/50-neuravps-maxstartups.conf`,
  installed by `base_setup.sh` step 1b; applied live to both BASEs the same day).
  `node_health_check` (daily + on-demand) and `run_remotes/*` hop through the
  BASEs with bursts of short SSH connections; the default `MaxStartups
  10:30:100` randomly reset part of every burst
  (`kex_exchange_identification: read: Connection reset by peer`), which showed
  up as false "unreachable" nodes in sweeps and failed mass rollouts. Raised to
  `MaxStartups 60:30:200` + `MaxSessions 30`. Even so, keep ad-hoc sweep
  concurrency moderate (≤ 12 across both BASEs) — the limit protects the BASEs
  from connection floods.
