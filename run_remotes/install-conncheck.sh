#!/bin/bash
# Instala neuravps-conncheck en una BASE: copia el prober adyacente a
# /usr/local/sbin y crea service+timer. El offset horario depende del host
# (b0 a los :05, b1 a los :35) para que las dos pasadas no coincidan sobre
# las mismas VMs. Idempotente.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "${HERE}/neuravps-conncheck.py" /usr/local/sbin/neuravps-conncheck.py

case "$(hostname)" in
  0000000-BASE) MINUTE=05 ;;
  0000001-BASE) MINUTE=35 ;;
  *) echo "host $(hostname) no es una BASE conocida"; exit 1 ;;
esac

cat > /etc/systemd/system/neuravps-conncheck.service <<UNIT
[Unit]
Description=NeuraVPS per-VM connectivity sweep (probes the PEER base's NAT path)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/neuravps-conncheck.py
SyslogIdentifier=neuravps-conncheck
TimeoutStartSec=900
UNIT

cat > /etc/systemd/system/neuravps-conncheck.timer <<UNIT
[Unit]
Description=Run neuravps-conncheck hourly (offset :${MINUTE} on this base)

[Timer]
OnCalendar=*-*-* *:${MINUTE}:00
RandomizedDelaySec=60
Persistent=false

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now neuravps-conncheck.timer
systemctl list-timers neuravps-conncheck.timer --no-pager | head -3
