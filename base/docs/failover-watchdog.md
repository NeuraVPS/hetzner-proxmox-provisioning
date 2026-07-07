# Automatic BASE failover watchdog

Moves the 4 failover VIPs to the surviving base when a base dies, and emails
soporte@neuravps.com. Built 2026-07-07 (operator spec).

## Architecture

```
b1 ── ping b0 every 30s ──┐              ┌── TCP-probes BOTH bases (443+22, v4+v6)
b0 ── ping b1 every 30s ──┤              │
                          ▼              ▼
             failover-watchdog.sh   failover_watchdog CF  ──►  Hetzner Robot API
             (6 fails ≈ 3 min)      (THE ARBITER, in GCP)      (swap failover VIPs)
                          │              │
                          └── report ────┘──►  email soporte@ + Firestore event log
```

- **The base never swaps anything itself** — it only *reports*. The Cloud
  Function re-verifies both bases from GCP (third vantage point) so a base
  with a broken uplink can never steal the VIPs from its healthy peer
  (split-brain protection).
- **Swap rule**: only VIPs whose `active_server_ip` points AT the confirmed-dead
  base are moved. Manual maintenance switches (both bases alive) never match →
  no action, no email — exactly the operator's requirement.
- **No automatic fail-back**: after recovery a human moves the VIPs back.
- **Both-bases-down**: email CRITICAL, no swap (no clear survivor).

## Pieces

| Piece | Where |
|---|---|
| `failover-watchdog.sh` + `.service`/`.timer` (30 s) | each base — `/usr/local/sbin` + `/etc/systemd/system` (source: `base/snippets/`) |
| `/etc/neuravps/failover-watchdog.env` | per-base: SELF/PEER, peer IPs, CF_URL, TOKEN (mode 600) |
| CF `failover_watchdog` | `NeuraVPS/functions/failover_watchdog.py` (+ `main.py`), secrets `HETZNER_ROBOT_CREDENTIALS`, `FAILOVER_WATCHDOG_TOKEN`, `SMTP_PASSWORD` |
| Config gate | Firestore `config/failover_watchdog`: `enabled`, `maintenance`, `dryRun`, `cooldownMinutes` (15) |
| Event log | Firestore `failover_watchdog_events` |

## Operations

- **Operator maintenance** (planned base work): either just do it — a manual
  VIP switch with both bases alive never triggers anything — or belt-and-braces
  set `maintenance: true` in `config/failover_watchdog` while working.
- **Dry-run mode** (`dryRun: true`, initial state): on a confirmed outage the CF
  emails `[SIMULACRO] would move …` but does NOT touch VIPs. Set `dryRun: false`
  to arm for real (after the live rehearsal with the operator).
- **Disable everything**: `enabled: false`.
- **Cooldown**: one action per 15 min max (anti-flapping, protects the
  ~100 req/h Robot budget).
- **Fail-back after an incident**: move VIPs back manually (Robot API POST per
  VIP; lean pattern: POST + ~20 s + 1 confirm GET) once the dead base is healthy.

## Verifying it's alive

```bash
systemctl status failover-watchdog.timer        # on each base
journalctl -u failover-watchdog.service -n 20   # ping results / reports
# CF log: gcloud functions logs read failover_watchdog --gen2 --region europe-west1 --limit 20
```

## Known limits

- ICMP is the base-side signal; the CF verdict uses TCP 443+22 (GCP can't ping).
- A partial outage (base up but NAT broken) is NOT detected — this watches
  base liveness, not data-plane correctness (node_health covers deeper checks).
- Robot POSTs are async: timeouts/409 during the swap are normal; the confirm
  GET decides. VIP propagation is 2–3 min at Hetzner's side.
