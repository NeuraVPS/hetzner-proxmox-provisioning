#!/bin/bash
# BASE peer watchdog — reporter half of the automatic failover system.
#
# Runs every 30 s from failover-watchdog.timer on EACH base. Pings the peer
# base (v6, falling back to v4); after THRESHOLD consecutive full failures it
# reports to the failover_watchdog Cloud Function, which is the ARBITER: it
# re-verifies both bases from GCP and only then moves the failover VIPs to
# the survivor and emails soporte@. This script never touches VIPs itself.
#
# Config: /etc/neuravps/failover-watchdog.env  (mode 600, root)
#   SELF=b1                 # this base's id (b0|b1)
#   PEER=b0                 # the other base's id
#   PEER_V6=2a01:4f8:2b03:18a9::2
#   PEER_V4=188.40.153.120
#   CF_URL=https://failover-watchdog-....a.run.app
#   TOKEN=<shared bearer token = GCP secret FAILOVER_WATCHDOG_TOKEN>
#   THRESHOLD=6             # consecutive failures before reporting (~3 min)
#   RECOVER_OK=4            # consecutive OK ticks (~2 min) before clearing fails
#
# State in /run (resets on boot): consecutive-failure counter + OK-streak +
# report backoff.
set -u
ENV_FILE=/etc/neuravps/failover-watchdog.env
[ -r "$ENV_FILE" ] || { echo "failover-watchdog: missing $ENV_FILE"; exit 0; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${THRESHOLD:=6}"
: "${RECOVER_OK:=4}"

STATE=/run/failover-watchdog.fails
OKSTREAK=/run/failover-watchdog.okstreak
BACKOFF=/run/failover-watchdog.lastreport

peer_alive() {
  # APPLICATION-layer only: a zombie host whose kernel still answers ping and
  # ACKs SYNs (2026-07-13: b1 froze exactly like that) must NOT count as
  # alive. nginx completing a TLS exchange or sshd sending its banner is the
  # bar — both require live userspace.
  curl -ksS --max-time 4 -o /dev/null "https://[$PEER_V6]/" 2>/dev/null && return 0
  curl -ksS --max-time 4 -o /dev/null "https://$PEER_V4/" 2>/dev/null && return 0
  timeout 4 bash -c "exec 3<>/dev/tcp/$PEER_V4/22 && head -c4 <&3" 2>/dev/null | grep -q "SSH-" && return 0
  return 1
}

if peer_alive; then
  OK=$(( $(cat "$OKSTREAK" 2>/dev/null || echo 0) + 1 ))
  echo "$OK" > "$OKSTREAK"
  FAILS=$(cat "$STATE" 2>/dev/null || echo 0)
  # Anti-flap hysteresis: a single good tick must not wipe the failure
  # counter (2026-07-13: b1 flapped up for ~30 s, the counter reset, and the
  # 6/6 threshold was never re-reached while customers stayed down). Only a
  # RECOVER_OK-tick stable streak clears it.
  if [ "$FAILS" -gt 0 ] && [ "$OK" -lt "$RECOVER_OK" ]; then
    echo "failover-watchdog: peer $PEER ok (streak ${OK}/${RECOVER_OK}); fails=${FAILS} retained (anti-flap)"
    exit 0
  fi
  if [ "$FAILS" -ge "$THRESHOLD" ]; then
    echo "failover-watchdog: peer $PEER recovered (stable for ${RECOVER_OK} ticks)"
  fi
  echo 0 > "$STATE"
  exit 0
fi

echo 0 > "$OKSTREAK"
FAILS=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$FAILS" > "$STATE"
echo "failover-watchdog: peer $PEER unreachable (${FAILS}/${THRESHOLD})"
[ "$FAILS" -lt "$THRESHOLD" ] && exit 0

# Report at most every 10 runs (~5 min) while down — the CF has its own
# cooldown; this just avoids hammering it every 30 s.
NOW=$(date +%s)
LAST=$(cat "$BACKOFF" 2>/dev/null || echo 0)
[ $(( NOW - LAST )) -lt 300 ] && exit 0
echo "$NOW" > "$BACKOFF"

echo "failover-watchdog: reporting to arbiter (fails=$FAILS)"
HTTP=$(curl -sS -o /run/failover-watchdog.resp -w '%{http_code}' \
  --max-time 90 -X POST "$CF_URL" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"reporter\":\"$SELF\",\"peer\":\"$PEER\",\"consecutiveFailures\":$FAILS}" \
  2>/dev/null || echo 000)
echo "failover-watchdog: arbiter answered HTTP $HTTP: $(cat /run/failover-watchdog.resp 2>/dev/null | head -c 300)"
exit 0
