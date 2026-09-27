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

## Zero-impact detection drill

`base/snippets/failover-watchdog-drill.sh` (installed on both bases) fakes a
base's death by dropping ONLY the watchdog's probe traffic — peer pings +
GCP TCP probes to the MAIN IPs. Customer traffic (VIPs) is untouched; the
caller's SSH IP is excluded; a dead-man's switch removes the rules after
15 min. With `dryRun:true` the whole chain fires (reporter → arbiter →
`[SIMULACRO]` email) without moving anything.

Validated live 2026-07-07 (victim b0, reporter b1, 0 user impact): fails
1/6→6/6 in exactly 3 min → arbiter answered
`{"action":"dry_run","wouldMove":["94.130.3.118","2a01:4f8:fff2:95::"]}` —
correctly selecting ONLY the two VIPs pointing at the victim — and the
simulacro email landed at soporte@. Clean recovery on disarm.

```bash
ssh b0 '/usr/local/sbin/failover-watchdog-drill.sh arm'    # start
ssh b1 'journalctl -u failover-watchdog.service -f'        # watch
ssh b0 '/usr/local/sbin/failover-watchdog-drill.sh disarm' # end
```

⚠️ With `dryRun:false` the same drill performs a REAL swap (established RDP
sessions on the moved VIPs reconnect) — use only as a deliberate rehearsal.

## Known limits

- ICMP is the base-side signal; the CF verdict uses TCP 443+22 (GCP can't ping).
- A partial outage (base up but NAT broken) is NOT detected — this watches
  base liveness, not data-plane correctness (node_health covers deeper checks).
- Robot POSTs are async: timeouts/409 during the swap are normal; the confirm
  GET decides. VIP propagation is 2–3 min at Hetzner's side.

## First real drill — 26/09/2026 (b0 → b1, `dryRun:false`)

Result: detection → VIPs on b1 in ~6.3 min; new connections via the FSN VIPs
failed for ~25 s; established sessions through a moved VIP hang (no conntrack
on the other base); manual fail-back cost ~1 min on FSN v4 and nothing on v6.

Lessons, all fixed or recorded:

- `failover-watchdog-drill.sh` picked the guest-network `…ffff::2` as the
  base's main v6 on the ECC bases, so the peer's first probe
  (`https://[PEER_V6]/`) kept succeeding and the drill would never fire. The
  main v6 is now the default-route source address.
- The arbiter Cloud Function hit its 120 s timeout (504) during a real swap,
  and the reporter's curl gave up at 90 s ("HTTP 000"). The CF timeout is now
  300 s (NeuraVPS `functions/main.py`) and the reporter waits up to 320 s.
- Robot allows **100 `GET /failover` per hour**. Do not poll it in a loop during
  a drill; watch where new connections land with `conntrack -E -e NEW` on each
  base instead, and use one `GET /failover` (list) at the end.
- Not covered by this drill: the egress path (b0 kept doing NAT) and a real
  whole-base outage.

## Second drill — 27/09/2026 (b0 dead in the data plane too, `dryRun:false`)

Method: on b0, `failover-watchdog-drill.sh arm-dataplane` (probe rules plus all
GRE dropped in and out, so the nodes' ip6gre tunnels see a mute base). Window
00:58–01:07 UTC, a Saturday night; b0 was mute for 8 min 37 s.

Results (T0 = arm):

- Nodes: the 38 FSN nodes switched by themselves to the HEL tunnel in 21–36 s,
  egress SNAT'd with each VM's HEL pair IP; they came back ~1 min after the VIP
  v6 reached b1.
- Arbiter: peer reported at T+3:10, swap decided 3 s later; the reporter did
  get its HTTP 200 (fixes #250/#494 hold).
- Inbound: FSN VIPs cut ~5–6 min (hostnames ~5 min, v4 ~5 min 15 s, v6
  ~6 min 10 s). Through the HEL VIPs the FSN VMs had **no failures** at all,
  so a customer can reach the VM via the other region's hostname during a base
  outage.
- Fail-back (manual Robot POST of the two FSN VIPs, after b1 logged
  "recovered"): FSN v4 ~1 min of failures.
- Not measured: whether established outbound connections survive the nodes'
  second route change back to the FSN tunnel; `both_down` and a real hardware
  failure remain untested.

Safeguards used, keep them for the next one:

- Set `config/conncheck.dryRun=true` for the whole window so no conncheck pass
  can create distress (and with it `guestAutoFix` inside guests); restore after.
- Avoid the hourly passes: conncheck (b0 :05, b1 :35) and egresscheck (b0 :15,
  b1 :45), each 1–3 min.
- Do not poll Robot in a loop (100 GET/h); one GET before and one at the end.

Correction to the 26/09 notes: "b0 kept doing NAT" was wrong. The tunnels are
anchored to the VIP v6, so FSN egress had already moved to b1 with the VIP.
Cutting only the forward path does **not** trigger `neuravps-tunnel-probe`
(the probe is local TCP/443 through the tunnel); hence the GRE drop.
