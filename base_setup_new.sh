#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# CONFIG - EDIT THESE IF NEEDED
###############################################################################

WAN_IF="enp1s0"

# Public IPv4s
MAIN_IPV4="46.62.188.207"
MAIN_IPV4_PREFIX="25"
MAIN_IPV4_GW="46.62.188.129"
FAILOVER_IPV4="77.42.49.79"

# Host public/service IPv6s
HOST_V6_CANONICAL="2a01:4f9:3090:2488::2"
HOST_V6_ALIAS="2a01:4f9:3090:2488::3"
HOST_V6_FAILOVER="2a01:4f9:fff1:5f::2"
HOST_V6_SNAT="2a01:4f9:3090:2488::1201"

# Test VM
VM201_V6="2a01:4f9:3100:4bdb:43a9:a22e:ac8f:87c4"

# Namespace/veth
NS="v4edge"
HOST_VETH="v4host0"
NS_VETH="v4edge0"
MACVLAN_IF="v4pub0"

HOST_LINK_V6="fd00:4000::1/64"
HOST_LINK_V6_IP="fd00:4000::1"
NS_LINK_V6="fd00:4000::2/64"
NS_LINK_V6_IP="fd00:4000::2"

# Jool
JOOL_INSTANCE="edge"

###############################################################################
# HELPERS
###############################################################################

have_addr() {
    local dev="$1"
    local addr="$2"
    ip addr show dev "$dev" | grep -Fq "$addr"
}

have_ns_addr() {
    local ns="$1"
    local dev="$2"
    local addr="$3"
    ip netns exec "$ns" ip addr show dev "$dev" | grep -Fq "$addr"
}

have_route6_ns() {
    local ns="$1"
    local route="$2"
    ip netns exec "$ns" ip -6 route show | grep -Fq "$route"
}

have_netns() {
    ip netns list | awk '{print $1}' | grep -Fxq "$1"
}

have_link() {
    ip link show "$1" >/dev/null 2>&1
}

have_ns_link() {
    ip netns exec "$1" ip link show "$2" >/dev/null 2>&1
}

jool_instance_exists() {
    ip netns exec "$NS" jool_siit instance display 2>/dev/null | grep -Fq "$JOOL_INSTANCE"
}

###############################################################################
# PRECHECK
###############################################################################

echo "===> WARNING: this script moves ${MAIN_IPV4}/${MAIN_IPV4_PREFIX} into namespace ${NS}."
echo "===> Run from Hetzner console / KVM, not from SSH over ${MAIN_IPV4}."
sleep 2

###############################################################################
# PACKAGES
###############################################################################

echo "===> Installing packages"
apt update
DEBIAN_FRONTEND=noninteractive apt install -y \
    linux-headers-amd64 \
    jool-dkms \
    jool-tools \
    nftables \
    iproute2 \
    tcpdump

###############################################################################
# SYSCTL
###############################################################################

echo "===> Writing sysctl config"
cat >/etc/sysctl.d/99-neuravps-router.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
EOF

echo "===> Applying sysctl"
sysctl --system

###############################################################################
# HOST IPV6 ADDRESSES
###############################################################################

echo "===> Ensuring host IPv6 service addresses exist"
have_addr "$WAN_IF" "${HOST_V6_ALIAS}/64" || ip addr add "${HOST_V6_ALIAS}/64" dev "$WAN_IF"
have_addr "$WAN_IF" "${HOST_V6_SNAT}/64" || ip addr add "${HOST_V6_SNAT}/64" dev "$WAN_IF"

###############################################################################
# NFTABLES CONFIG (ONLY THIS VM)
###############################################################################

echo "===> Writing /etc/nftables.conf"
cat >/etc/nftables.conf <<EOF
#!/usr/sbin/nft -f

flush ruleset

define HOST_V6      = ${HOST_V6_CANONICAL}
define ALIAS_V6     = ${HOST_V6_ALIAS}
define FAILOVER_V6  = ${HOST_V6_FAILOVER}
define SNAT_V6      = ${HOST_V6_SNAT}
define VM201_V6     = ${VM201_V6}

