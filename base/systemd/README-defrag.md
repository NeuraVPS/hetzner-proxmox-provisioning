# Daily defrag (runs on a BASE, currently b0)
Reinstall after a base rebuild:
```
install -m755 run_remotes/neuravps-defrag.py /usr/local/sbin/neuravps-defrag.py
install -m644 base/systemd/neuravps-defrag.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now neuravps-defrag.timer
```
Needs: /etc/firebase-credentials.json + /root/migrate_vms_batch.sh (+migrate_vm.sh).
Kill-switch: Firestore `config/defrag` {enabled, dryRun, maxMovesPerRun} (doc ausente = OFF).
Journal: `journalctl -t neuravps-defrag`; runs en Firestore `defrag_runs/`.
