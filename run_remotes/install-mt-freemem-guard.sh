#!/bin/bash
# Instala neuravps-mt-freemem-guard en un NODO AX102 (MT). Idempotente.
# Arranca en DRY_RUN=1: decide y registra, no toca nada. Para actuar:
#   sed -i 's/^DRY_RUN=.*/DRY_RUN=0/' /etc/default/neuravps-mt-freemem-guard
# Kill switch (efecto en <1 min, sin redesplegar): ENABLED=0 en ese fichero.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(hostname)" in *-AX102*) ;; *) echo "no es un nodo AX102; nada que hacer"; exit 0 ;; esac

install -m 755 "${HERE}/neuravps-mt-freemem-guard.py" /usr/local/sbin/neuravps-mt-freemem-guard.py

if [ ! -f /etc/default/neuravps-mt-freemem-guard ]; then
  cat > /etc/default/neuravps-mt-freemem-guard <<'CONF'
# Gestionado por NeuraVPS. ENABLED=0 apaga el guard; DRY_RUN=1 solo registra.
ENABLED=1
DRY_RUN=1
CONF
fi

cat > /etc/systemd/system/neuravps-mt-freemem-guard.service <<'UNIT'
[Unit]
Description=NeuraVPS MT free-memory guard (no retener globo a invitados que van justos)

[Service]
Type=oneshot
Nice=10
TimeoutStartSec=50
ExecStart=/usr/bin/python3 /usr/local/sbin/neuravps-mt-freemem-guard.py
SyslogIdentifier=neuravps-mt-freemem-guard
UNIT

cat > /etc/systemd/system/neuravps-mt-freemem-guard.timer <<'UNIT'
[Unit]
Description=Run neuravps-mt-freemem-guard every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now neuravps-mt-freemem-guard.timer
systemctl is-active neuravps-mt-freemem-guard.timer
