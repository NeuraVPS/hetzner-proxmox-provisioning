#!/usr/bin/env bash
# setup_netconsole_base.sh — install/repair the BASE-side netconsole freeze-
# capture endpoint. Idempotent; run on each base (b0/b1):
#
#   1. a dual-stack UDP-6666 collector -> /var/log/netconsole/<src-ip>.log
#   2. a Firestore-driven nft allowlist of every node's IPv6 /64 (nc_fleet_v6),
#      refreshed by a systemd timer, so the collector is reachable ONLY from our
#      own nodes' /64s, never the public internet
#   3. the firewall rule that gates the collector on that set (live + persisted)
#
# Node side (points netconsole at the region base's IPv6, over configfs, because
# the netconsole module-param path can't transmit IPv6 on this kernel):
# first_boot.sh + run_remotes/apply_freeze_mitigations.sh.
# See docs/INCIDENT_2026-07-08_node0000008_freeze.md.
set -uo pipefail

# --- 1) collector: dual-stack UDP 6666 ---------------------------------------
install -d /var/log/netconsole
cat > /usr/local/sbin/netconsole-collector.py <<'PY'
#!/usr/bin/env python3
import socket, os, time
LOGDIR="/var/log/netconsole"; os.makedirs(LOGDIR, exist_ok=True)
# Dual-stack: bind :: with V6ONLY=0 so we receive IPv6 (the fleet's netconsole
# transport) plus, during transition, v4-mapped IPv4. netconsole runs over IPv6
# (module-param can't transmit v6 on this kernel; the node helper uses configfs).
s=socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("::", 6666))
files={}
while True:
    try:
        data, addr = s.recvfrom(65535)
        ip = addr[0]
        if ip.startswith("::ffff:"): ip = ip[7:]
        f=files.get(ip)
        if f is None:
            f=open(os.path.join(LOGDIR, ip+".log"), "ab"); files[ip]=f
        f.write(data if data.endswith(b"\n") else data+b"\n"); f.flush()
    except Exception:
        time.sleep(0.5)
PY
chmod +x /usr/local/sbin/netconsole-collector.py
cat > /etc/systemd/system/netconsole-collector.service <<'SVC'
[Unit]
Description=NeuraVPS netconsole collector (fleet kernel console capture)
After=network.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/sbin/netconsole-collector.py
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
SVC

# --- 2) /64 allowlist refresh script + timer ---------------------------------
cat > /usr/local/sbin/netconsole-fleet-allow.py <<'PY'
#!/usr/bin/env python3
# Keep the nft set `inet filter nc_fleet_v6` equal to the /64 of every node's
# IPv6 (Firestore proxmox_nodes.ip), so the netconsole collector (UDP 6666) only
# accepts kernel-console traffic from our own nodes' /64 ranges.
import ipaddress
import subprocess
import sys


def log(m):
    print(m, file=sys.stderr)


try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception as e:  # noqa: BLE001
    log(f"firebase_admin unavailable: {e}")
    sys.exit(0)

try:
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate("/etc/firebase-credentials.json")
        )
    db = firestore.client()
    nets = set()
    for d in db.collection("proxmox_nodes").stream():
        ip = ((d.to_dict() or {}).get("ip") or "").strip()
        if not ip:
            continue
        try:
            net = ipaddress.ip_network(f"{ip}/64", strict=False)
        except ValueError:
            continue
        if net.version == 6:
            nets.add(str(net))
    nets = sorted(nets)
except Exception as e:  # noqa: BLE001
    log(f"Firestore query failed: {e}")
    sys.exit(1)

if not nets:
    log("no fleet /64s resolved; leaving set unchanged")
    sys.exit(1)

if "--dry-run" in sys.argv:
    print(f"nc_fleet_v6 would hold {len(nets)} /64s")
    sys.exit(0)

subprocess.run(
    ["nft", "add", "set", "inet", "filter", "nc_fleet_v6",
     "{ type ipv6_addr; flags interval; auto-merge; }"],
    capture_output=True, text=True,
)
script = (
    "flush set inet filter nc_fleet_v6\n"
    "add element inet filter nc_fleet_v6 { " + ", ".join(nets) + " }\n"
)
p = subprocess.run(["nft", "-f", "-"], input=script, text=True, capture_output=True)
if p.returncode != 0:
    log(f"nft update failed: {p.stderr.strip()}")
    sys.exit(1)
print(f"nc_fleet_v6: {len(nets)} /64s")
PY
chmod +x /usr/local/sbin/netconsole-fleet-allow.py
cat > /etc/systemd/system/netconsole-fleet-allow.service <<'SVC'
[Unit]
Description=Refresh nft nc_fleet_v6 (netconsole UDP 6666 /64 allowlist) from Firestore
After=network-online.target nftables.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/netconsole-fleet-allow.py
SVC
cat > /etc/systemd/system/netconsole-fleet-allow.timer <<'TMR'
[Unit]
Description=Periodically refresh the netconsole UDP 6666 /64 allowlist
[Timer]
OnBootSec=30s
OnUnitActiveSec=30min
Persistent=true
[Install]
WantedBy=timers.target
TMR

systemctl daemon-reload
systemctl enable --now netconsole-collector.service >/dev/null 2>&1 || true
systemctl restart netconsole-collector.service
systemctl enable --now netconsole-fleet-allow.timer >/dev/null 2>&1 || true

# --- 3) populate the set + live firewall -------------------------------------
/usr/local/sbin/netconsole-fleet-allow.py
nft list chain inet filter input 2>/dev/null | grep -q 'ip6 saddr @nc_fleet_v6' \
  || nft add rule inet filter input udp dport 6666 ip6 saddr @nc_fleet_v6 accept
# drop any legacy permissive catch-all (pre-allowlist)
PH=$(nft -a list chain inet filter input 2>/dev/null | awk '/udp dport 6666 accept +#/{print $NF; exit}')
[ -n "$PH" ] && nft delete rule inet filter input handle "$PH"

# --- 4) persist to /etc/nftables.conf (idempotent, syntax-validated) ---------
F=/etc/nftables.conf
if [ -f "$F" ]; then
  cp -a "$F" "${F}.bak-ncv6"
  grep -q 'nc_fleet_v6' "$F" \
    || sed -i 's/^table inet filter {/&\n    set nc_fleet_v6 { type ipv6_addr; flags interval; auto-merge; }/' "$F"
  if ! grep -q 'dport 6666 ip6 saddr @nc_fleet_v6' "$F"; then
    if grep -q 'udp dport 6666 accept' "$F"; then
      sed -i 's/udp dport 6666 accept/udp dport 6666 ip6 saddr @nc_fleet_v6 accept/' "$F"
    else
      sed -i 's/^\( *\)tcp dport 443 accept *$/\1tcp dport 443 accept\n\1udp dport 6666 ip6 saddr @nc_fleet_v6 accept/' "$F"
    fi
  fi
  if nft -c -f "$F" >/dev/null 2>&1; then echo "persistent: OK"; else echo "persistent: SYNTAX FAIL -> reverted"; cp -a "${F}.bak-ncv6" "$F"; fi
fi

echo "netconsole base setup: collector=$(systemctl is-active netconsole-collector.service) timer=$(systemctl is-active netconsole-fleet-allow.timer) set=$(nft list set inet filter nc_fleet_v6 2>/dev/null | tr ',' '\n' | grep -cE '/[0-9]+') rules=$(nft list chain inet filter input 2>/dev/null | grep -c 'ip6 saddr @nc_fleet_v6')"
