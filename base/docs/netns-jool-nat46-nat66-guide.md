# NAT46 + NAT66 Router (Jool + Netns + nftables)

## Overview

This guide assumes a **clean install of Debian 13** (trixie) on the router host. It implements:

- **NAT46 (IPv4 → IPv6)** using Jool (stateless SIIT) in **netfilter** mode
- **NAT66 (IPv6 → IPv6)** using nftables for dynamic port forwarding

Flow:

```
IPv4 client → public IPv4
    ↓ (nft DNAT → 10.0.0.3)
Jool (netns) → NAT46 → IPv6 router
    ↓ (nft NAT66)
Final IPv6 backend
```

---

## 1. Base Network Configuration

Edit `/etc/network/interfaces`:

```bash
auto lo
iface lo inet loopback

auto enp6s0
iface enp6s0 inet dhcp

iface enp6s0 inet6 static
    address <YOUR_IPV6>/64
    gateway fe80::1
```

Disable cloud-init networking:

```bash
echo "network: {config: disabled}" > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
```

---

## 2. Enable Forwarding

```bash
cat >/etc/sysctl.d/99-router.conf <<EOF
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
# Keep RA processing on WAN while forwarding is enabled
# (replace enp6s0 if your WAN interface is different)
net.ipv6.conf.enp6s0.accept_ra=2

net.netfilter.nf_conntrack_max=524288
net.netfilter.nf_conntrack_buckets=131072
net.core.somaxconn=65535
net.core.netdev_max_backlog=250000
net.ipv4.tcp_max_syn_backlog=262144
net.ipv4.ip_local_port_range=10240 65535

# --- TCP buffer auto-tuning (min / default / max bytes) ---
# Larger buffers prevent stalls on bursty RDP graphics updates and SMB
# file transfers. Default max (212992) is far too small for a NAT forwarder.
net.core.rmem_max=16777216
net.core.wmem_max=16777216
net.ipv4.tcp_rmem=4096 131072 16777216
net.ipv4.tcp_wmem=4096 65536 16777216

# --- UDP buffer sizes (critical for RDP UDP transport) ---
# UDP has no auto-tuning; the default must be large enough for RDP-over-UDP
# (used by modern mstsc for lower latency on lossy links).
net.core.rmem_default=1048576
net.core.wmem_default=1048576

# --- BBR congestion control + fair queueing ---
# BBR achieves higher throughput at lower latency than CUBIC. Mostly benefits
# the BASE box's own TCP (nginx, Firestore), but also improves any locally
# terminated RDP flows. Windows VMs should be tuned separately.
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr

# --- Conntrack timeout tuning for NAT forwarding ---
# Default tcp_timeout_established is 432000s (5 days); 1h reclaims dead
# RDP/SMB entries much faster and prevents conntrack table exhaustion.
net.netfilter.nf_conntrack_tcp_timeout_established=3600
net.netfilter.nf_conntrack_udp_timeout=30
net.netfilter.nf_conntrack_udp_timeout_stream=120

# --- Faster socket recycling & keepalives ---
net.ipv4.tcp_timestamps=1
net.ipv4.tcp_tw_reuse=1
net.ipv4.tcp_fin_timeout=15
net.ipv4.tcp_keepalive_time=600
net.ipv4.tcp_keepalive_intvl=30
net.ipv4.tcp_keepalive_probes=5

# --- SMB throughput hardening ---
# tcp_sack recovers from loss without full retransmit; tcp_no_metrics_save
# prevents the kernel from caching pessimistic metrics from a single bad
# SMB session and degrading all subsequent connections to the same VM.
net.ipv4.tcp_window_scaling=1
net.ipv4.tcp_sack=1
net.ipv4.tcp_no_metrics_save=1

# --- Disable slow-path features on a dedicated forwarder ---
net.ipv4.conf.all.log_martians=0
net.ipv4.conf.default.log_martians=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv6.conf.all.accept_redirects=0
net.ipv6.conf.default.accept_redirects=0
EOF

sysctl --system
```

