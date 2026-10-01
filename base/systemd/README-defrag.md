# Daily defrag (runs on a BASE, currently b0)
Schedule: **22:10 UTC** (since 2026-10-01; was 06:30). Right after the New York close / start of
the Asian session, so non-urgent customer-VM migrations stay out of the trading session. It plans
from the Firestore counters that the salud sweep (`node_health_check`) reconciles, which therefore
runs at **06:00 and 22:00 UTC**: keep a salud sweep right before the defrag if either moves.
Reinstall after a base rebuild:
```
install -m755 run_remotes/neuravps-defrag.py /usr/local/sbin/neuravps-defrag.py
install -m644 base/systemd/neuravps-defrag.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now neuravps-defrag.timer
```
Needs: /etc/firebase-credentials.json + /root/migrate_vms_batch.sh (+migrate_vm.sh).
Kill-switch: Firestore `config/defrag` {enabled, dryRun, maxMovesPerRun} (doc ausente = OFF).
Journal: `journalctl -t neuravps-defrag`; runs en Firestore `defrag_runs/`.
