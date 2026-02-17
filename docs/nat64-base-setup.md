# NAT64 BASE Setup (Debian 13 + Proxmox)

This document describes how to configure the BASE node for NAT64 so that IPv4 clients can reach VMs via BASE's public IP, with traffic translated to IPv6 and forwarded to VMs over the private VLAN. Prerequisites: BASE is installed with **raw Debian 13** and **Proxmox on top** (that process is not documented here).

---

## 1. Install Jool (NAT64)

```bash
apt update
apt install -y jool-dkms jool-tools
echo "jool" >> /etc/modules
modprobe jool
```

---

## 2. Enable IP forwarding

```bash
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1
```

Persist in `/etc/sysctl.d/99-forwarding.conf`:

```
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
```

---

## 3. Create Jool NAT64 instance

```bash
jool instance add "default" --netfilter --pool6 64:ff9b::/96
```

---

## 4. Add pool4 (for BIB / port forwarding)

BASE's public IPv4 and the RDP/Samba port ranges must be in pool4:

```bash
BASE_IP=$(ip -4 route get 8.8.8.8 | grep -oP 'src \K\S+')
jool pool4 add --tcp "$BASE_IP" 10000-10999
jool pool4 add --tcp "$BASE_IP" 20000-20999
jool pool4 add --udp "$BASE_IP" 10000-10999
jool pool4 add --udp "$BASE_IP" 20000-20999
```

---

## 5. Routes on BASE (VM subnets via nodes)

For NAT64 to reach VMs, BASE must route each node's VM IPv6 subnet via that node's fd00:4000:: address. These routes are added automatically when nodes run `first_boot.sh` (they register their subnet with BASE). BASE applies them at boot via `/usr/local/bin/apply-nat64-routes.sh` reading `/etc/sync-dnat/nat64-routes.conf`.

Format of `nat64-routes.conf`: one line per node, `VM_IPv6_PREFIX/64 NODE_FD00_IPV6`  
Example: `2a01:4f9:6a:44eb::/64 fd00:4000::2`

No manual route setup is needed if first_boot has run on each node.

---

## 6. Routes on each node (NAT64 return path)

Each **non-BASE** node must route the NAT64 prefix to BASE so that VM replies (dest 64:ff9b::/96) reach Jool:

```text
64:ff9b::/96 via fd00:4000::1
```

This is added automatically by `first_boot.sh` to the node's interface that has fd00:4000:: (e.g. eth0.4000), in `/etc/network/interfaces` (up/down lines).

---

## 7. Proxmox firewall on BASE

Allow incoming TCP to BASE's public IP for the NAT64 ports (RDP/SSH 20000–20999, Samba 10000–10999). Either:

- In Proxmox UI: Datacenter or node Firewall → add IN ACCEPT rules for TCP 20000–20999 and 10000–10999, or
- Temporarily: `pve-firewall stop` (not recommended in production).

---

## 8. VM firewall (64:ff9b::/96)

NAT64 traffic appears at the VM with **source 64:ff9b::/96** (not BASE's IP). You must allow this source for RDP (TCP 3389) and, if used, Samba (TCP 445).

- **Windows:** Inbound rule for Remote Desktop (and File and Printer Sharing if needed), Remote IP = `64:ff9b::/96` (or the equivalent IPv6 range in the UI).
- **Linux:** Allow TCP 22 (and 445 if Samba) from `64:ff9b::/96` in your firewall (nft/iptables/ufw).

Security note: 64:ff9b::/96 is the set of IPv4 clients translated by NAT64. Restrict who can reach BASE's NAT64 ports (e.g. by source IP or VPN) if you want to limit who can connect.

---

## 9. BIB entries (per-VM)

BIB entries (IPv4 port → VM IPv6:port) are managed automatically by `sync-dnat.py` when VMs start/stop. Manual examples:

```bash
BASE_IP=$(ip -4 route get 8.8.8.8 | grep -oP 'src \K\S+')
# RDP: BASE:20201 → VM_IPv6:3389
jool bib add "VM_IPv6#3389" "${BASE_IP}#20201" --tcp
# Samba: BASE:10201 → VM_IPv6:445
jool bib add "VM_IPv6#445" "${BASE_IP}#10201" --tcp
```

Remove on VM stop:

```bash
jool bib remove "${BASE_IP}#20201" --tcp
jool bib remove "${BASE_IP}#10201" --tcp
```

---

## 10. ip6tables FORWARD on BASE

When adding a BIB entry, BASE must allow forwarding to the VM's IPv6. `sync-dnat.py` adds these rules when it adds BIB entries. Manual example:

```bash
ip6tables -A FORWARD -p tcp -d VM_IPv6 -j ACCEPT
ip6tables -A FORWARD -p tcp -s VM_IPv6 -j ACCEPT
```

---

## 11. nodes.conf on BASE

`/etc/sync-dnat/nodes.conf` on BASE lists nodes (hostname and public IPv4), one per line. Used by sync-dnat and other tooling. Example:

```text
0000001-BASE 37.27.135.250
0000002-AX162-R 65.108.9.14
```

BASE's line is optional (BASE does not forward to itself). The NAT64 routes (VM subnet → node fd00) are in `nat64-routes.conf` and applied by `apply-nat64-routes.sh`.

---

## 12. Optional: rp_filter

If translated traffic is dropped, try relaxing reverse path filter on BASE:

```bash
sysctl -w net.ipv4.conf.all.rp_filter=0
sysctl -w net.ipv4.conf.<wan_interface>.rp_filter=0
```

Persist in sysctl.d if needed.

---

## Summary checklist

| Step            | Where     | Action                                                                     |
| --------------- | --------- | -------------------------------------------------------------------------- |
| Jool            | BASE      | Install jool-dkms, jool-tools; modprobe jool; instance add default         |
| Forwarding      | BASE      | ip_forward=1, ipv6 forwarding=1                                            |
| pool4           | BASE      | Add BASE public IP, ports 10000–10999 and 20000–20999 (tcp/udp)            |
| Routes to VMs   | BASE      | Via nat64-routes.conf + apply-nat64-routes.sh (first_boot registers nodes) |
| Return route    | Each node | 64:ff9b::/96 via fd00:4000::1 (first_boot adds to interfaces)              |
| BASE firewall   | BASE      | Allow TCP 20000–20999, 10000–10999 to BASE                                 |
| VM firewall     | Each VM   | Allow source 64:ff9b::/96 for RDP (3389) and Samba (445)                   |
| BIB + ip6tables | BASE      | Automatic via sync-dnat.py on VM start/stop                                |
