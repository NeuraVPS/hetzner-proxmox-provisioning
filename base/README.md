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
| Deployed config snippets (curl'd by the runbook) | `snippets/` |

## Operational changes log

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
