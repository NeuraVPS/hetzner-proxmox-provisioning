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

**Armed since 2026-07-25** (was dry-run for one day; new BASEs now ship armed).

The dry-run measured the port-diversity distribution the design rests on, and
the gap is wide enough to act on:

| distinct ports | b0 | b1 |
|---|---|---|
| 200+ | 0 | 60 |
| 50-200 | 0 | 6 |
| 20-50 | 1 | 1 |
| 8-20 | 0 | 1 |
| under 8 | 84 | 126 |

The highest non-sweeper was 16 ports (`88.198.66.15`, a rented Hetzner box that
was ALSO over the 60/min rate limit — an attacker, just a slower one); every
other legitimate source sat at 1-4. Arming blocked 68 sources and immediately
started dropping ~185 pps of attack traffic on b1.

**The one realistic false positive is a shared egress IP** — an office or prop
firm where 20+ people each RDP into their own VPS from one NAT. Symptom: a
group of customers loses RDP at the same moment, from the same location, with
everything green on our side. Fix in 10 seconds, and it self-heals in 24h
anyway:

    nft add element ip rdpguard bf_allow { <their.public.ip> }
    nft delete element ip rdpguard bf_auto { <their.public.ip> }

Then persist the allow entry in `/etc/nftables.conf`.
