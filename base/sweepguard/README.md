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

## Three layers

`deploy_portguard.sh` adds a **third**, and it turned out to be the one that
matters most for actual customer harm — see "Concentrated attacks" below.

## Two independent detectors (sweepguard.py)

**1 — port diversity.** A real client opens its own VM's port; the busiest
customer owns 7 servers. Sweepers touch 20-686. See below.

**2 — connection flood** (added 2026-07-25, after the operator asked whether
100+ attempts/min is simply an attack regardless of who it is — it is).
Detector 1 has a hole: `bf_seen` records a source on its NEW SYN and ages out
after 1h, so an attacker that opens many connections and HOLDS them goes
unseen. Measured on b1: six sources holding 100-1372 live RDP connections had
a `bf_seen` count of ZERO — two coordinated /24 clusters (`88.214.25.121/123/
124/125` and `91.238.181.92/94`), 2725 connections, invisible to detector 1.

`maxConns` (default 100) blocks on connections held concurrently, read from
conntrack. Live distribution on b1 when it was written:

    1000+ conns    2 sources        30-100 conns    2 sources, BOTH attackers
    300-1000       3 sources        10-30           9 sources  <- highest legit
    100-300       11 sources         1-10         185 sources

Nothing legitimate sat between 30 and 100. Armed, it blocked exactly those six
and nothing else.

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
    maxConns      live concurrent RDP connections (100 = 3x the busiest real
                  source ever observed)
    maxAddsPerRun cap on blocks per run, bounds the blast radius of a bug
    blockSeconds  how long an automatic block lasts (86400)
    hotPortMinTotal      LIVE connections on one port = port under attack (10)
    hotPortMinSrc        LIVE connections from one source on that port (5)
    hotPortMaxIdle       an ESTABLISHED connection is live if its last packet is
                         at most this old, in seconds (300 = one pass)
    sessionMemorySeconds how long a proven RDP session keeps a source out of the
                         hot-port detector (86400; 0 = only the current one)
    hotPortBlockSeconds  how long a hot-port block lasts (3600)

No base carries this file today (checked 2026-09-27 on b0 and b1): the
`DEFAULTS` in `sweepguard.py` are what runs.

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

## What does NOT identify a customer

Four candidate signals were tested against the 67 blocked sources on b1.
**All four fail**, which is why port diversity is the only input:

| signal | why it fails |
|---|---|
| rDNS / geography | The heaviest attacker is `customer.sntochl1.isp.starlink.com` — a residential Starlink line at 730 ports. Also seen: FTTH, Telecom Italia "business", ISPs in Peru/Brazil/Argentina. A botnet *is* compromised home machines. |
| ESTABLISHED connection | 31 of the 67 have one; one holds 2846. Brute-forcers complete the TCP handshake, then fail auth. |
| hitting real assigned ports | Median blocked source hits **99.7%** ports that map to a live VM. They work from a target list, they do not sweep blindly. |
| in-guest successful logon (4624) | The guest never sees the client's public IP — NAT46 rewrites the source to an address in our own prefix. Only the BASE ever sees the real IP, which is precisely why the block belongs here. |

Byte accounting (`net.netfilter.nf_conntrack_acct=1`, enabled on both BASEs
2026-07-25, persisted in `/etc/sysctl.d/99-neuravps-conntrack.conf`) was the one
signal that looked like it *would* discriminate — a real session moves
megabytes, a brute-force moves kilobytes.

**Revisited 2026-07-29: it does not.** With accounting on for four days, the
largest byte total on ANY (source, port) pair in the customer range on b1 was
**1767 bytes**; attackers p99 = 236, everything else p99 = 897. The RDP data
path is not accounted here, so there is nothing to discriminate with. Two more
were tested the same day and also fail:

| signal | why it fails |
|---|---|
| conntrack byte counters | Max 1767 B over 1441 pairs. No separation. |
| TCP state / connection age | Attackers p90 64006, others p90 307405 — but the ranges overlap all the way to the maximum. |
| "familiar source" memory per port | Learning who connects to a port while it is *not* under attack looks sound, and fails: it memorised `88.214.25.121/124` and `91.238.181.94` — the very clusters the hot-port detector exists to catch — because they hold 1-4 connections across a handful of VMs, which is exactly a customer's shape. Built, measured, discarded. |

