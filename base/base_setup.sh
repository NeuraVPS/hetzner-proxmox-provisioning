# BASE bootstrap for dynamic forwarding sync (Jool + static nftables already persistent)
#
# Assumptions:
# - Base topology is already installed and persistent:
#   - jool-nat46.service builds netns/Jool plumbing
#   - /etc/nftables.conf contains static/global rules from docs/netns-jool-nat46-nat66-guide.md
# - This setup only installs runtime sync services that:
#   - reconcile dynamic VM forwardings in `ip6 nat prerouting` from Firestore
#   - keep nginx proxmox_nodes map + firewall sync commands

# 0) Interfaces example — as built on the German b0 (2026-07-04).
# Per-base values: main IPs change; bind ALL failover VIPs with
# preferred_lft 0 (deprecated => never chosen as source) so a failover
# swing needs no config change on the base.

### Hetzner Online GmbH installimage

# source /etc/network/interfaces.d/*

# auto lo
# iface lo inet loopback
# iface lo inet6 loopback

# auto enp6s0
# iface enp6s0 inet static
#   address 188.40.153.120
#   netmask 255.255.255.128
#   gateway 188.40.153.1
#   up route add -net 188.40.153.0 netmask 255.255.255.128 gw 188.40.153.1 dev enp6s0
#   # failover FSN (sqx-fsn)
#   up ip addr add 94.130.3.118/32 dev enp6s0 preferred_lft 0
#   down ip addr del 94.130.3.118/32 dev enp6s0
#   # failover HEL (sqx-hel)
#   up ip addr add 77.42.49.79/32 dev enp6s0 preferred_lft 0
#   down ip addr del 77.42.49.79/32 dev enp6s0

# iface enp6s0 inet6 static
#   address 2a01:4f8:2b03:18a9::2
#   netmask 64
#   gateway fe80::1
#   # failover FSN v6
#   up ip addr add 2a01:4f8:fff2:95::2/64 dev enp6s0 preferred_lft 0
#   down ip addr del 2a01:4f8:fff2:95::2/64 dev enp6s0
#   # failover HEL v6
#   up ip addr add 2a01:4f9:fff1:5f::2/64 dev enp6s0 preferred_lft 0
#   down ip addr del 2a01:4f9:fff1:5f::2/64 dev enp6s0

# 1) Install runtime dependencies.
apt update && apt upgrade -y
apt install -y nftables nginx libnginx-mod-http-js python3 python3-pip curl ca-certificates certbot ethtool sshpass smbclient netcat-openbsd freerdp3-x11 xvfb imagemagick
# Debian 13: the dpkg-owned `requests` blocks the dependency upgrade
# (uninstall-no-record-file) — --ignore-installed installs the tree into
# /usr/local without touching dpkg files.
pip3 install --break-system-packages --ignore-installed firebase-admin

# 1b) sshd rate limits for fleet sweeps. node_health_check and run_remotes/*
# hop through this BASE with bursts of short SSH connections; the default
# MaxStartups 10:30:100 resets part of each burst (kex_exchange_identification).
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/50-neuravps-maxstartups.conf \
  -o /etc/ssh/sshd_config.d/50-neuravps-maxstartups.conf
sshd -t && systemctl reload ssh

# 2) Runtime configuration for sync-base-nat.py.
cat >/etc/default/base-nat <<'EOF'
# Host addresses (used by boot checks only).
# Per-base values — this example is the German b0 (2026-07-04).
MAIN_IPV4=188.40.153.120
MAIN_IPV6=2a01:4f8:2b03:18a9::2
FAILOVER_IPV4=94.130.3.118
FAILOVER_IPV6=2a01:4f8:fff2:95::2

# Port contract:
# SMB external port = SAMBA_PORT_BASE + proxmoxId -> VM 445 (TCP)
# RDP external port = RDP_PORT_BASE + proxmoxId -> VM 3389 (TCP/UDP)
# SSH external port = SSH_PORT_BASE + proxmoxId -> VM 22 (TCP)
SAMBA_PORT_BASE=10000
RDP_PORT_BASE=20000
SSH_PORT_BASE=30000
VMID_MAX=9999
INCLUDE_UDP_RDP=1

