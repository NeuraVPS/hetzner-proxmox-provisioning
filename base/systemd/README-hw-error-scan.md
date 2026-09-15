# Daily netconsole hardware-error sweep (runs on BOTH bases, b0 and b1)

Why: node `0000235-AX162-2-LTD` died 2026-09-15 with its NVMe logging kernel
errors in `/var/log/netconsole/<ipv6>.log` for **ten days** (1 line on 05/09
-> 32,316 on 15/09, the day it died) before anyone read the file. netconsole
is the one source that survives a hard hang — it's a raw UDP kernel console
feed, independent of the guest agent, SSH, or anything in userspace on the
node — so it's the right thing to watch. Full docstring/rationale (including
THE TRAP — why this isn't a one-line grep) in
`run_remotes/neuravps-hw-error-scan.py`.

Baseline swept live across ~200 files on both bases the day this was built:
**zero background noise**. Only the dead node and one node whose matching
lines were all from a boot before its hardware swap (0000207) hit. That
means no threshold/calibration: any error line in a node's CURRENT boot is
real signal, and staying silent otherwise is correct.

Install / reinstall after a base rebuild (run on EACH base — unlike defrag,
this is not b0-only, since each base only sees the nodes that netconsole to
it):
```
install -m755 run_remotes/neuravps-hw-error-scan.py /usr/local/sbin/neuravps-hw-error-scan.py
install -m644 base/systemd/neuravps-hw-error-scan.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now neuravps-hw-error-scan.timer
```

Needs:
- `/var/log/netconsole/*.log` (already collected by `netconsole-collector.service`,
  see `run_remotes/setup_netconsole_base.sh`).
- `/var/lib/base-nat/pve_nodes.json` (nodeId -> ipv6, kept fresh by
  `sync-base-nat.py`, already present on both bases for the other daemons) —
  used only to label nodes by name in the email; if it's missing or stale
  the scan still runs and just labels the node by its raw IPv6 instead of
  silently skipping it.
- `/etc/neuravps/hw-error-scan.env` (mode 600, root-only) with one line:
  `SMTP_PASSWORD=<the Gmail app password>` — same Gmail account
  (`soporte@neuravps.com`, authenticated SMTP, `smtp.gmail.com:587`) the
  Cloud Functions email service already uses (`functions/email_service.py`,
  Firebase secret `SMTP_PASSWORD`). Sending straight from the base with
  authenticated SMTP is what makes this deliverable at all — the fleet's own
  unauthenticated postfix→direct-to-MX path has been dead since the DMARC
  `p=reject` change (see `memory/neuravps-dmarc-node-mail.md`); this script
  never touches that path.

No Firestore kill-switch, no state file, no dedup across runs — kept
deliberately dumb per the operator's ask ("una sola cosa sencilla, no un
sistema"). It's read-only against `/var/log/netconsole` and only ever sends
mail; it cannot touch a node, a VM, or a customer. If a node keeps erroring
for ten days it should get ten emails, growing — that IS the point.

## Behavior

- Finds nothing in any node's CURRENT boot (the segment of the log after the
  last `netconsole: network logging started` line) -> **silent, exit 0, no
  email.**
- Finds current-boot errors on one or more nodes -> **one email** to
  `soporte@neuravps.com`, subject
  `[NeuraVPS] Errores de hardware en N nodo(s) (<base>)`, one section per
  node: node id, error count, device(s) involved (`0000:xx:00.0` PCI
  address or `MCnn_STATUS`), and the last 5 matching lines.

## Testing without touching the live install

```
python3 run_remotes/neuravps-hw-error-scan.py --log-dir /some/test/dir \
  --nodes-map /some/test/nodes.json --dry-run
```
`--dry-run` (alias `--no-send`) prints the report (or nothing, if clean) and
never calls SMTP. Verified live 2026-09-15 against real fleet data on both
bases (silent on both — 0000207's matches are all pre-hardware-swap, the
dead 0000235 had already been power-cycled onto a clean boot by the time of
the test) and against synthetic fixtures covering: a node erroring in its
current boot (alerts, correct device + sample lines), the 0000207-style trap
(silent), a totally clean node (silent), a memory `MCnn_STATUS` hit
(alerts), and a file with no boot marker at all (fails open — alerts, since
staleness can't be proven).

Real delivery was verified end-to-end on 2026-09-15: ran the script for real
(not `--dry-run`) on b0 against a synthetic log carrying a unique marker,
confirmed `email sent` in the script's own output, then independently
fetched `soporte@neuravps.com` via the Gmail API (same DWD path
`eval_support_agent.py` uses for local runs) and found the message —
correct subject, correct body, arrived within seconds. The test message was
deleted from the mailbox afterward.

Journal: `journalctl -t neuravps-hw-error-scan` on each base.