## The false positive this design can actually produce

The detector's only input is (source IP, destination port). So the ONLY way a
customer gets caught is **many distinct customers sharing one source IP**:
a shared office egress, carrier-grade NAT on a mobile/ISP pool, a popular VPN
exit node, or a reseller. Which of those it would be is not predictable, and as
the table above shows it is not identifiable from the address either.

What IS measured is the margin. Lowest source ever blocked: **27 ports**.
Largest legitimate source observed: **18** (`88.198.66.15` — itself
attacker-shaped: rented Hetzner box, also over the 60/min rate limit). Every
other free source sits at 1-4. The largest real customer owns 7 servers.

Symptom if it ever happens: a group of customers loses RDP at the same moment,
from the same location, with everything green on our side. Fix in 10 seconds,
and it self-heals in 24h regardless:

    nft add element ip rdpguard bf_allow { <their.public.ip> }
    nft delete element ip rdpguard bf_auto { <their.public.ip> }

Then persist the allow entry in `/etc/nftables.conf`.

### The false positive the HOT-PORT detector produces — and it did

That analysis covers detectors 1 and 2. Detector 3 (hot-port, below) has a
different and worse failure mode, and it is **structural, not a margin
problem**: the legitimate owner of the attacked port is, by definition, one of
the sources piling connections onto it. When their VM stops answering they
reconnect, and the retries push them over `hotPortMinSrc`.

**Real case, 2026-07-29.** A customer with 4 servers reached 6 connections on
his own port during an attack on it and was blocked for 24h. Because the block
is by source IP on the shared v4 VIP, he lost **all four servers and the VNC
console at once** — the one symptom support reads as "our edge is down". He was
IPv4-only at home, so every check we ran (all over IPv6, from allow-listed
addresses) came back green. Ten hours down, five support emails, no diagnosis —
and the log line said only `6 connections onto ONE attacked port`, without the
port, so nobody could even tell whose machine it was.

Two changes came out of it, both in `sweepguard.py`:

* the hot-port log now always names the **port and the VM**, plus a `NOTE` line
  spelling out that this detector is ambiguous;
* `hotPortBlockSeconds` (default **3600**) replaces the 24h block for this
  detector only. A real attacker is re-detected on the next 5-minute pass, so
  protection is unchanged; a wrongly-blocked customer gets their service back in
  1h instead of 24h.

The thresholds were deliberately NOT raised: 6 is already above the measured
p95 of 4, so no threshold separates these two populations.

It happened twice more (2026-08-30: a stale `.rdp` shortcut retrying against
another customer's VM; 2026-09-26/27: 27 hourly blocks of a customer holding
SSH sessions to their own VM), both patched by hand with `bf_allow`. The root
cause turned out not to be the thresholds but WHAT was counted — fixed on
2026-09-27, see "Hot-port counts LIVE connections" below.

**Triage, when a customer says "all my servers AND the VNC console died at once,
and it works from my phone":** that shape is a source-IP block until proven
otherwise. Check it first, before touching anything else:

    journalctl -u neuravps-sweepguard.service --since "24 hours ago" | grep <their.ip>
    nft get element ip rdpguard bf_auto { <their.public.ip> }

Their address will not be in the guest logs — everything arrives SNATed to the
BASE VIP — so get it from the customer (`ifconfig.me`) or match the ISP against
the hot-port blocks in the journal.


## Concentrated attacks — `deploy_portguard.sh`

The operator's framing, which was right: a botnet spread thin across many VMs
costs us little. The damage is one VM taking thousands of attempts, because
that is what locks the customer's account and wedges their RDP.

The v4 table had no per-(source,port) limit — only 60/min per source. So one IP
could hammer ONE VM at 60 attempts/min forever, and several IPs coordinating on
one port were invisible to everything:

* port-diversity (`minPorts` 20) — they touch 1-2 ports
* connection flood (`maxConns` 100) — connect→fail→close leaves ~1 live
  connection at any instant no matter how fast they go
* per-source rate (`bf_src` 60/min) — they stay under it