# Dynamic nft destination for managed forwarding entries.
NFT_DNAT_FAMILY=ip6
NFT_DNAT_TABLE=nat
NFT_DNAT_CHAIN=prerouting
NFT_MANAGED_RULE_COMMENT_PREFIX=sync-base-nat

# Boot behavior.
SYNC_PVE_NODES_ON_BOOT=auto
WAIT_FOR_IPS_SEC=120

# Firebase + local state.
FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json
STATE_FILE=/var/lib/base-nat/state.json
PVE_NODES_STATE_FILE=/var/lib/base-nat/pve_nodes.json
PVE_NGINX_MAP_FILE=/etc/nginx/conf.d/pve-proxy-backends.map.conf

# Storage Box source for cluster.fw (used by: sync-base-nat.py sync nodes sync-firewall).
FIREWALL_STORAGE_USER=u560363
FIREWALL_STORAGE_HOST=u560363.your-storagebox.de
FIREWALL_REMOTE_PATH=/home/firewall/cluster.fw
FIREWALL_SCP_PORT=23

# Named base aliases (name=ipv6; '=' because b0/b00/b1 are valid IPv6
# hextets, so a colon would be ambiguous). Keep identical on every BASE.
# b00 = old Helsinki base-0 (retiring), b0 = German base, b1 = Helsinki.
BASE_HOSTS=b0=2a01:4f8:2b03:18a9::2,b00=2a01:4f9:3090:2488::2,b1=2a01:4f9:3070:3984::2
EOF

# 3) Install Firebase credentials before starting service.
# install -m 600 firebase-credentials.json /etc/firebase-credentials.json

# 4) Install runtime scripts from this repository.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/sync-base-nat.py \
  -o /usr/local/sbin/sync-base-nat.py
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/base-nat-boot.sh \
  -o /usr/local/sbin/base-nat-boot.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/base-nat-boot.service \
  -o /etc/systemd/system/base-nat-boot.service
chmod +x /usr/local/sbin/base-nat-boot.sh /usr/local/sbin/sync-base-nat.py

# 4a) nftables include file for persisted DNAT map elements.
# /etc/nftables.conf has `include "/etc/nftables.d/base-nat-elements.nft"`
# inside `table ip6 nat` (see docs/netns-jool-nat46-nat66-guide.md §7).
# nftables fails to load if the include target is missing, so create an
# empty file before the first reload. sync-base-nat.py rewrites it
# atomically on every reconcile.
mkdir -p /etc/nftables.d
[ -f /etc/nftables.d/base-nat-elements.nft ] || : > /etc/nftables.d/base-nat-elements.nft

# 4b) veth-host.service: tiny early-boot oneshot that creates the
# `veth-host` netdev pair before nftables.service loads. The flowtable
# in /etc/nftables.conf references this device; loading the ruleset
# before it exists fails the entire load. See guide §7.1 + §8.0.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/veth-host-setup.sh \
  -o /usr/local/sbin/veth-host-setup.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/veth-host.service \
  -o /etc/systemd/system/veth-host.service
chmod +x /usr/local/sbin/veth-host-setup.sh

# 4c) nftables.service drop-in: order after veth-host.service so the
# flowtable can resolve `devices = { enp*, veth-host }` on first try,
# and re-run sync after every (re)start so map elements are always
# populated. See guide §7.1.
mkdir -p /etc/systemd/system/nftables.service.d
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/nftables-base-nat.conf \
  -o /etc/systemd/system/nftables.service.d/10-base-nat.conf

# 5) Enable boot-time dynamic sync.
# NOTE: /var/lib/base-nat MUST exist before (re)starting nftables — the
# drop-in's ReadWritePaths references it and the unit dies with
# status=226/NAMESPACE otherwise.
mkdir -p /var/lib/base-nat
systemctl daemon-reload
systemctl enable --now veth-host.service
systemctl restart nftables.service
systemctl enable --now base-nat-boot.service

