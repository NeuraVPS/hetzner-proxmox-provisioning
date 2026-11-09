#!/usr/bin/env bash
############################################################
# NeuraVPS: weekly guest ReTrim on EX44 hosts (NEXRETRIM=1)
#
# WHY: Windows only returns deleted space to the zvol when it runs a ReTrim.
# On 2026-11-09 EX44 0000129 had a 512 G zvol whose guest used 377 G but
# whose zvol held 435 G; the 448 G rpool filled, the VM paused and the host
# became unreachable. `qm guest cmd fstrim` does NOT work on Windows
# (0x80070001), so we run Optimize-Volume -ReTrim inside the guest via
# `qm guest exec`.
#
# Per running VM with a responsive QGA: ReTrim every fixed NTFS volume,
# log the zvol size before/after to the journal. Unresponsive agent -> skip
# and log. Never reboots, stops or deletes anything. flock: no overlap.
# QGA output is only trusted if it carries our NV-RUN-<nonce> marker
# (QGA can return another process's output for a reused PID).
############################################################
NEXRETRIM=1
set -u
GUEST_TIMEOUT="${NV_RETRIM_GUEST_TIMEOUT:-1500}"   # seconds per VM
PING_TIMEOUT=15
LOCK=/run/neuravps-ex44-retrim.lock

case "$(hostname)" in *EX44*) ;; *) echo "not an EX44 host ($(hostname)) - nothing to do"; exit 0;; esac
exec 9>"$LOCK"
flock -n 9 || { echo "another retrim run is active - exiting"; exit 0; }

log() { echo "retrim: $*"; }

zvol_bytes() { # sum of usedbydataset over all zvols of a VM
  zfs list -Hp -t volume -o name,usedbydataset 2>/dev/null \
    | awk -v p="/vm-$1-disk-" 'index($1,p){s+=$2} END{print s+0}'
}
gib() { awk -v b="$1" 'BEGIN{printf "%.1f", b/1073741824}'; }

pool_line() { zpool list -Hp -o name,size,alloc,free rpool 2>/dev/null \
  | awk '{printf "rpool alloc=%.1fG free=%.1fG of %.1fG", $3/1073741824, $4/1073741824, $2/1073741824}'; }

log "start $(pool_line)"
ok=0; skipped=0; failed=0

while read -r vmid; do
  [[ "$vmid" =~ ^[0-9]+$ ]] || continue
  if ! timeout "$PING_TIMEOUT" qm guest cmd "$vmid" ping >/dev/null 2>&1; then
    log "vm=$vmid SKIP agent not responding"; skipped=$((skipped+1)); continue
  fi
  # Windows guests only
  if ! qm config "$vmid" 2>/dev/null | grep -qiE '^ostype: *w'; then
    log "vm=$vmid SKIP not a Windows guest"; skipped=$((skipped+1)); continue
  fi
  nonce="$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  ps=$(cat <<PS
\$ErrorActionPreference = 'Continue'
\$tag = 'NV-RUN-$nonce'
Write-Output "\$tag START"
foreach (\$v in (Get-Volume | Where-Object { \$_.DriveType -eq 'Fixed' -and \$_.FileSystem -eq 'NTFS' -and \$_.DriveLetter })) {
  \$sw = [Diagnostics.Stopwatch]::StartNew()
  try {
    Optimize-Volume -DriveLetter \$v.DriveLetter -ReTrim -ErrorAction Stop | Out-Null
    Write-Output ("\$tag VOL " + \$v.DriveLetter + " ok " + [int]\$sw.Elapsed.TotalSeconds + "s")
  } catch {
    Write-Output ("\$tag VOL " + \$v.DriveLetter + " ERR " + \$_.Exception.Message)
  }
}
Write-Output "\$tag DONE"
PS
)
  enc="$(printf '%s' "$ps" | iconv -t UTF-16LE | base64 -w0)"
  before=$(zvol_bytes "$vmid")
  t0=$SECONDS
  # Async exec + our own polling: the synchronous form dies when a single
  # guest-exec-status call exceeds QMP's short timeout (ReTrim takes minutes).
  raw=""; rc=1
  pid="$(qm guest exec "$vmid" --synchronous 0 --timeout 0 -- powershell -NoProfile -NonInteractive -EncodedCommand "$enc" 2>/dev/null \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["pid"])
except Exception: pass' 2>/dev/null)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    while (( SECONDS - t0 < GUEST_TIMEOUT )); do
      sleep 10
      st="$(timeout 60 qm guest exec-status "$vmid" "$pid" 2>/dev/null)" || continue   # transient QGA timeout: keep polling
      if printf '%s' "$st" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("exited") else 1)' 2>/dev/null; then
        raw="$st"; rc=0; break
      fi
    done
    [[ $rc -ne 0 ]] && raw="no exit after ${GUEST_TIMEOUT}s (pid $pid)"
  else
    raw="guest exec did not start"
  fi
  after=$(zvol_bytes "$vmid")
  out="$(printf '%s' "$raw" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(d.get("out-data",""), end="")' 2>/dev/null)"
  vols="$(printf '%s\n' "$out" | tr -d '\r' | grep "NV-RUN-$nonce VOL" | sed "s/NV-RUN-$nonce VOL //" | paste -sd, -)"
  if printf '%s' "$out" | grep -q "NV-RUN-$nonce DONE"; then
    log "vm=$vmid OK before=$(gib "$before")G after=$(gib "$after")G freed=$(gib $((before-after)))G took=$((SECONDS-t0))s vols=[${vols}]"
    ok=$((ok+1))
  else
    log "vm=$vmid FAIL rc=$rc no valid NV-RUN-$nonce marker (timeout or foreign output) before=$(gib "$before")G after=$(gib "$after")G took=$((SECONDS-t0))s vols=[${vols}] raw=$(printf '%s' "$raw" | head -c 200 | tr '\n' ' ')"
    failed=$((failed+1))
  fi
done < <(qm list 2>/dev/null | awk '$3=="running"{print $1}')

log "end ok=$ok skipped=$skipped failed=$failed $(pool_line)"
exit 0