table inet neuravps_filter {
    set published_tcp_ports {
        type inet_service
        elements = { 22, 10201, 20201 }
    }

    set published_udp_ports {
        type inet_service
        elements = { 20201 }
    }

    chain input {
        type filter hook input priority filter; policy drop;

        iifname "lo" accept
        ct state established,related accept
        ct state invalid drop

        ip protocol icmp accept
        ip6 nexthdr ipv6-icmp accept

        tcp dport @published_tcp_ports counter accept
        udp dport @published_udp_ports counter accept
    }

    chain forward {
        type filter hook forward priority filter; policy drop;

        ct state established,related accept
        ct state invalid drop

        ip6 daddr \$VM201_V6 tcp dport { 3389, 445 } counter accept
        ip6 daddr \$VM201_V6 udp dport 3389 counter accept
    }

    chain output {
        type filter hook output priority filter; policy accept;
    }
}

table ip6 neuravps_nat6 {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;

        # Canonical host service IPv6
        ip6 daddr \$HOST_V6 tcp dport 20201 counter dnat to [${VM201_V6}]:3389
        ip6 daddr \$HOST_V6 udp dport 20201 counter dnat to [${VM201_V6}]:3389
        ip6 daddr \$HOST_V6 tcp dport 10201 counter dnat to [${VM201_V6}]:445

        # Alias IPv6 (used by failover IPv4 translation)
        ip6 daddr \$ALIAS_V6 tcp dport 20201 counter dnat to [${VM201_V6}]:3389
        ip6 daddr \$ALIAS_V6 udp dport 20201 counter dnat to [${VM201_V6}]:3389
        ip6 daddr \$ALIAS_V6 tcp dport 10201 counter dnat to [${VM201_V6}]:445

        # Native failover IPv6 path
        ip6 daddr \$FAILOVER_V6 tcp dport 20201 counter dnat to [${VM201_V6}]:3389
        ip6 daddr \$FAILOVER_V6 udp dport 20201 counter dnat to [${VM201_V6}]:3389
        ip6 daddr \$FAILOVER_V6 tcp dport 10201 counter dnat to [${VM201_V6}]:445
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;

        ip6 daddr \$VM201_V6 tcp dport 3389 counter snat to \$SNAT_V6
        ip6 daddr \$VM201_V6 udp dport 3389 counter snat to \$SNAT_V6
        ip6 daddr \$VM201_V6 tcp dport 445 counter snat to \$SNAT_V6
    }
}
EOF

echo "===> Loading nftables"
nft -f /etc/nftables.conf
systemctl enable nftables
systemctl restart nftables

###############################################################################
# NAMESPACE + VETH
###############################################################################

echo "===> Creating namespace ${NS}"
if ! have_netns "$NS"; then
    ip netns add "$NS"
fi

if ! have_link "$HOST_VETH"; then
    ip link add "$HOST_VETH" type veth peer name "$NS_VETH"
fi

if have_link "$NS_VETH"; then
    ip link set "$NS_VETH" netns "$NS"
fi

have_addr "$HOST_VETH" "$HOST_LINK_V6" || ip addr add "$HOST_LINK_V6" dev "$HOST_VETH"
ip link set "$HOST_VETH" up

have_ns_addr "$NS" "$NS_VETH" "$NS_LINK_V6" || ip netns exec "$NS" ip addr add "$NS_LINK_V6" dev "$NS_VETH"
ip netns exec "$NS" ip link set "$NS_VETH" up
ip netns exec "$NS" ip link set lo up

###############################################################################
# PUBLIC MACVLAN IN NAMESPACE
###############################################################################

echo "===> Creating macvlan ${MACVLAN_IF}"
if ! have_link "$MACVLAN_IF"; then
    ip link add "$MACVLAN_IF" link "$WAN_IF" type macvlan mode bridge
fi

if have_link "$MACVLAN_IF"; then
    ip link set "$MACVLAN_IF" netns "$NS"
fi

ip netns exec "$NS" ip link set "$MACVLAN_IF" up

if ! have_ns_addr "$NS" "$MACVLAN_IF" "${MAIN_IPV4}/${MAIN_IPV4_PREFIX}"; then
    ip netns exec "$NS" ip addr add "${MAIN_IPV4}/${MAIN_IPV4_PREFIX}" dev "$MACVLAN_IF"
fi

if ! have_ns_addr "$NS" "$MACVLAN_IF" "${FAILOVER_IPV4}/32"; then
    ip netns exec "$NS" ip addr add "${FAILOVER_IPV4}/32" dev "$MACVLAN_IF"
fi

# Default route inside namespace
if ! ip netns exec "$NS" ip route show default | grep -Fq "via ${MAIN_IPV4_GW} dev ${MACVLAN_IF}"; then
    ip netns exec "$NS" ip route replace default via "${MAIN_IPV4_GW}" dev "$MACVLAN_IF"
