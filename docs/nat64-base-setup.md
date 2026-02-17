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

## 8. Proxmox VM firewall (cluster.fw) and NAT64

NAT64 traffic reaches VMs with **source 64:ff9b::/96**. The cluster firewall must allow that source for the same services as internal IPv4 (RDP, SSH, Samba).

The `first_boot.sh` template for `cluster.fw` includes:

- **IPset** `nat64-clients`: `64:ff9b::/96`
- In **group vm-default**: `IN SSH/RDP/SMB ACCEPT -source +dc/nat64-clients`

So VMs using the default group accept RDP, SSH and Samba from both internal IPv4 and from NAT64 (64:ff9b::/96). Groups like vm-no-rdp / vm-no-ssh / vm-no-samba still apply (e.g. RDP is dropped if vm-no-rdp is set). vm-no-internet continues to allow only RDP/SSH/SMB; NAT64 traffic for those services is allowed.

**On an existing cluster** (already running before this change), add to `/etc/pve/firewall/cluster.fw`:

1. Under `[IPSET ...]` add a new ipset:
   ```text
   [IPSET nat64-clients]
   64:ff9b::/96
   ```
2. In `[group vm-default]` add after the existing IN SSH/RDP/SMB lines:
   ```text
   IN SSH(ACCEPT) -source +dc/nat64-clients -log nolog
   IN RDP(ACCEPT) -source +dc/nat64-clients -log nolog
   IN SMB(ACCEPT) -source +dc/nat64-clients -log nolog
   ```
   Reload: `pve-firewall restart` (or restart the cluster firewall from the UI).

---

## 9. Guest firewall (64:ff9b::/96)

