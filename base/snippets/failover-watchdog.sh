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
#
# State in /run (resets on boot): consecutive-failure counter + report backoff.
set -u
ENV_FILE=/etc/neuravps/failover-watchdog.env
[ -r "$ENV_FILE" ] || { echo "failover-watchdog: missing $ENV_FILE"; exit 0; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${THRESHOLD:=6}"

STATE=/run/failover-watchdog.fails
BACKOFF=/run/failover-watchdog.lastreport

peer_alive() {
  ping -6 -c1 -W2 "$PEER_V6" >/dev/null 2>&1 && return 0
  ping -4 -c1 -W2 "$PEER_V4" >/dev/null 2>&1 && return 0
  return 1
}

if peer_alive; then
  if [ -s "$STATE" ] && [ "$(cat "$STATE")" -ge "$THRESHOLD" ]; then
    echo "failover-watchdog: peer $PEER recovered (was down)"
  fi
  echo 0 > "$STATE"
  exit 0
fi

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