---

## 3. Create Network Namespace

```bash
ip netns add jool

ip link add veth-host type veth peer name veth-ns
ip link set veth-ns netns jool

ip addr add 10.0.0.1/24 dev veth-host
ip -6 addr add fd00::1/64 dev veth-host
ip link set veth-host up

ip netns exec jool ip addr add 10.0.0.2/24 dev veth-ns
ip netns exec jool ip addr add 10.0.0.3/32 dev veth-ns
ip netns exec jool ip -6 addr add fd00::2/64 dev veth-ns
ip netns exec jool ip link set veth-ns up
ip netns exec jool ip link set lo up

ip netns exec jool ip route add default via 10.0.0.1
ip netns exec jool ip -6 route add default via fd00::1
```

---

## 4. Routing

```bash
ip route add 10.0.0.3 via 10.0.0.2 dev veth-host
ip -6 route add 64:ff9b:1::/96 via fd00::2 dev veth-host
```

---

## 5. Install Jool (Debian packages)

Install the kernel module (DKMS) and userspace tools from Debian. The packaged Jool targets **netfilter** for SIIT; you do **not** need iptables or ip6tables rules inside the `jool` netns for translation.

```bash
apt update
apt install -y jool-dkms jool-tools linux-headers-amd64
```

Sanity check:

```bash
modprobe jool_siit
jool_siit --version
```

Complete **§5** before **§6** and before enabling `jool-nat46.service` later, so `jool_siit` and `jool_siit.ko` are available.

---

## 6. Configure Jool

```bash
ip netns exec jool jool_siit instance add br46 --netfilter
ip netns exec jool jool_siit -i br46 global update pool6 64:ff9b:1::/96

# Map router IPv6 <-> internal IPv4
ip netns exec jool jool_siit -i br46 eamt add <ROUTER_IPV6>/128 10.0.0.3/32
```

With `jool_siit instance add … --netfilter`, Jool registers SIIT in that network namespace via the netfilter framework.

---

## 7. nftables Rules

Create `/etc/nftables.conf`:

> Replace `enp6s0` with your real WAN interface (for example `enp1s0`).
> Replace `37.27.135.250` and `77.42.49.79` with your two public IPv4 addresses.
> Replace `2a01:4f9:3070:3984::2` with your **main** public IPv6.
> Do **not** put an IPv6 address in `table ip nat` rules.

