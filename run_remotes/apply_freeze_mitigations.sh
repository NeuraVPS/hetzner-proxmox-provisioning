#!/usr/bin/env bash
# apply_freeze_mitigations.sh — apply the validated node-freeze mitigations to
# EXISTING fleet nodes IN PLACE (no reinstall). Companion to first_boot.sh (new
# installs), branch freeze-mitigations-2026-07 / PR #68.
#
# Per node, idempotently:
#   1. sysctls kernel.panic=10 + hardlockup_panic=1 + panic_on_oops=1  -> LIVE.
#   2. processor.max_cstate=1 in /etc/kernel/cmdline (AMD hosts only)   -> next reboot.
#   3. netconsole: stream this node's kernel console (live, panic run-up) to the
#      region BASE (--base-ip) via a boot-time helper that RE-DERIVES the gateway
#      MAC each boot (robust to ARP/gateway changes)                    -> LIVE + persistent.
#
# NOT included: kdump (crash kernel hangs on AX162, see
# docs/INCIDENT_2026-07-08_node0000008_freeze.md) and the sync-dnat.py change.
# Validated on canary 0000032 + 0000008 (2026-07-09).
#
# Run FROM the region's BASE (it can `ssh <node>` directly AND is the netconsole
# collector target):
#   b1$ ./apply_freeze_mitigations.sh --base-ip 2a01:4f9:3070:3984::2 [-n] p2 p3 ...
#   b0$ ./apply_freeze_mitigations.sh --base-ip 2a01:4f8:2b03:18a9::2 [-n] -f fsn.txt
# --base-ip is the region base's IPv6 (netconsole streams over IPv6; the
# collector only accepts our nodes' /64s).
#     -n = DRY-RUN (report only). Needs the netconsole-collector service on the BASE.
# max_cstate needs a reboot; reboot ONLY nodes with 0 running VMs (check `qm list`).

set -uo pipefail
DRY=0; BASE_IP=""; NODES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) DRY=1; shift ;;
    --base-ip) BASE_IP="$2"; shift 2 ;;
    -f) mapfile -t f < <(grep -vE '^[[:space:]]*(#|$)' "$2"); NODES+=("${f[@]}"); shift 2 ;;
    *)  NODES+=("$1"); shift ;;
  esac
done
[[ -n "$BASE_IP" && ${#NODES[@]} -gt 0 ]] || { echo "usage: $0 --base-ip <ip> [-n] <node...> | -f <file>"; exit 2; }

read -r -d '' REMOTE <<'PAYLOAD' || true
set -uo pipefail
DRY="${1:-0}"; BASE_IP="${2:?base-ip required}"; changed=""
# 1) stability sysctls (LIVE)
WANT=$'kernel.panic = 10\nkernel.hardlockup_panic = 1\nkernel.panic_on_oops = 1'
if [ "$(cat /etc/sysctl.d/99-neuravps-stability.conf 2>/dev/null)" != "$WANT" ]; then
  changed="$changed sysctl"
  [ "$DRY" != 1 ] && { printf '%s\n' "$WANT" > /etc/sysctl.d/99-neuravps-stability.conf; sysctl -p /etc/sysctl.d/99-neuravps-stability.conf >/dev/null 2>&1; }
fi
# 2) processor.max_cstate=1 in the effective cmdline (AMD; next reboot)
KC=/etc/kernel/cmdline
if grep -qi 'AuthenticAMD\|AMD EPYC' /proc/cpuinfo && [ -f "$KC" ] && command -v proxmox-boot-tool >/dev/null 2>&1 && ! grep -qw 'processor.max_cstate=1' "$KC"; then
  changed="$changed cmdline(reboot)"
  [ "$DRY" != 1 ] && { CUR="$(tr -s ' ' < "$KC" | sed 's/[[:space:]]*$//')"; printf '%s processor.max_cstate=1\n' "$CUR" > "$KC"; proxmox-boot-tool refresh >/dev/null 2>&1; }
fi
# 3) netconsole: region BASE in a conf, a static helper that re-derives the
#    gateway MAC each boot, a oneshot unit, enable + run live.
if [ "$(sed -n 's/^BASE_IP=//p' /etc/neuravps-netconsole.conf 2>/dev/null)" != "$BASE_IP" ] || ! grep -q '#NCVER=3' /usr/local/sbin/neuravps-netconsole.sh 2>/dev/null; then
  changed="$changed netconsole"
  if [ "$DRY" != 1 ]; then
    printf 'BASE_IP=%s\nPORT=6666\n' "$BASE_IP" > /etc/neuravps-netconsole.conf
    cat > /usr/local/sbin/neuravps-netconsole.sh <<'NCH'
#!/bin/bash
#NCVER=3
. /etc/neuravps-netconsole.conf 2>/dev/null
[ -n "$BASE_IP" ] || exit 0
PORT=${PORT:-6666}
# BASE_IP is the region base's IPv6. Resolve v6 src/dev/gateway + the gateway
# MAC (NDP), retrying while routes/neighbours settle at boot.
MAC=""; SRC=""; DEV=""; GW=""
for _try in $(seq 1 10); do
  read -r SRC DEV GW < <(ip -6 route get "$BASE_IP" 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="src")s=$(i+1); if($i=="dev")d=$(i+1); if($i=="via")g=$(i+1)}} END{print s, d, g}')
  [ -n "$GW" ] || GW="$BASE_IP"
  ping6 -c1 -W1 "$GW" >/dev/null 2>&1
  MAC=$(ip -6 neigh show "$GW" dev "$DEV" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="lladdr"){print $(i+1); exit}}')
  [ -n "$MAC" ] && [ -n "$SRC" ] && [ -n "$DEV" ] && break
  sleep 3