Measured the moment the rule went in, on b1: port **21845 under attack from six
distinct sources at once**. In-guest confirmation on that VM:

| VM | failed logons 24h | successful RDP 24h | top target |
|---|---|---|---|
| 1845 (port 21845) | **17936** | **0** | ADMINISTRATOR ×16589 |
| 389 (port 20389) | 1189 | **0** | ADMINISTRATOR ×739 |

Zero successful logins under that volume is a paying customer locked out of
their own machine.

**Rate limit, not a block, and that is deliberate.** It can only throttle; it
can never lock anyone out. A real client needs ONE successful connection, and
12/min with burst 15 leaves that untouched even during an RDP auto-reconnect
storm. Measured legitimate behaviour: median **1**, p95 **4** concurrent
connections to a given port. An attacker needs thousands and gets 12.

The ip6 table already had this rule. Only v4 — the family that judges
essentially every real client — was missing it.


## Slow DISTRIBUTED attacks on one VM — `hotPortMinTotal` / `hotPortMinSrc`

The hardest case, and the one that was actually hurting a customer. Six sources
at ~2 connections/min each, all aimed at port 21845:

* invisible to port diversity — they touch ONE port
* invisible to the connection flood — ~12 connections each, far under 100
* invisible to every rate limit — 2/min is slower than any real client

And rate limiting could not have fixed it anyway: **the auth attempts ride
inside connections that are already open.** No new SYN is ever sent, so there
is nothing to rate-limit. Only a block drops packets on an established
connection.

Two conditions must BOTH hold, which is what makes blocking safe here:

* the port carries `hotPortMinTotal` (20) connections — measured on b1, 472
  customer ports sat at 1-4 and 422 at 5-9;
* and the source contributes `hotPortMinSrc` (5) of them — a real client holds
  1-4 (p95 = 4).

(Those were the July numbers, counting every conntrack entry. Since
2026-09-27 both conditions count only LIVE connections, and `hotPortMinTotal`
is 10 — see the next-but-one section.)

Armed, it picked exactly the four heaviest of that cluster and nothing else.
Effect on the attacked VM within four minutes: live connections **67 → 16**,
failed logons **12.4/min → 4.8/min**.

**It does not reach zero, and that is deliberate.** The remaining sources hold
fewer than 5 connections each; catching them would mean dropping the threshold
into the range where real clients live. Closing that last gap needs evidence
the BASE does not have — which is the argument for making NAT46 carry the
client's real address (see below).

## Known gap: the guest cannot see who is attacking it

NAT46 rewrites the source into our own prefix, so the in-guest Security log
records our address, never the client's. That is why the strongest possible
signal is unavailable: a VM with thousands of 4625s and zero 4624s KNOWS it is
under attack, but cannot say by whom, and the BASE — which knows the addresses
— cannot see the failed logons.