```nft
flush ruleset

table ip nat {
    chain prerouting {
        type nat hook prerouting priority dstnat;
        # Accept ingress on both main and failover IPv4.
        ip daddr 37.27.135.250 tcp dport 10000-29999 dnat to 10.0.0.3
        ip daddr 37.27.135.250 udp dport 20000-29999 dnat to 10.0.0.3
        ip daddr 77.42.49.79 tcp dport 10000-29999 dnat to 10.0.0.3
        ip daddr 77.42.49.79 udp dport 20000-29999 dnat to 10.0.0.3
    }

    chain postrouting {
        type nat hook postrouting priority srcnat;
        # For DNAT'ed flows, keep symmetry: reply from the same public IPv4 hit by client.
        ct status dnat snat to ct original daddr
        # Default egress source for non-DNAT traffic (prefer main IPv4).
        ip saddr 10.0.0.0/24 oifname "enp6s0" snat to 37.27.135.250
    }
}

table ip6 nat {
    # O(1) DNAT via maps. Structure lives here; element population is
    # handled by sync-base-nat.py (which only touches map elements, never
    # rules or the map declarations themselves).
    map rdp_tcp_map {
        type inet_service : ipv6_addr . inet_service
    }
    map rdp_udp_map {
        type inet_service : ipv6_addr . inet_service
    }
    map smb_tcp_map {
        type inet_service : ipv6_addr . inet_service
    }

    # Persisted map elements (written by sync-base-nat.py). Restoring this
    # file on every nftables reload is what keeps DNAT alive across reboots
    # and manual reloads when Firestore is unreachable. The file is created
    # empty on first install and is rewritten atomically on every sync.
    include "/etc/nftables.d/base-nat-elements.nft"

    chain prerouting {
        type nat hook prerouting priority dstnat;
        # Match-then-lookup: `@map` acts as a set over the map's keys so the
        # rule only fires for known ports, then `map @map` performs the
        # lookup and produces the target `ipv6_addr . inet_service` value.
        tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map
        udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map
        tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map
    }

    chain postrouting {
        type nat hook postrouting priority srcnat;
        # Global rule: always present main IPv6 to backend VMs for DNAT'ed flows.
        ct status dnat snat to 2a01:4f9:3070:3984::2
    }
}

table inet filter {
    # Fast-path: kernel flowtable for offloading established flows.
    # Once a connection is added to @ft, subsequent packets bypass the full
    # netfilter hook traversal (including NAT), cutting per-packet latency
    # significantly on long-lived RDP and SMB sessions. NAT transformations
    # are replayed in the fast path.
    flowtable ft {
        hook ingress priority filter;
        devices = { enp6s0, veth-host };
    }

    chain input {
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept
        tcp dport 22 accept
        # Required if nginx terminates TLS / handles HTTP redirects or ACME.
        tcp dport 80 accept
        tcp dport 443 accept
    }

    chain forward {
        type filter hook forward priority filter; policy drop;
        # Offload established flows to the flowtable fast path.
        ip protocol { tcp, udp } ct state established flow add @ft
        ip6 nexthdr { tcp, udp } ct state established flow add @ft
        ct state established,related accept
        # Allow any flow that was explicitly DNAT'ed in prerouting.
        iifname "enp6s0" ct status dnat accept
        iifname "veth-host" oifname "enp6s0" accept
    }
}
```

Enable:

```bash
# Required: the include directive above will fail if this file is missing.
mkdir -p /etc/nftables.d
: > /etc/nftables.d/base-nat-elements.nft

systemctl enable nftables
systemctl start nftables
```

### 7.1 nftables.service drop-in (boot race + auto-resync)

`nftables.service` is loaded very early in boot, before the WAN interface
is registered. Because the flowtable in `table inet filter` references
the WAN device by name, an early load fails with
`Could not process rule: No such file or directory` and **the entire
ruleset is rejected** — leaving the box with zero firewall/DNAT until a
later retry succeeds. Even when that retry succeeds, the maps come up
empty.

The drop-in below fixes both problems:

- `After=/Wants=network-online.target` — wait until interfaces are up
  before loading `/etc/nftables.conf`, so the flowtable resolves on the
  first try.
- `ExecStartPost=` — re-run sync whenever nftables starts. Combined with
  the include file (§7), this guarantees the maps are populated after
  every (re)load, regardless of the trigger (boot, manual restart,
  package upgrade hook).

```bash
mkdir -p /etc/systemd/system/nftables.service.d
cat >/etc/systemd/system/nftables.service.d/10-base-nat.conf <<'EOF'
[Unit]
After=network-online.target
Wants=network-online.target

[Service]
# Best-effort resync on every (re)start. The leading "-" means a sync
# failure (e.g. Firestore unreachable) does not fail nftables itself —
# the include file from §7 already provides the last-known-good state.
ExecStartPost=-/usr/bin/python3 /usr/local/sbin/sync-base-nat.py sync
EOF

systemctl daemon-reload
```

---

## 8. Persist NAT46/Jool Across Reboots

The following makes everything persistent except **Dynamic NAT66 Rules**:

- netns + veth addresses/routes
- host static routes for Jool
- Jool SIIT instance (`--netfilter`) + EAMT mapping (hooks attach in the netns automatically)
- kernel module load at boot

