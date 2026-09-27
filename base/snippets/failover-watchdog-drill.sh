#!/bin/bash
# Drill for the failover watchdog.
# Run ON the base that should look "dead" (the victim), over SSH:
#
#   failover-watchdog-drill.sh arm            # probes only (auto-cleans in 15 min)
#   failover-watchdog-drill.sh arm-dataplane  # probes + all GRE (auto-cleans: 15 / 20 min)
#   failover-watchdog-drill.sh disarm         # end either mode early
#
# arm: drops ONLY the watchdog's probe traffic: ICMP echo from the peer base
# (reporter pings) and TCP 443/22 addressed to THIS base's MAIN IPs (the
# arbiter's GCP probes). Your own SSH source IP is excluded so you keep
# control, and a systemd-run dead-man's switch (fwtest-deadman) deletes the
# rules after 15 min regardless. Use it to validate detection (dryRun:true).
#
# arm-dataplane: does everything arm does AND drops every GRE packet, in and
# out, on this base (table inet fwtest_tun, priority -20, own dead-man
# fwtest-tun-deadman, 20 min). The nodes' ip6gre tunnels are anchored to the
# VIP v6, so with GRE dead the base is mute for the nodes' egress too and they
# fail over to the other base by themselves (neuravps-tunnel-probe). Cutting
# only the forward path would NOT trigger that probe: it is local traffic
# (TCP/443 to the canonical address through the tunnel). IPv4 and the control
# SSH are not touched. Use it for a rehearsal of a whole-base outage.
#
# Both modes refuse to arm if their table or dead-man already exists, and
# abort before adding any rule if the dead-man cannot be created.
#
# Expected timeline with dryRun:true (validated live 2026-07-07, 0 user impact):
#   t+0:00  arm
#   t+0:30..3:00  peer journal: "peer <self> unreachable (1/6 .. 6/6)"
#   t+3:00  peer reports -> arbiter probes both bases from GCP
#   t+3:20  arbiter: {"action":"dry_run","wouldMove":[<only VIPs pointing here>]}
#           + email "[SIMULACRO]" to soporte@
#   disarm  peer logs recovery; steady state resumes
#
# With dryRun:false REAL impact, both modes: the VIPs swap to the peer and
# established RDP sessions on the moved VIPs reconnect (new connections fail
# ~25 s on the FSN v4 VIP; fail-back ~1 min). arm-dataplane adds: every node
# whose tunnel anchors here switches its egress to the other base within
# ~20-40 s (SNAT with the HEL pair IP) and returns ~1 min after the VIP moves
# back; outbound sessions may be affected by those route changes. Only do that
# as a deliberate rehearsal (27/09/2026: see docs/failover-watchdog.md).
set -euo pipefail
ENV_FILE=/etc/neuravps/failover-watchdog.env
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE (is this a base?)"; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"   # PEER_V4 / PEER_V6 = the reporter whose pings we must drop

MAIN_V4=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p')
# Public v6 = the source the kernel uses for the default route, the address
# the peer's peer_alive() probes first (https://[PEER_V6]/). The old
# 'first global ::2' regex picked the guest-network address
# (2a01:4f9:c01f:e:ffff::2) on the ECC bases, so the drill never fired
# (26/09/2026 drill) and it briefly blocked 22/443 on the guest network.
MAIN_V6=$(ip -6 route get 2606:4700:4700::1111 2>/dev/null | sed -n 's/.* src \([0-9a-f:]*\).*/\1/p')
CALLER=${SSH_CLIENT%% *}   # exclude the operator's own SSH source (v4 session)

# preflight TABLE UNIT: refuse to duplicate rules or stack a second dead-man.
preflight() {
  if nft list table inet "$1" >/dev/null 2>&1; then echo "table $1 already exists: disarm first"; return 1; fi
  if systemctl is-active --quiet "$2.timer"; then echo "dead-man $2 already active: disarm first"; return 1; fi
}
# arm_deadman UNIT DELAY TABLE: must be confirmed active BEFORE any rule is added.
arm_deadman() {
  systemd-run --unit="$1" --on-active="$2" /usr/sbin/nft delete table inet "$3" >/dev/null 2>&1 || true
  systemctl is-active --quiet "$1.timer" || { echo "dead-man $1 NOT created: aborting, no rules added (a leftover failed unit? systemctl reset-failed $1.service $1.timer)"; return 1; }
}
arm_probe_rules() {
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
}
arm_tun_rules() {
  nft -f - <<'NFT'
table inet fwtest_tun {
  chain in  { type filter hook input  priority -20; policy accept; meta l4proto gre counter drop; }
  chain out { type filter hook output priority -20; policy accept; meta l4proto gre counter drop; }
}
NFT
}

# Do all checks and both dead-mans first; rules only go in once everything holds.
arm_common() {
  [ -n "$MAIN_V4" ] && [ -n "$MAIN_V6" ] || { echo "could not detect main IPs (v4='$MAIN_V4' v6='$MAIN_V6')"; exit 1; }
  echo "victim=$SELF peer=$PEER main_v4=$MAIN_V4 main_v6=$MAIN_V6 excluded_caller=${CALLER:-none}"
  preflight fwtest fwtest-deadman || exit 1
  if [ "$1" = dataplane ]; then preflight fwtest_tun fwtest-tun-deadman || exit 1; fi
  arm_deadman fwtest-deadman 15min fwtest || exit 1
  if [ "$1" = dataplane ]; then
    arm_deadman fwtest-tun-deadman 20min fwtest_tun || { systemctl stop fwtest-deadman.timer 2>/dev/null || true; exit 1; }
  fi
}

case "${1:-}" in
  arm)
    arm_common probes
    arm_probe_rules
    echo "ARMED (dead-man cleanup in 15 min). Watch on $PEER:"
    echo "  journalctl -u failover-watchdog.service -f"
    ;;
  arm-dataplane)
    arm_common dataplane
    arm_probe_rules
    arm_tun_rules
    echo "ARMED + GRE DROPPED $(date -u +%FT%TZ) (dead-mans: probes 15 min, GRE 20 min). Watch on $PEER:"
    echo "  journalctl -u failover-watchdog.service -f"
    ;;
  disarm)
    nft delete table inet fwtest 2>/dev/null && echo "fwtest removed" || echo "fwtest not present"
    nft delete table inet fwtest_tun 2>/dev/null && echo "fwtest_tun removed" || echo "fwtest_tun not present"
    systemctl stop fwtest-deadman.timer 2>/dev/null || true
    systemctl stop fwtest-tun-deadman.timer 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 arm|arm-dataplane|disarm"; exit 1
    ;;
esac
