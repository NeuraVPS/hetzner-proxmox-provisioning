#!/usr/bin/env bash
# apply_freeze_mitigations.sh — apply the validated node-freeze mitigations to
# EXISTING fleet nodes IN PLACE (no reinstall). Companion to first_boot.sh (new
# installs) from branch freeze-mitigations-2026-07 / PR #68.
#
# Applies, idempotently, per node:
#   1. /etc/sysctl.d/99-neuravps-stability.conf  (kernel.panic=10 +
#      hardlockup_panic=1 + panic_on_oops=1)  -> ACTIVE IMMEDIATELY.
#   2. processor.max_cstate=1 in /etc/kernel/cmdline (AMD hosts only) + a
#      proxmox-boot-tool refresh -> effective on the node's NEXT reboot.
#      THIS SCRIPT NEVER REBOOTS.
#
# NOT included: kdump (dropped — the crash kernel hangs on AX162 and leaves the
# node dead; see docs/INCIDENT_2026-07-08_node0000008_freeze.md) and the
# sync-dnat.py change (separate deploy, pending the auto-restart decision).
#
# Validated on canary 0000032 (2026-07-09): panic=10 auto-reboots a hung node in
# ~2 min; max_cstate=1 removed the deep C2 idle state.
#
# Usage (run wherever you can `ssh <node>` — a BASE, or the sandbox):
#   ./apply_freeze_mitigations.sh [-n] p2 p3 p4 ...     # nodes as args
#   ./apply_freeze_mitigations.sh [-n] -f nodes.txt     # nodes from file (1/line)
#     -n = DRY-RUN (report only, change nothing)
# After a real run the sysctls are LIVE fleet-wide; schedule a ROLLING reboot so
# max_cstate takes effect (never reboot the whole fleet at once).

set -uo pipefail
DRY=0; NODES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) DRY=1; shift ;;
    -f) mapfile -t f < <(grep -vE '^[[:space:]]*(#|$)' "$2"); NODES+=("${f[@]}"); shift 2 ;;
    *)  NODES+=("$1"); shift ;;
  esac
done
[[ ${#NODES[@]} -gt 0 ]] || { echo "usage: $0 [-n] <node...> | -f <file>"; exit 2; }

read -r -d '' REMOTE <<'PAYLOAD' || true
set -uo pipefail
DRY="${1:-0}"; changed=""
WANT=$'kernel.panic = 10\nkernel.hardlockup_panic = 1\nkernel.panic_on_oops = 1'
if [ "$(cat /etc/sysctl.d/99-neuravps-stability.conf 2>/dev/null)" != "$WANT" ]; then
  changed="$changed sysctl"
  if [ "$DRY" != 1 ]; then
    printf '%s\n' "$WANT" > /etc/sysctl.d/99-neuravps-stability.conf
    sysctl -p /etc/sysctl.d/99-neuravps-stability.conf >/dev/null 2>&1
  fi
fi
KC=/etc/kernel/cmdline
if grep -qi 'AuthenticAMD\|AMD EPYC' /proc/cpuinfo && [ -f "$KC" ] && command -v proxmox-boot-tool >/dev/null 2>&1; then
  if ! grep -qw 'processor.max_cstate=1' "$KC"; then
    changed="$changed cmdline(reboot-pending)"
    if [ "$DRY" != 1 ]; then
      CUR="$(tr -s ' ' < "$KC" | sed 's/[[:space:]]*$//')"
      printf '%s processor.max_cstate=1\n' "$CUR" > "$KC"
      proxmox-boot-tool refresh >/dev/null 2>&1
    fi
  fi
fi
P=$(sysctl -n kernel.panic 2>/dev/null); H=$(sysctl -n kernel.hardlockup_panic 2>/dev/null); O=$(sysctl -n kernel.panic_on_oops 2>/dev/null)
CS=$(grep -qw processor.max_cstate=1 /proc/cmdline && echo live || echo pending)
echo "sysctls=${P}/${H}/${O} max_cstate=${CS} changed=[${changed:- none}]"
PAYLOAD
export B64; B64=$(printf '%s' "$REMOTE" | base64 -w0)
export DRY

echo "== apply_freeze_mitigations$([ "$DRY" = 1 ] && echo ' (DRY-RUN)') over ${#NODES[@]} node(s) =="
printf '%s\n' "${NODES[@]}" | xargs -P 16 -I NODE bash -c '
  r=$(timeout 40 ssh -o ConnectTimeout=8 -o BatchMode=yes NODE "echo $B64 | base64 -d | bash -s $DRY" 2>&1 | tail -1)
  printf "%-9s %s\n" "NODE" "${r:-UNREACHABLE}"'
echo "== done. sysctls LIVE now; max_cstate applies on each node's NEXT reboot -> schedule a ROLLING reboot. =="