fi

# Remove main IPv4 from host if still present
if have_addr "$WAN_IF" "${MAIN_IPV4}/${MAIN_IPV4_PREFIX}"; then
    ip addr del "${MAIN_IPV4}/${MAIN_IPV4_PREFIX}" dev "$WAN_IF"
fi

###############################################################################
# FORWARDING INSIDE NAMESPACE
###############################################################################

echo "===> Enabling forwarding in namespace"
ip netns exec "$NS" sysctl -w net.ipv4.conf.all.forwarding=1
ip netns exec "$NS" sysctl -w net.ipv6.conf.all.forwarding=1

###############################################################################
# JOOL
###############################################################################

echo "===> Loading Jool kernel module"
modprobe jool_siit

echo "===> Creating Jool SIIT instance (${JOOL_INSTANCE}) in namespace ${NS}"
if ! jool_instance_exists; then
    ip netns exec "$NS" jool_siit instance add "$JOOL_INSTANCE" --netfilter
fi

echo "===> Installing EAM mappings"
# main IPv4 -> canonical host service IPv6
if ! ip netns exec "$NS" jool_siit -i "$JOOL_INSTANCE" eamt display | grep -Fq "${MAIN_IPV4}/32"; then
    ip netns exec "$NS" jool_siit -i "$JOOL_INSTANCE" eamt add "${MAIN_IPV4}" "${HOST_V6_CANONICAL}"
fi

# failover IPv4 -> alias host service IPv6
if ! ip netns exec "$NS" jool_siit -i "$JOOL_INSTANCE" eamt display | grep -Fq "${FAILOVER_IPV4}/32"; then
    ip netns exec "$NS" jool_siit -i "$JOOL_INSTANCE" eamt add "${FAILOVER_IPV4}" "${HOST_V6_ALIAS}"
fi

###############################################################################
# NAMESPACE IPV6 ROUTES BACK TO HOST
###############################################################################

echo "===> Adding namespace IPv6 routes towards host over veth"
have_route6_ns "$NS" "${HOST_V6_CANONICAL} via ${HOST_LINK_V6_IP}" || \
    ip netns exec "$NS" ip -6 route add "${HOST_V6_CANONICAL}/128" via "${HOST_LINK_V6_IP}" dev "$NS_VETH"

have_route6_ns "$NS" "${HOST_V6_ALIAS} via ${HOST_LINK_V6_IP}" || \
    ip netns exec "$NS" ip -6 route add "${HOST_V6_ALIAS}/128" via "${HOST_LINK_V6_IP}" dev "$NS_VETH"

###############################################################################
# SHOW RESULT
###############################################################################

echo
echo "===== HOST ADDRESSES ====="
ip a show dev "$WAN_IF"
ip a show dev "$HOST_VETH"

echo
echo "===== NAMESPACE ADDRESSES ====="
ip netns exec "$NS" ip a

echo
echo "===== NAMESPACE ROUTES ====="
ip netns exec "$NS" ip route
ip netns exec "$NS" ip -6 route

echo
echo "===== JOOL INSTANCE ====="
ip netns exec "$NS" jool_siit instance display
ip netns exec "$NS" jool_siit -i "$JOOL_INSTANCE" eamt display

echo
echo "===== NFTABLES ====="
nft list ruleset

cat <<'EOM'

Next validation commands:

# Host forwarding
sysctl net.ipv4.ip_forward
sysctl net.ipv6.conf.all.forwarding

# Namespace translator stats
ip netns exec v4edge jool_siit -i edge stats display --all

# Watch translated IPv6 arrive from namespace
tcpdump -ni v4host0 'ip6 and (port 20201 or port 10201)'

# Watch Jool activity in namespace
ip netns exec v4edge tcpdump -ni any 'host 46.62.188.207 or host 77.42.49.79 or port 20201 or port 10201'

# Check nft counters
nft list table ip6 neuravps_nat6
nft list table inet neuravps_filter

External tests:
- MAIN IPv4:
  nc -vz -w 3 46.62.188.207 20201
  nc -vz -w 3 46.62.188.207 10201

- FAILOVER IPv4:
  nc -vz -w 3 77.42.49.79 20201
  nc -vz -w 3 77.42.49.79 10201

- Native IPv6:
  nc -vz -w 3 2a01:4f9:3090:2488::2 20201
  nc -vz -w 3 2a01:4f9:fff1:5f::2 20201

EOM