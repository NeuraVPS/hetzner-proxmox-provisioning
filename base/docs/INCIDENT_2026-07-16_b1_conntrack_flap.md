# Incident 2026-07-16 — b1 "network flap" was conntrack exhaustion (distributed RDP brute-force)

**Severity:** HEL region served cross-region from b0 for the duration; **no
customer downtime** (the failover watchdog moved the HEL VIPs to b0 and b0
served the fleet transparently — VIP probes stayed 18/18 through the event).

**This corrects the root cause recorded for the 2026-07-13 incident**
(`neuravps-b1-zombie-freeze`), which was blamed on a Hetzner switch-port /
DDoS-mitigation fault. It was almost certainly this same conntrack exhaustion
— conntrack was never inspected on the 13th.

## Symptom

b1's connectivity to the outside world flapped: SSH/TLS/RDP to b1's main IP
(37.27.135.250) succeeded and failed in ~30–90 s windows, from every vantage
(GCP prober, our sandbox, b0's watchdog). b1's userspace was fully alive the
whole time (uptime 2d10h, 0 failed units, no reboot, no ixgbe link events).
The failover watchdog correctly moved the HEL VIPs (77.42.49.79,
2a01:4f9:fff1:5f::) to b0.

## Root cause

```
b1 nf_conntrack_count == nf_conntrack_max == 262144   (table 100% full)
dmesg: "nf_conntrack: table full, dropping packet"    (repeating)
```

The conntrack table was full, so the kernel dropped **new** packets (inbound
*and* outbound) — which is why b1 also saw *b0* as unreachable (symmetric),
and why ICMP looked 100% lost while already-established TCP kept working. As
entries expired, a few packets got through, then it refilled → the flap.

What filled it: a **distributed RDP brute-force**. Snapshot of the table was
~99.5% `ESTABLISHED` connections to the RDP port range (20000–29999), with the
default **5-day** `ESTABLISHED` timeout — abandoned attack connections
accumulate for days. Sources sprayed the *entire* fleet: single IPs held
10k–14k connections across 10k+ **distinct** RDP ports (one SYN per VM). 70+
distinct source IPs were involved; the four heaviest:

```
176.120.22.179     91.202.233.79     45.227.254.0/24     194.165.16.0/24
```

Why the existing guard missed it: `table ip6 rdpguard` rate-limits per
`(saddr . dport)` at 12/min. A sprayer hitting each port once stays under the
per-port limit while opening thousands/min in aggregate. (It also can't even
*see* v4 client IPs — see below.)

## Fix (applied live to b0 + b1, persisted, and in this repo)

1. **`nf_conntrack_max` 262144 → 1048576** (buckets → 262144), and
   **`tcp_timeout_established` 5 days → 1 h**, in the canonical
   `/etc/sysctl.d/99-router.conf` (documented in
   `netns-jool-nat46-nat66-guide.md` §2). 1 h is above any live RDP session's
   keepalive / screen-update interval, so it never drops a real session, and it
   bounds abandoned-attack-flow accumulation to a small fraction of the max.

2. **New per-SOURCE guard `table ip rdpguard`** (see
   `netns-jool-nat46-nat66-guide.md` §7). It sits on the **v4 ingress at raw
   priority (-300)** — before conntrack and before the `ip nat` DNAT.
   - `bf_static` — blocklist of the confirmed sources (dropped before conntrack
     → zero table cost).
   - `bf_src` — inline meter: excess NEW RDP SYNs **over 60/min per source**
     are dropped. Not a persistent ban; under-rate sources always pass.
   - `bf_allow` — trusted infra/ops exemption (bases; add a monitoring box
     on demand for the RDP reachability sweep).

### Why the per-source guard MUST live in the v4 table, not ip6

At `ip6 rdpguard` (prerouting -150, after Jool) a **v4** RDP client is not
visible as itself: v4 RDP to the VIP is DNAT'd by `table ip nat` to 10.0.0.3
and reaches the ip6 hook with **`saddr = the VIP`**. Empirically ~99% of RDP
traffic there carried the VIP as source. A per-`saddr`-only rule at that layer
therefore buckets *every* v4 client into one address and drops them all — this
was hit and rolled back during the incident before moving the logic to the v4
ingress, where `ip saddr` is still the real client. The existing
`(saddr . dport)` ip6 rule survives only because `dport` is unique per VM (it
is effectively a per-destination-VM limit for v4).

## Verification

- b1 flap: b1-direct TCP 8/18 → **20/20** the instant `nf_conntrack_max` was
  raised.
- Customers: VIP RDP probes 5/5 with the v4 guard live (normal-rate clients
  unaffected); `bf_v4_drops` climbing (attack dropped).
- b0 conntrack stable at ~137k / 1,048,576 (**13 %**), no longer climbing.

## Tuning knobs

- `bf_src` rate (`60/minute burst 30`) — lower to shed more attack, raise if a
  legit high-volume source (e.g. big CGNAT) is ever rate-limited.
- `tcp_timeout_established` (3600) — lower bounds accumulation harder; keep
  well above real idle-RDP keepalive intervals.
- Add new confirmed sources to `bf_static`; whitelist ops boxes in `bf_allow`:
  `nft add element ip rdpguard bf_static { 1.2.3.0/24 }`
  `nft add element ip rdpguard bf_allow { 5.6.7.8 }`
