#!/usr/bin/env bash
# BASE-only setup: NAT64 routes, apply script, boot restore.
# Run at end of first_boot.sh when hostname contains BASE.
set -euo pipefail

log() { echo "[$(date +'%F %T')] $*"; }

log "NAT64: Configuring BASE (nat64-routes.conf + apply script)"
mkdir -p /etc/sync-dnat
touch /etc/sync-dnat/nat64-routes.conf
cat > /usr/local/bin/apply-nat64-routes.sh <<'APPLYEOF'
#!/bin/bash
# Apply NAT64 routes from /etc/sync-dnat/nat64-routes.conf (format: VM_IPv6_PREFIX NODE_PUBLIC_IPV6)
CONF=/etc/sync-dnat/nat64-routes.conf
[[ -f "$CONF" ]] || exit 0
while read -r prefix gateway _; do
  [[ -z "$prefix" || "$prefix" =~ ^# ]] && continue
  ip -6 route add "$prefix" via "$gateway" 2>/dev/null || true
done < "$CONF"
APPLYEOF
chmod +x /usr/local/bin/apply-nat64-routes.sh
cat > /etc/network/if-up.d/apply-nat64-routes <<'IFUPEOF'
#!/bin/bash
# Run apply-nat64-routes when any global IPv6 interface comes up (main interface on BASE)
[[ "$ADDRFAM" = inet6 ]] || exit 0
ip -6 addr show dev "$IFACE" 2>/dev/null | grep -q 'scope global' || exit 0
/usr/local/bin/apply-nat64-routes.sh
IFUPEOF
chmod +x /etc/network/if-up.d/apply-nat64-routes
/usr/local/bin/apply-nat64-routes.sh

# NAT64 boot restore: re-create Jool instance + pool4 and repopulate BIB from Firestore after reboot
log "NAT64: Installing boot restore script and systemd service"
cat > /usr/local/bin/nat64-boot-restore.sh <<'NAT64BOOT'
#!/bin/bash
# Restore NAT64 routes, Jool instance, pool4, and BIB after BASE reboot. Idempotent.
LOG_TAG="nat64-boot-restore"
logger -t "$LOG_TAG" "Starting NAT64 boot restore"
# Apply routes to VM subnets (from nat64-routes.conf) so traffic can reach nodes
[[ -x /usr/local/bin/apply-nat64-routes.sh ]] && /usr/local/bin/apply-nat64-routes.sh
modprobe jool 2>/dev/null || true
jool instance add "default" --netfilter --pool6 64:ff9b::/96 2>/dev/null || true
BASE_IP=$(ip -4 route get 8.8.8.8 2>/dev/null | grep -oP 'src \K\S+' || true)
if [[ -n "$BASE_IP" ]]; then
  jool pool4 add --tcp "$BASE_IP" 10000-19999 2>/dev/null || true
  jool pool4 add --tcp "$BASE_IP" 20000-29999 2>/dev/null || true
  jool pool4 add --udp "$BASE_IP" 10000-19999 2>/dev/null || true
  jool pool4 add --udp "$BASE_IP" 20000-29999 2>/dev/null || true
fi
if [[ -x /var/lib/svz/snippets/sync-dnat.py ]]; then
  /var/lib/svz/snippets/sync-dnat.py update_base restore 2>/dev/null || true
  logger -t "$LOG_TAG" "NAT64 restore from Firestore completed"
else
  logger -t "$LOG_TAG" "sync-dnat.py not found, skipping BIB restore"
fi
logger -t "$LOG_TAG" "Finished"
NAT64BOOT
chmod +x /usr/local/bin/nat64-boot-restore.sh
cat > /etc/systemd/system/nat64-boot-restore.service <<'NAT64SVC'
[Unit]
Description=NAT64 Jool and BIB restore after boot (BASE)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/nat64-boot-restore.sh
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
NAT64SVC
systemctl daemon-reload
systemctl enable nat64-boot-restore.service
log "NAT64: nat64-boot-restore.service enabled (runs once after network is up)"

log "first_boot_base.sh finished"