Install **`jool-dkms`** and **`jool-tools`** (see §5) before enabling this service.

Create a boot script:

```bash
cat >/usr/local/sbin/jool-nat46-setup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROUTER_IPV6="<ROUTER_IPV6>"
WAN_IF="enp6s0"

# 1) netns + veth
ip netns add jool 2>/dev/null || true
ip link add veth-host type veth peer name veth-ns 2>/dev/null || true
ip link set veth-ns netns jool 2>/dev/null || true

ip addr replace 10.0.0.1/24 dev veth-host
ip -6 addr replace fd00::1/64 dev veth-host
ip link set veth-host up

ip netns exec jool ip addr replace 10.0.0.2/24 dev veth-ns
ip netns exec jool ip addr replace 10.0.0.3/32 dev veth-ns
ip netns exec jool ip -6 addr replace fd00::2/64 dev veth-ns
ip netns exec jool ip link set veth-ns up
ip netns exec jool ip link set lo up

ip netns exec jool ip route replace default via 10.0.0.1
ip netns exec jool ip -6 route replace default via fd00::1

# 2) host routes toward Jool
ip route replace 10.0.0.3 via 10.0.0.2 dev veth-host
ip -6 route replace 64:ff9b:1::/96 via fd00::2 dev veth-host

# 3) jool module + SIIT instance (netfilter; no iptables rules in netns)
modprobe jool_siit

if ! ip netns exec jool jool_siit instance display | grep -q " br46 "; then
  ip netns exec jool jool_siit instance add br46 --netfilter
fi
ip netns exec jool jool_siit -i br46 global update pool6 64:ff9b:1::/96

# Ensure EAMT entry exists exactly once
if ip netns exec jool jool_siit -i br46 eamt display | grep -q "${ROUTER_IPV6}/128"; then
  :
else
  ip netns exec jool jool_siit -i br46 eamt add "${ROUTER_IPV6}/128" 10.0.0.3/32
fi

# 4) Consistent 1500 MTU on the forwarding path.
# Jool SIIT is stateless and adds no headers, so the veth pair must match
# the WAN MTU to avoid fragmentation in the NAT46 step.
ip link set veth-host mtu 1500
ip netns exec jool ip link set veth-ns mtu 1500

# 5) NIC offload tuning.
# GRO/GSO/TSO on the physical NIC reduces interrupt load. On the veth pair
# they add latency without throughput benefit, so disable them there.
ethtool -K "${WAN_IF}" gro on gso on tso on 2>/dev/null || true
ethtool -K veth-host gro off gso off tso off 2>/dev/null || true
ip netns exec jool ethtool -K veth-ns gro off gso off tso off 2>/dev/null || true

# 6) RPS (Receive Packet Steering) on veth-host.
# veth is single-queue, so packet processing is pinned to one CPU. RPS
# spreads softirq load across all cores, removing a bottleneck for the
# IPv4 RDP/SMB path that crosses the veth pair.
CPU_COUNT=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
RPS_MASK=$(printf '%x' $(( (1 << CPU_COUNT) - 1 )))
for q in /sys/class/net/veth-host/queues/rx-*; do
  [ -d "$q" ] || continue
  echo "$RPS_MASK" > "$q/rps_cpus" 2>/dev/null || true
  echo 4096 > "$q/rps_flow_cnt" 2>/dev/null || true
done
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || true
EOF

chmod +x /usr/local/sbin/jool-nat46-setup.sh
```

Load Jool module on boot:

```bash
echo jool_siit >/etc/modules-load.d/jool-siit.conf
```

Create systemd unit:

```bash
cat >/etc/systemd/system/jool-nat46.service <<'EOF'
[Unit]
Description=Setup Jool NAT46 netns topology
Wants=network-online.target
After=network-online.target nftables.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/jool-nat46-setup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now jool-nat46.service
```

Verify after reboot:

