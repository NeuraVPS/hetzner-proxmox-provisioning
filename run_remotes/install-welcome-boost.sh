#!/bin/bash
# Instala neuravps-welcome-boost en una BASE (daemon systemd). Idempotente.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "${HERE}/neuravps-welcome-boost.py" /usr/local/sbin/neuravps-welcome-boost.py

cat > /etc/systemd/system/neuravps-welcome-boost.service <<'UNIT'
[Unit]
Description=NeuraVPS welcome boost (restore plan RAM the moment a customer's RDP login lands)
After=network-online.target nftables.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/sbin/neuravps-welcome-boost.py
Restart=always
RestartSec=15
SyslogIdentifier=neuravps-welcome-boost

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now neuravps-welcome-boost.service
sleep 2
systemctl is-active neuravps-welcome-boost.service
