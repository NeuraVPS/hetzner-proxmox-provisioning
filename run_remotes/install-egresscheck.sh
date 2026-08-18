#!/bin/bash
# Instala neuravps-egresscheck en una BASE: copia el prober adyacente a
# /usr/local/sbin y crea service+timer. Idempotente.
#
# Offsets a :15 y :45 a propósito, esquivando los de conncheck (:05 y :35): las
# dos sondas entran en los mismos nodos por SSH y solaparlas sólo añade cola.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "${HERE}/neuravps-egresscheck.py" /usr/local/sbin/neuravps-egresscheck.py

case "$(hostname)" in
  0000000-BASE) MINUTE=15 ;;   # b0, Falkenstein
  0000001-BASE) MINUTE=45 ;;   # b1, Helsinki
  *) echo "host $(hostname) no es una BASE conocida"; exit 1 ;;
esac

cat > /etc/systemd/system/neuravps-egresscheck.service <<UNIT
[Unit]
Description=NeuraVPS egress probe (do the GUESTS reach the internet?)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/neuravps-egresscheck.py
SyslogIdentifier=neuravps-egresscheck
# Una pasada son ~240 sondas por el agente + una re-sonda a los 45 s. Medido:
# 60 s la primera ronda. 900 s da margen de sobra sin dejarla colgada.
TimeoutStartSec=900
UNIT

cat > /etc/systemd/system/neuravps-egresscheck.timer <<UNIT
[Unit]
Description=Run neuravps-egresscheck hourly (offset :${MINUTE} on this base)

[Timer]
OnCalendar=*-*-* *:${MINUTE}:00
RandomizedDelaySec=60
Persistent=false

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now neuravps-egresscheck.timer
systemctl list-timers neuravps-egresscheck.timer --no-pager | head -3