done
[ -n "$MAC" ] && [ -n "$SRC" ] && [ -n "$DEV" ] || { logger "neuravps-netconsole: v6 params incompletos (gw=$GW src=$SRC dev=$DEV mac=$MAC)"; exit 1; }
# IPv6 netconsole must go through configfs — the module-param path does not
# transmit v6 on this kernel (validated 2026-07-09). Reload the module clean
# (drop any stale param target), then (re)create our dynamic target.
modprobe -r netconsole 2>/dev/null
modprobe netconsole 2>/dev/null
CG=/sys/kernel/config/netconsole
[ -d "$CG" ] || { logger "neuravps-netconsole: configfs netconsole no disponible"; exit 1; }
T="$CG/neuravps"
[ -d "$T" ] && { echo 0 > "$T/enabled" 2>/dev/null; rmdir "$T" 2>/dev/null; }
mkdir -p "$T" 2>/dev/null || { logger "neuravps-netconsole: no pude crear target configfs"; exit 1; }
echo "$DEV"     > "$T/dev_name"
echo "$SRC"     > "$T/local_ip"
echo "$BASE_IP" > "$T/remote_ip"
echo "$MAC"     > "$T/remote_mac"
echo "$PORT"    > "$T/remote_port"
if echo 1 > "$T/enabled" 2>/dev/null; then
  logger "neuravps-netconsole: [v6/configfs] $SRC/$DEV -> $BASE_IP/$MAC (gw $GW)"
else
  logger "neuravps-netconsole: enable configfs FALLO"; exit 1
fi
NCH
    chmod +x /usr/local/sbin/neuravps-netconsole.sh
    cat > /etc/systemd/system/neuravps-netconsole.service <<'UNIT'
[Unit]
Description=NeuraVPS netconsole (stream kernel console to region BASE)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neuravps-netconsole.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload; systemctl enable neuravps-netconsole.service >/dev/null 2>&1
    /usr/local/sbin/neuravps-netconsole.sh >/dev/null 2>&1
  fi
fi
P=$(sysctl -n kernel.panic 2>/dev/null); H=$(sysctl -n kernel.hardlockup_panic 2>/dev/null); O=$(sysctl -n kernel.panic_on_oops 2>/dev/null)
CS=$(grep -qw processor.max_cstate=1 /proc/cmdline && echo live || echo pending)
NC=$([ -d /sys/module/netconsole ] && echo up || echo down)
echo "sysctls=${P}/${H}/${O} max_cstate=${CS} netconsole=${NC} changed=[${changed:- none}]"
PAYLOAD
export B64; B64=$(printf '%s' "$REMOTE" | base64 -w0)
export DRY BASE_IP

echo "== apply_freeze_mitigations$([ "$DRY" = 1 ] && echo ' (DRY-RUN)') base=$BASE_IP over ${#NODES[@]} node(s) =="
printf '%s\n' "${NODES[@]}" | xargs -P 12 -I NODE bash -c '
  r=$(timeout 45 ssh -o ConnectTimeout=8 -o BatchMode=yes NODE "echo $B64 | base64 -d | bash -s $DRY $BASE_IP" 2>&1 | tail -1)
  printf "%-9s %s\n" "NODE" "${r:-UNREACHABLE}"'
echo "== done. netconsole+sysctls LIVE now; max_cstate on each node's NEXT reboot (reboot ONLY 0-running-VM nodes). =="