# 5bis) Tuneles base<->nodo anclados a las VIPs de failover.
# Cada base levanta DOS tuneles por nodo, uno por VIP, los dos siempre en pie.
# Solo lleva trafico el de la VIP que esta base posee de verdad; el otro esta de
# reserva y se activa SOLO cuando la VIP se mueve, sin reconfigurar nada. Un
# unico juego NO vale: al moverse la VIP los paquetes llegan dirigidos a una VIP
# para la que no hay tunel con ese par (local, remoto) y se caen.
# Ver docs/egress-failover-e-ipv6-estable.md §15 y §17.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/neuravps-base-tunnels.sh \
  -o /usr/local/sbin/neuravps-base-tunnels.sh
chmod 755 /usr/local/sbin/neuravps-base-tunnels.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/neuravps-base-tunnels.service \
  -o /etc/systemd/system/neuravps-base-tunnels.service
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/zz-neuravps-rpfilter.conf \
  -o /etc/sysctl.d/zz-neuravps-rpfilter.conf
sysctl -p /etc/sysctl.d/zz-neuravps-rpfilter.conf

# HOME_REGION = la region cuya VIP posee esta base en operacion normal.
# ⚠️ Es lo unico que distingue una base de la otra aqui: fsn en b0, hel en b1.
mkdir -p /etc/neuravps
cat > /etc/default/neuravps-base-tunnels <<'EOF'
HOME_REGION=fsn
TRANSIT_BASE=2a01:4f9:c01f:e:ffff::
VIP_FSN=2a01:4f8:fff2:95::2
VIP_HEL=2a01:4f9:fff1:5f::2
NODES_FILE=/etc/neuravps/tunnel-nodes.conf
EOF
# La tabla de nodos NO se siembra aqui: la genera sync-base-nat.py desde
# Firestore en cada `sync nodes`, y relanza esta unidad cuando cambia. En el
# primer arranque el script no encuentra el fichero, sale limpio sin hacer nada,
# y base-nat-boot lo crea acto seguido con la flota entera. Ver
# snippets/tunnel-nodes.conf.example para el formato.

# TUNNEL_IFACE_PREFIX le dice a sync-base-nat.py por que tunel colgar las rutas
# por VM: tun-f en la base de Falkenstein, tun-h en la de Helsinki.
grep -q '^TUNNEL_IFACE_PREFIX=' /etc/default/base-nat \
  || echo 'TUNNEL_IFACE_PREFIX=tun-f' >> /etc/default/base-nat

# Antes que base-nat-boot: sus rutas por VM apuntan a estas interfaces.
systemctl daemon-reload
systemctl enable --now neuravps-base-tunnels.service

# 6) Validation.
systemctl status --no-pager jool-nat46.service
systemctl status --no-pager base-nat-boot.service
python3 /usr/local/sbin/sync-base-nat.py sync
nft -a list chain ip6 nat prerouting

# Manual sync examples (VM forwardings):
# /usr/local/sbin/sync-base-nat.py sync
# /usr/local/sbin/sync-base-nat.py sync 400
# /usr/local/sbin/sync-base-nat.py sync 400 2001:db8::1
# /usr/local/sbin/sync-base-nat.py sync 400 del

# Manual sync examples (PVE nodes map + firewall):
# /usr/local/sbin/sync-base-nat.py sync nodes
# /usr/local/sbin/sync-base-nat.py sync nodes add 0000009-EX44 2a01:4f9:...
# /usr/local/sbin/sync-base-nat.py sync nodes del 0000009-EX44
# /usr/local/sbin/sync-base-nat.py sync nodes sync-firewall

# =============================================================================
# BASE: PVE proxy (*.pve.neuravps.com) + set-ticket + Firestore proxmox_nodes
# =============================================================================
# Node firewalls must allow BASE -> Proxmox :8006.
# Backends are built from Firestore proxmox_nodes (document ID = server ID, field ip = public IPv6).