Encoding the client IPv4 into the NAT46 source (RFC 6052 style, a /96 inside
the VM's own prefix) would join the two halves and let a VM's own failed-logon
flood drive blocks at the edge. It is a change to the data path for every
customer, so it belongs in a planned window, not during an attack.

## Guests are judged only when they go to OUR addresses (2026-09-18)

Since 2026-08-15 every guest's Internet egress crosses the BASE, with source
`10.64.x.y` (v4) or its identity `2a01:4f9:c01f:e::/64` (v6). The guard looked
only at the destination PORT, so a guest talking to any Internet service in
10000-39999 was counted as attacking our forwards: a MetaTrader broker on AWS
Global Accelerator (214xx/220xx, vm1261), RustDesk 21116, Syncthing 22000, a
DigitalOcean service on 25060… From 05/09 to 18/09 all 113 guest blocks in the
journal went to Internet destinations; vm570/vm581 were re-blocked every day.
While blocked, a guest loses every NEW connection to those ports — its broker
reconnect. The hot-port and flood detectors had the same bug (they read
conntrack by port only: 25060 blocked 10.64.4.36/4.113 several times).

A forward only exists on OUR addresses, so:

* `deploy_guest_dst_scope.sh` creates `nuestras4` / `nuestras6` (interval sets:
  both bases' main IPs, the four VIPs, `10.0.0.0/8`, the guest identity /64,
  `fd00::/8`) and ONE rule per family right after the existing exemptions:

      ip  saddr 10.64.0.0/16         ip  daddr != @nuestras4 accept
      ip6 saddr 2a01:4f9:c01f:e::/64 ip6 daddr != @nuestras6 accept

  Live in ONE `nft -f` transaction (no flush, no reload of nftables.conf),
  persisted scoped to each rdpguard table, checked with `nft -c`.
* `sweepguard.py` skips any conntrack flow whose ORIGINAL destination is not in
  that set. If the set is missing (a BASE without this change) it logs it and
  behaves as before.
* Both main IPs go in the set on BOTH bases: a guest leaving through b1 towards
  b0's main IP reaches b0 with b1's main IP as source, which is in `bf_allow` —
  it can only be seen at the base it leaves through.
* **When a new address gets forwards** (e.g. the egress-ipv4-pools /26s), add
  it to the set: `nft add element ip rdpguard nuestras4 '{ a.b.c.d/26 }'` and
  in `/etc/nftables.conf` (and in the script).

Undo: `rollback_guest_dst_scope.sh` (targeted: deletes the two rules by handle
and the two sets in one transaction, strips the conf, restores the pre-change
`.py`). Tested on a netns clone of the tables in both bases (conf restored byte
for byte, tables identical) and for real on b0.

Also fixed on the way: conntrack prints IPv6 uncompressed and nft compressed,
so an already-blocked v6 source was "BLOCKED" again every run (210 log lines for
one Google Cloud scanner). Addresses are now normalised.

Tests: `python3 base/sweepguard/test_sweepguard_dst.py`.

## Hot-port counts LIVE connections and never judges a proven RDP session (2026-09-27)

Third customer blocked by this detector: `79.158.167.228`, owner of VMs
1634/1741/1742, blocked **27 times, once an hour**, from 2026-09-26 07:43Z,
losing RDP to 1742 each time. Measured the next morning on b0 and b1 (five
conntrack samples 3 min apart plus the journal since 13/09), the cause was not
the thresholds but what the detector counted.

**What the data showed**

* `nf_conntrack_acct` and `nf_conntrack_timestamp` are **0** on both bases, and
  `nf_conntrack_tcp_timeout_established` is the kernel default **432000 (5 days)**,
  not the 1h set in July (that sysctl file did not survive the base
  replacements). No bytes and no age, but the remaining timeout still gives
  **silence**: the kernel resets it on every packet, so
  `432000 - remaining` = seconds since the last packet.
* Everything with traffic sits in the flowtable (`[OFFLOAD]`, evicted after 30 s
  without packets). Across the samples, **not one** ESTABLISHED entry outside the
  flowtable had less than 30 min of silence; most had hours to days.
* The detector counted those dead entries — and **the block manufactures
  them**: dropping a source's packets freezes its connections in ESTABLISHED
  for 5 days. Each hour the block expired and the next pass re-blocked on the
  same corpses. On b0, 3849 hot-port blocks since 13/09 came from **92
  sources** and 93 % were re-blocks ≤75 min after the previous one (b1: 9555
  from 3905 sources, 42 %).
* The customer's 24 SSH connections to 31634 had their last packets between
  07:19 and 07:43:12Z on 26/09 — the first block was at 07:43:22Z. Every block
  after that counted connections the first block had killed. At that first
  block only 7 had traffic in the previous 5 minutes, all his, on his own port.
* 113 sources had a replied RDP UDP flow (the multitransport leg a client only
  opens after a completed login). **None** of them was ever blocked by any
  detector since 05/09 — except this customer.

**The rule** (`hot_port_scan`)

1. Only LIVE connections count, both for the source and for the port total:
   `[OFFLOAD]`, any state other than ESTABLISHED (SYN_SENT, SYN_RECV, TIME_WAIT,
   CLOSE… all expire in ≤2 min, i.e. connect/fail/close churn — exactly what
   brute force leaves), or ESTABLISHED with ≤ `hotPortMaxIdle` (300 s) of
   silence. If the sysctl cannot be read, every entry counts (old behaviour).
2. A source with a **proven RDP session** — a UDP flow to a 2xxxx forward that
   has a reply (`[ASSURED]` or `[OFFLOAD]`, never `[UNREPLIED]`) — is not judged
   by hot-port; it is logged as `SKIP … RDP session now|in memory`. The session
   is remembered `sessionMemorySeconds` (24 h) in
   `/var/lib/neuravps-sweepguard/sessions.json`, which covers the owner of an
   attacked VM whose session has just died and who is reconnecting (the
   2026-07-29 case). An unreadable file starts empty: the detector gets
   stricter, never looser. Rate limits and detectors 1 and 2 still apply.
3. Thresholds on live connections: `hotPortMinSrc` 5 (unchanged) and
   `hotPortMinTotal` **10** (was 20 on all entries). Live, no real VM port went
   above 2 connections and no source above 2 on one port in any sample; 10 keeps
   a 5x margin and recovers the 31337 pairs (two sources, 13-16 live between
   them) that 20 would have lost once dead entries stopped padding the total.

Not used, and why: bytes (accounting is off and the data path is not
accounted here anyway); age (no timestamps — silence is not age); the VM owner.
The owner was evaluated with the Firestore map: of 157 hot-port-blocked sources
still visible, exactly one touched ≥2 VMs of the attacked VM's owner — this
customer, whom rules 1 and 2 already cover — while 270 of 9167 sources touch
≥2 VMs of one owner. It would add a dependency (the base only has same-owner
pairs inside `/var/lib/base-nat/smb-policy-last-good.json`) for no measured gain.

**Dry run** (`hotport_dryrun.py`, same samples, bf_allow ignored)

| | b0 | b1 |
|---|---|---|
| sources flagged in the 5 samples, old → new | 6 → 0 | 11 → 3 |
| dropped: dead connections only / proven session | 6 / 0 | 8 / 0 |
| hot-port blocks in force at 10:56Z | 5 | 25 |
| … would still be blocked (all SYN floods on 25565, no VM there) | 0 | 18 |
| … would not (dead connections / session) | 5 / 0 | 6 / 1 |
| first blocks still reconstructible: new rule also blocks | 2 of 5 | 4 of 10 |
| `79.158.167.228` | — | old BLOCKS, new does not (0 of 23-24 live; RDP session) |

The new rule never flagged a source the old one did not. The first-block rows
it does not reproduce are lower bounds (expired SYN/TIME_WAIT entries are no
longer visible): the customer (7 live, correct), four sources with at most 4
live connections still visible (`94.26.68.91` and `220.166.134.10` show 0 and
1: their first block in the window was already on dead connections), and
**single sources
bursting 5-9 SSH connections at one port with nobody else on it**
(`167.172.88.141` →30360, `209.38.19.25` →30537, `20.150.193.0` and
`74.249.179.215` →31337). Those stay capped by `bf_port` (12 new/min) and
`bf_src`; lowering `hotPortMinTotal` to 5 would catch them and would also have
blocked the customer's first 7.

**Deploying** (no nftables change; only the script):

    scp base/sweepguard/{sweepguard.py,hotport_dryrun.py} bX:/root/sgrepo/
    ssh bX 'python3 /root/sgrepo/sweepguard.py --dry-run'           # logs only, no blocks, no memory
    ssh bX 'python3 /root/sgrepo/hotport_dryrun.py /proc/net/nf_conntrack'
    ssh bX 'cp -a /usr/local/sbin/sweepguard.py /usr/local/sbin/sweepguard.py.bak.hotport.$(date +%Y%m%d-%H%M%S) \
            && install -m 0755 /root/sgrepo/sweepguard.py /usr/local/sbin/sweepguard.py'

The timer picks it up on its next pass (≤5 min); check
`journalctl -u neuravps-sweepguard.service` for `SKIP … RDP session` and
`BLOCKED … live connections`. Then remove the hand patch on both bases:
`nft delete element ip rdpguard bf_allow { 79.158.167.228 }`. Rollback:
`install -m 0755` the `.bak.hotport.*` copy back. `deploy_sweepguard.sh` (full
install on a new base) installs `/root/sweepguard.py` the same way.

Tests: `python3 base/sweepguard/test_sweepguard_hotport.py` (cases from the
samples and the three customers).
