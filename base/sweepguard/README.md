# RDP sweep guard (auto-block port sweepers)

Complements the existing `rdpguard` rate limits on a BASE. Those limit how FAST
a source may open new RDP connections; they cannot stop a **slow** sweep. The
botnet that reached a customer's VM on 2026-07-25 ran at ~3.5 conns/min spread
over 675 different customer ports — far under the 60/min per-source limit and
under the per-(source,port) ip6 limit, yet it produced 5051 failed logons in 24h
on one VM and left the customer staring at a black screen.

**What separates an attacker from a client is not rate, it is port diversity.**
A real client opens its own VM's port; the busiest customer owns 7 servers. The
sweepers touch 20-686 distinct ports.

## How it works
* `bf_seen` (`addr . port`, 1h timeout) — a no-verdict rule records every new
  RDP SYN as a (source, port) pair.
* `sweepguard.py` (systemd timer, every 5 min) counts DISTINCT PORTS per source
  and drops those over `minPorts` into `bf_auto`.
* `bf_auto` (`addr`, 24h timeout) is dropped by the chain — and every automatic
  block **expires by itself**, so a wrong block heals without intervention.

Chain order matters and is asserted by the deploy script:

    bf_allow accept -> bf_static drop -> bf_auto drop -> record -> rate-limit

`bf_allow` must stay FIRST: if `bf_auto` were evaluated before it, an ops box or
one of our own bases could be auto-blocked despite being allow-listed.

## Families
The v4 table is authoritative for v4 clients: by the time traffic reaches the
ip6 side it has been SNATed to the BASE's own VIP, so every customer looks like
one source there. The ip6 detector therefore skips `64:ff9b:1::/96` and only
judges NATIVE v6 clients (NAT66).

## Config — /etc/neuravps-sweepguard.json
    enabled       kill switch
    dryRun        log what WOULD be blocked, block nothing (ships ON)
    minPorts      distinct-port threshold (20 = 3x the largest real customer)
    maxAddsPerRun cap on blocks per run, bounds the blast radius of a bug
    blockSeconds  how long an automatic block lasts (86400)

Ship with `dryRun: true`, watch `journalctl -u neuravps-sweepguard` for a few
days, confirm no customer ever appears, then set it to false.
