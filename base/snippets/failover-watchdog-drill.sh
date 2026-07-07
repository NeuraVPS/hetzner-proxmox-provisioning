#!/bin/bash
# Zero-impact detection drill for the failover watchdog.
# Run ON the base that should look "dead" (the victim), over SSH:
#
#   failover-watchdog-drill.sh arm      # start the drill (auto-cleans in 15 min)
#   failover-watchdog-drill.sh disarm   # end it early
#
# It drops ONLY the watchdog's probe traffic: ICMP echo from the peer base
# (reporter pings) and TCP 443/22 addressed to THIS base's MAIN IPs (the
# arbiter's GCP probes). Customer traffic rides the failover VIPs and is
# untouched. Your own SSH source IP is excluded so you keep control, and a
# systemd-run dead-man's switch deletes the rules after 15 min regardless.
#
# Expected timeline with dryRun:true (validated live 2026-07-07, 0 user impact):
#   t+0:00  arm
#   t+0:30..3:00  peer journal: "peer <self> unreachable (1/6 .. 6/6)"
#   t+3:00  peer reports -> arbiter probes both bases from GCP
#   t+3:20  arbiter: {"action":"dry_run","wouldMove":[<only VIPs pointing here>]}
#           + email "[SIMULACRO]" to soporte@
#   disarm  peer logs recovery; steady state resumes
#
# With dryRun:false this drill triggers a REAL swap (established RDP sessions
# on the moved VIPs reconnect) — only do that as a deliberate rehearsal.
set -euo pipefail
ENV_FILE=/etc/neuravps/failover-watchdog.env
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE (is this a base?)"; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"   # PEER_V4 / PEER_V6 = the reporter whose pings we must drop

MAIN_V4=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p')
MAIN_V6=$(ip -6 addr show scope global 2>/dev/null | sed -n 's/.*inet6 \([0-9a-f:]*::2\)\/.*/\1/p' | head -1)
CALLER=${SSH_CLIENT%% *}   # exclude the operator's own SSH source (v4 session)

case "${1:-}" in
  arm)
    [ -n "$MAIN_V4" ] && [ -n "$MAIN_V6" ] || { echo "could not detect main IPs (v4='$MAIN_V4' v6='$MAIN_V6')"; exit 1; }
    echo "victim=$SELF peer=$PEER main_v4=$MAIN_V4 main_v6=$MAIN_V6 excluded_caller=${CALLER:-none}"
    systemd-run --unit=fwtest-deadman --on-active=15min /usr/sbin/nft delete table inet fwtest >/dev/null 2>&1 || true
    nft add table inet fwtest
    nft add chain inet fwtest input '{ type filter hook input priority -10 ; }'
    nft add rule inet fwtest input ip6 saddr "$PEER_V6" icmpv6 type echo-request drop
    nft add rule inet fwtest input ip saddr "$PEER_V4" icmp type echo-request drop
    if [ -n "$CALLER" ] && [[ "$CALLER" == *.* ]]; then
      nft add rule inet fwtest input ip saddr != "$CALLER" ip daddr "$MAIN_V4" tcp dport '{ 443, 22 }' drop
    else
      nft add rule inet fwtest input ip daddr "$MAIN_V4" tcp dport '{ 443, 22 }' drop
    fi
    nft add rule inet fwtest input ip6 daddr "$MAIN_V6" tcp dport '{ 443, 22 }' drop
    echo "ARMED (dead-man cleanup in 15 min). Watch on $PEER:"
    echo "  journalctl -u failover-watchdog.service -f"
    ;;
  disarm)
    nft delete table inet fwtest 2>/dev/null && echo "fwtest removed" || echo "fwtest not present"
    systemctl stop fwtest-deadman.timer 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 arm|disarm"; exit 1
    ;;
esac
