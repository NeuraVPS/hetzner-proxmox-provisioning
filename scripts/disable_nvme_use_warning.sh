cat >/usr/local/sbin/smartd-filter.sh <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

MESSAGE="${SMARTD_MESSAGE:-}"
FAILTYPE="${SMARTD_FAILTYPE:-}"
DEVICE="${SMARTD_DEVICE:-}"
DEVICESTRING="${SMARTD_DEVICESTRING:-}"
FULLMESSAGE="${SMARTD_FULLMESSAGE:-}"

combined_text="$(printf '%s\n%s\n%s\n%s\n%s\n' \
  "$MESSAGE" "$FULLMESSAGE" "$FAILTYPE" "$DEVICE" "$DEVICESTRING")"

# Suppress only the NVMe wear/reliability alert:
#   - Critical Warning (0x04): Reliability
#   - NVM subsystem reliability has been degraded
if grep -qiE 'Critical Warning \(0x04\): Reliability|NVM subsystem reliability has been degraded' <<<"$combined_text"; then
  logger -t smartd-filter "Suppressed known NVMe reliability/wear alert for ${DEVICE:-unknown}: ${MESSAGE:-$FAILTYPE}"
  exit 0
fi

exec /usr/share/smartmontools/smartd-runner
EOF

chmod 755 /usr/local/sbin/smartd-filter.sh
cp -a /etc/smartd.conf /etc/smartd.conf.bak.$(date +%Y%m%d-%H%M%S)

cat >/etc/smartd.conf <<'EOF'
/dev/nvme0 -d nvme -a -n standby -m root -M exec /usr/local/sbin/smartd-filter.sh
/dev/nvme1 -d nvme -a -n standby -m root -M exec /usr/local/sbin/smartd-filter.sh
EOF

smartd -q onecheck && systemctl restart smartd && systemctl --no-pager --full status smartd