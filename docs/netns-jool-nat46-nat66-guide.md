# NAT46 + NAT66 Router (Jool + Netns + nftables)

## Overview

This setup implements:

- **NAT46 (IPv4 → IPv6)** using Jool (stateless SIIT)
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

## 5. Install Jool

See `scripts/build-jool-from-source.sh`

---

## 6. Configure Jool

```bash
ip netns exec jool jool_siit instance add br46 --netfilter
ip netns exec jool jool_siit -i br46 global update pool6 64:ff9b:1::/96

# Map router IPv6 <-> internal IPv4
ip netns exec jool jool_siit -i br46 eamt add <ROUTER_IPV6>/128 10.0.0.3/32
```

---

## 7. Hook Jool into Netfilter

```bash
ip netns exec jool iptables -t mangle -A PREROUTING -d 10.0.0.3 -j JOOL_SIIT --instance br46
ip netns exec jool ip6tables -t mangle -A PREROUTING -d 64:ff9b:1::/96 -j JOOL_SIIT --instance br46
```

---

## 8. nftables Rules

Create `/etc/nftables.conf`:

> Replace `enp6s0` with your real WAN interface (for example `enp1s0`).
> Replace `37.27.135.250` and `77.42.49.79` with your two public IPv4 addresses.
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
    chain prerouting {
        type nat hook prerouting priority dstnat;
        # dynamic rules here
    }

    chain postrouting {
        type nat hook postrouting priority srcnat;
        # Global rule: for DNAT'ed IPv6 flows, preserve ingress destination IP
        # (main/failover) as source on return traffic.
        ct status dnat snat to ct original daddr
    }
}

table inet filter {
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
        ct state established,related accept
        # Allow any flow that was explicitly DNAT'ed in prerouting.
        iifname "enp6s0" ct status dnat accept
        iifname "veth-host" oifname "enp6s0" accept
    }
}
```

Enable:

```bash
systemctl enable nftables
systemctl start nftables
```

---

## 9. Persist NAT46/Jool Across Reboots

The following makes everything persistent except **Dynamic NAT66 Rules**:

- netns + veth addresses/routes
- host static routes for Jool
- Jool SIIT instance + EAMT mapping
- Jool netfilter hooks inside netns
- kernel module load at boot

Create a boot script:

```bash
cat >/usr/local/sbin/jool-nat46-setup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROUTER_IPV6="<ROUTER_IPV6>"

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

# 3) jool module + instance
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

# 4) netfilter hooks inside jool netns
ip netns exec jool iptables -t mangle -C PREROUTING -d 10.0.0.3 -j JOOL_SIIT --instance br46 2>/dev/null || \
ip netns exec jool iptables -t mangle -A PREROUTING -d 10.0.0.3 -j JOOL_SIIT --instance br46

ip netns exec jool ip6tables -t mangle -C PREROUTING -d 64:ff9b:1::/96 -j JOOL_SIIT --instance br46 2>/dev/null || \
ip netns exec jool ip6tables -t mangle -A PREROUTING -d 64:ff9b:1::/96 -j JOOL_SIIT --instance br46
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
ip netns exec jool jool_siit instance display
ip netns exec jool iptables -t mangle -S PREROUTING
ip netns exec jool ip6tables -t mangle -S PREROUTING
```

> Dynamic NAT66 rules are intentionally not persisted in this guide.

---

## 10. Dynamic NAT66 Rules

### Add rule

```bash
PORT=20201
TARGET="2a01:4f9:3100:4bdb:43a9:a22e:ac8f:87c4"

# Applies to both main and failover IPv6 because no `ip6 daddr` filter is used.
nft add rule ip6 nat prerouting tcp dport $PORT dnat to $TARGET:3389
nft add rule ip6 nat prerouting udp dport $PORT dnat to $TARGET:3389
```

SNAT symmetry and forward acceptance are handled globally in section 8.

### Remove rule

```bash
# Dynamic management only touches ip6 nat prerouting handles.
nft -a list chain ip6 nat prerouting

# Delete matching DNAT handles for the published port.
nft delete rule ip6 nat prerouting handle <DNAT_TCP_HANDLE>
nft delete rule ip6 nat prerouting handle <DNAT_UDP_HANDLE>
```

---

## 11. Key Properties

- Jool translates **ALL IPv4 traffic**
- nftables decides allowed ports
- Dynamic rule changes **DO NOT DROP EXISTING CONNECTIONS**
- Stateless design → highly scalable

---

## Done
