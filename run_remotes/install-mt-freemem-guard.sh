#!/bin/bash
# Instala neuravps-mt-freemem-guard en un NODO AX102 (MT). Idempotente: nunca
# pisa /etc/default si ya existe (ahi vive el kill switch del nodo).
#
# Un nodo nuevo arranca con el DECAY ACTIVO y PROTECT APAGADO: es el estado que
# el operador aprobo para los 25 AX102 el 2026-09-18 (decay real en 048/147
# desde el 17/09 sin rebotes ni errores). PROTECT (PIN/STALE) sigue apagado:
# en el canario 0000147 devolvio ~40 GB de globo y el host lo saco a swap.
# first_boot.sh llama a este script en cada AX102 nuevo o reinstalado.
#
# MTG_DRY_RUN=1 al instalar deja el nodo en modo registro (decide, no toca).
# Kill switch (efecto en <1 min, sin redesplegar), en
# /etc/default/neuravps-mt-freemem-guard: ENABLED=0 lo apaga entero,
# DRY_RUN=1 solo registra, DECAY_ENABLED=0 quita el decay.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$(hostname)" in *-AX102*) ;; *) echo "no es un nodo AX102; nada que hacer"; exit 0 ;; esac

install -m 755 "${HERE}/neuravps-mt-freemem-guard.py" /usr/local/sbin/neuravps-mt-freemem-guard.py

MTG_DRY_RUN="${MTG_DRY_RUN:-0}"
case "$MTG_DRY_RUN" in 0|1) ;; *) echo "MTG_DRY_RUN debe ser 0 o 1" >&2; exit 2 ;; esac
if [ ! -f /etc/default/neuravps-mt-freemem-guard ]; then
  cat > /etc/default/neuravps-mt-freemem-guard <<CONF
# Gestionado por NeuraVPS. ENABLED=0 apaga el guard; DRY_RUN=1 solo registra;
# DECAY_ENABLED=0 quita el decay; PROTECT_ENABLED=1 reactivaria PIN/STALE (NO:
# saco el host a swap en el canario 0000147); EXCLUDE_VMIDS=1,2 saca VMs.
ENABLED=1
DRY_RUN=${MTG_DRY_RUN}
DECAY_ENABLED=1
PROTECT_ENABLED=0
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