mkdir -p /var/www/letsencrypt /etc/nginx/njs /opt/pve-set-ticket

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-proxy-map.conf \
  -o /etc/nginx/conf.d/pve-proxy-map.conf
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-proxy-backends.map.conf.example \
  -o /etc/nginx/conf.d/pve-proxy-backends.map.conf
# Host desconocido -> neuravps.com. SIN esto nginx usa el PRIMER bloque como
# predeterminado, y cualquiera que entrase por la IP de la base --la que los
# invitados ven como su salida-- se encontraba el navegador de archivos.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/nginx-00-catchall.conf \
  -o /etc/nginx/sites-available/00-catchall.conf
ln -sf /etc/nginx/sites-available/00-catchall.conf /etc/nginx/sites-enabled/00-catchall.conf

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/neuravps-redirects.conf \
  -o /etc/nginx/sites-available/neuravps-redirects.conf

ln -sf /etc/nginx/sites-available/neuravps-redirects.conf /etc/nginx/sites-enabled/neuravps-redirects.conf
rm -f /etc/nginx/sites-enabled/default

# Wildcard TLS (DNS-01): scripts/certbot-namecheap-dns-hooks.py
# See docs/pve-proxy-base-server-setup.md section 7.

# TLS: issue the 12-SAN dual-region cert via DNS-01 against the Hetzner
# Cloud API (token in /opt/letsencrypt/hetzner.env, chmod 600; hook =
# snippets/certbot-hetzner-dns-hooks.py; renewal is automatic via
# certbot.timer once issued — hooks are recorded in the renewal conf):
#   certbot certonly --manual --preferred-challenges dns \
#     --manual-auth-hook "/opt/letsencrypt/certbot-hetzner-dns-hooks.py auth" \
#     --manual-cleanup-hook "/opt/letsencrypt/certbot-hetzner-dns-hooks.py cleanup" \
#     --cert-name neuravps-dual \
#     -d "*.pve.neuravps.com" -d pve.neuravps.com \
#     -d "*.pve-hel.neuravps.com" -d pve-hel.neuravps.com \
#     -d "*.pve-fsn.neuravps.com" -d pve-fsn.neuravps.com \
#     -d sqx.neuravps.com -d trading.neuravps.com \
#     -d sqx-hel.neuravps.com -d trading-hel.neuravps.com \
#     -d sqx-fsn.neuravps.com -d trading-fsn.neuravps.com \
#     --non-interactive --agree-tos
# (Bootstrap alternative while the cert doesn't exist yet: clone
# /etc/letsencrypt from an existing BASE — compat names only.)
rsync -avz -e ssh root@b1.neuravps.com:/etc/letsencrypt/ /etc/letsencrypt/

# Dual-region names: extend BOTH the map regex in pve-proxy-map.conf
#   "~^([0-9]+-[^.]+)\\.pve(?:-hel|-fsn)?\\.neuravps\\.com$" $1;
# and the server_name lines in neuravps-redirects.conf
#   server_name *.pve.neuravps.com *.pve-hel.neuravps.com *.pve-fsn.neuravps.com;
#   server_name trading.neuravps.com sqx.neuravps.com trading-hel.neuravps.com sqx-hel.neuravps.com trading-fsn.neuravps.com sqx-fsn.neuravps.com;
# (as built on b0, 2026-07-04)

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-set-ticket.py \
  -o /opt/pve-set-ticket/pve-set-ticket.py
chmod 755 /opt/pve-set-ticket/pve-set-ticket.py

tee /opt/pve-set-ticket/env << 'EOF'
PVE_REDEEM_SECRET=...
REDEEM_FUNCTION_URL=https://.../redeem_pve_ticket_token
EOF
chmod 600 /opt/pve-set-ticket/env

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-set-ticket.service \
  -o /etc/systemd/system/pve-set-ticket.service
systemctl daemon-reload
systemctl enable --now pve-set-ticket

# Re-sync nodes whenever proxmox_nodes changes (or run on-demand):
#   /usr/local/sbin/sync-base-nat.py sync nodes