```bash
ip netns list
ip route | grep 10.0.0.3
ip -6 route | grep 64:ff9b:1::/96
jool_siit --version
ip netns exec jool jool_siit instance display
```

Confirm `jool_siit instance display` lists `br46` with **Framework** `netfilter` inside the `jool` netns (namespace column will show an opaque id).

> Dynamic NAT66 rules are intentionally not persisted in this guide.

---

## 9. Dynamic NAT66 Rules

Dynamic DNAT targets live as **map elements** (see §7 for the map
declarations). `sync-base-nat.py` only touches elements; the maps and the
rules that reference them stay in `/etc/nftables.conf`.

### Add / replace a single element

```bash
PORT=20201
TARGET="2a01:4f9:3100:4bdb:43a9:a22e:ac8f:87c4"

# RDP TCP
nft add element ip6 nat rdp_tcp_map "{ $PORT : $TARGET . 3389 }"
# RDP UDP (only if INCLUDE_UDP_RDP=1)
nft add element ip6 nat rdp_udp_map "{ $PORT : $TARGET . 3389 }"
# SMB TCP (port 10000+VMID)
nft add element ip6 nat smb_tcp_map "{ 10201 : $TARGET . 445 }"
```

SNAT-to-main-IPv6 and forward acceptance are handled globally in section 7.

### Remove a single element

```bash
nft delete element ip6 nat rdp_tcp_map "{ 20201 }"
nft delete element ip6 nat rdp_udp_map "{ 20201 }"
nft delete element ip6 nat smb_tcp_map "{ 10201 }"
```

### Inspect current state

```bash
nft list map ip6 nat rdp_tcp_map
nft list map ip6 nat rdp_udp_map
nft list map ip6 nat smb_tcp_map
```

### Full reconcile from Firestore

```bash
/usr/local/sbin/sync-base-nat.py sync
```

The sync script does an **atomic** `flush map` + `add element` transaction
per map, so there is no visible gap where a published port stops working.
On first run after upgrading from the rule-based version, any legacy
per-rule managed entries in `ip6 nat prerouting` are removed automatically.

---

## 10. Key Properties

- Jool translates **ALL IPv4 traffic**
- nftables decides allowed ports
- Dynamic rule changes **DO NOT DROP EXISTING CONNECTIONS**
- Stateless design → highly scalable

---

## 11. Performance Notes

### Flowtable fast path

Section 7 installs a flowtable (`inet filter ft`) and adds established flows
to it from the forward chain. Once a TCP/UDP connection is established, the
kernel processes subsequent packets in the software fast path, bypassing the
full netfilter hook traversal. This is the single biggest win for RDP and
SMB latency through the NAT path. Verify offload is active:

```bash
nft list table inet filter
conntrack -L 2>/dev/null | grep -c OFFLOAD
```

Dynamic rule changes in `ip6 nat prerouting` do not affect already-offloaded
flows; only new connections re-enter the slow path.

### Map-based dynamic DNAT

`sync-base-nat.py` populates three nftables maps in `ip6 nat` (see §7):

- `rdp_tcp_map` — external port `20000+VMID` → VM IPv6 `.` 3389
- `rdp_udp_map` — same, UDP (only when `INCLUDE_UDP_RDP=1`)
- `smb_tcp_map` — external port `10000+VMID` → VM IPv6 `.` 445

Three static rules in `prerouting` turn new-connection lookup into an O(1)
hash lookup regardless of VM count, and the whole reconcile is applied as
a single atomic `nft -f -` transaction per sync run. Flowtable offload
(above) already eliminates per-packet cost for established flows, so this
primarily tightens new-connection setup — which is also what matters for
RDP reconnect and initial SMB handshakes.

Inspect element counts:

```bash
nft list map ip6 nat rdp_tcp_map | grep -c ':'
nft list map ip6 nat smb_tcp_map | grep -c ':'
```

---

## Done