NAT64 traffic appears at the VM with **source 64:ff9b::/96** (not BASE's IP). The Proxmox VM firewall (above) allows it at the hypervisor; the **guest OS** may still need to allow that source for RDP (TCP 3389), SSH (22), and Samba (445).

- **Windows:** Inbound rule for Remote Desktop (and File and Printer Sharing if needed), Remote IP = `64:ff9b::/96` (or the equivalent IPv6 range in the UI).
- **Linux:** Allow TCP 22 (and 445 if Samba) from `64:ff9b::/96` in your firewall (nft/iptables/ufw).

Security note: 64:ff9b::/96 is the set of IPv4 clients translated by NAT64. Restrict who can reach BASE's NAT64 ports (e.g. by source IP or VPN) if you want to limit who can connect.

---

## 10. BIB entries (per-VM)

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

## 11. ip6tables FORWARD on BASE

When adding a BIB entry, BASE must allow forwarding to the VM's IPv6. `sync-dnat.py` adds these rules when it adds BIB entries. Manual example:

```bash
ip6tables -A FORWARD -p tcp -d VM_IPv6 -j ACCEPT
ip6tables -A FORWARD -p tcp -s VM_IPv6 -j ACCEPT
```

---

## 12. nodes.conf on BASE

`/etc/sync-dnat/nodes.conf` on BASE lists nodes (hostname and public IPv4), one per line. Used by sync-dnat and other tooling. Example:

```text
0000001-BASE 37.27.135.250
0000002-AX162-R 65.108.9.14
```

BASE's line is optional (BASE does not forward to itself). The NAT64 routes (VM subnet → node fd00) are in `nat64-routes.conf` and applied by `apply-nat64-routes.sh`.

---

## 13. Optional: rp_filter

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
| Cluster VM fw   | cluster.fw| ipset nat64-clients (64:ff9b::/96); vm-default allows SSH/RDP/SMB from it   |
| Guest firewall | Each VM   | Allow source 64:ff9b::/96 for RDP (3389), SSH (22), Samba (445) if needed   |
| BIB + ip6tables | BASE      | Automatic via sync-dnat.py on VM start/stop                                |

---

## 14. After restart

### Node (non-BASE) restart

- **64:ff9b::/96 route** is in `/etc/network/interfaces` (post-up on the fd00 interface), so it is re-applied when the interface comes up. No action needed.
- **Firewall** (cluster.fw) is on disk; `pve-firewall` loads it at boot. No action needed.
- **NAT64 connectivity** for VMs on that node continues to work after the node reboots.

### BASE restart

- **Routes to VMs** (nat64-routes.conf): Applied at boot by `/etc/network/if-up.d/apply-nat64-routes` when the fd00:4000::1 interface comes up. No action needed.
- **Jool** (kernel module) is loaded from `/etc/modules`. The **instance** and **pool4** are not persistent across reboot; they are re-created automatically by the NAT64 boot script (see below) so that NAT64 works again without manual steps.
- **BIB entries and ip6tables FORWARD** are not persistent. They are repopulated at boot by running `sync-dnat.py update_base restore`, which reads all servers with `ipv6` set from Firestore and adds the corresponding BIB and ip6tables rules on BASE. This is done automatically by the same NAT64 boot script.

If you have not deployed the NAT64 boot script (see next section), then after a BASE reboot you must either:

1. Manually run: `jool instance add "default" --netfilter --pool6 64:ff9b::/96`, then add pool4 (step 4), then run `sync-dnat.py update_base restore` (with Firebase credentials on BASE), or  
2. Restart each VM so that the Firestore trigger runs and re-adds BIB for that VM (only fixes BIB for VMs you restart; Jool instance and pool4 still need to be re-created manually).

---

## 15. NAT64 boot script on BASE (optional but recommended)

To make BASE fully recover after reboot without manual steps, install the NAT64 boot script. It is installed automatically by `first_boot.sh` on the BASE node.

- **Script:** `/usr/local/bin/nat64-boot-restore.sh`  
  - Loads the Jool module, ensures the `default` instance and pool4 exist (idempotent), then runs `sync-dnat.py update_base restore` to repopulate BIB and ip6tables from Firestore.
- **Systemd:** `nat64-boot-restore.service` runs once after `network-online.target`, so routes and interfaces are up before Jool and BIB are restored.

After a BASE reboot, NAT64 (and direct IPv4 RDP/Samba to BASE ports) will work again once this service has run. Ensure `/etc/firebase-credentials.json` exists on BASE so that `update_base restore` can read from Firestore.

---

## 16. Troubleshooting RDP error 0x204 (NAT64)

Error **0x204** usually means the RDP client cannot reach the host over the network (firewall, port, or path). Work through these checks on **BASE**, then the **node** where the VM runs, then the **guest**.

### On BASE (run as root)

1. **Port and Jool**  
   Replace `VMID` with the VM’s ID (e.g. 201 → RDP port 20201).

   ```bash
   VMID=201
   BASE_IP=$(ip -4 route get 8.8.8.8 | grep -oP 'src \K\S+')
   RDP_PORT=$((20000 + VMID))
   echo "RDP port for VM $VMID: $RDP_PORT (BASE $BASE_IP)"
   jool instance display
   jool pool4 display
   jool bib display --tcp | grep -E "20201|$RDP_PORT"
   ```

   - If `jool instance display` or `jool pool4 display` fails or is empty, (re)create instance and pool4 (steps 3–4).
   - There must be a BIB entry mapping `BASE_IP#RDP_PORT` → `VM_IPv6#3389` (or #22 for Linux). If missing, run `sync-dnat.py update_base restore` or add it manually.

2. **Route to the VM’s IPv6**  
   Get the VM’s IPv6 from Firestore or the node, then:

   ```bash
   ip -6 route get "VM_IPv6"
   ```

   The route should go via the correct node’s fd00:4000:: address. If there is no route, check `/etc/sync-dnat/nat64-routes.conf` and run `/usr/local/bin/apply-nat64-routes.sh`.

3. **ip6tables FORWARD**  
   BASE must allow forwarding to the VM’s IPv6:

   ```bash
   ip6tables -L FORWARD -n -v | grep -E "VM_IPv6|nat64"
   ```

   If there is no ACCEPT for that VM’s IPv6, run `sync-dnat.py update_base restore` or re-add the BIB (which adds the FORWARD rules).

4. **BASE firewall**  
   Proxmox must allow TCP to the RDP port range:

   ```bash
   pve-firewall status
   ```

   Ensure there is an IN ACCEPT rule for TCP 20000–20999 (and 10000–10999 for Samba) on the BASE node or datacenter.

### On the node where the VM runs

5. **Return route for NAT64**  
   Replies from the VM (dest 64:ff9b::/96) must go to BASE:

   ```bash
   ip -6 route | grep 64:ff9b
   ```

   You should see `64:ff9b::/96 via fd00:4000::1` (or similar). If not, check `/etc/network/interfaces` for the fd00 interface’s `post-up` and run `ifreload -a` or reboot the node.

### On the VM (Proxmox VM firewall and guest)

6. **Proxmox VM firewall**  
   The VM’s firewall group must allow RDP from 64:ff9b::/96. On any node:

   ```bash
   cat /etc/pve/firewall/cluster.fw | grep -A2 nat64-clients
   cat /etc/pve/firewall/VMID.fw   # VMID = e.g. 201
   ```

   The VM must use a group that includes `IN RDP(ACCEPT) -source +dc/nat64-clients` (e.g. vm-default), and must **not** use vm-no-rdp (which drops RDP).

7. **Windows guest firewall**  
   The VM sees RDP from **64:ff9b::/96**, not from BASE’s IP. In Windows Firewall, the Remote Desktop rule must allow inbound TCP 3389 from that range:

   - Windows Defender Firewall → Inbound rules → Remote Desktop (TCP-In).
   - Edit the rule → Scope → Remote IP: add an IPv6 range `64:ff9b::/96` (or allow “Any” for testing).

   If the rule only allows IPv4 or a different range, connections from NAT64 will be dropped and you can get 0x204.

### Quick connectivity test from BASE

From BASE, after replacing `VM_IPv6` with the VM’s real IPv6:

```bash
# From BASE: can we reach the VM’s RDP port over IPv6?
curl -v --connect-timeout 5 "[VM_IPv6]:3389"
```

You should get a timeout or RDP garbage (not “Connection refused” from BASE). If BASE gets “No route to host” or “Network unreachable”, the problem is routing or FORWARD on BASE/node.
