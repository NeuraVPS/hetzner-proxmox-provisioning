# neuravps-auction-watch — email when the Hetzner server auction has a box useful for SQX

Operator's policy (2026-10-20): no price rises, no regular AX162 unless critical; buy every
AX162-2-LTD and **watch the server auction (Serverbörse) and buy what is useful for SQX**, after
testing one with real SQX. This watcher only reads public data and sends mail: it cannot order,
cancel or touch a node.

**Source:** `https://www.hetzner.com/_resources/app/data/app/live_data_sb.json`, the file the public
page <https://www.hetzner.com/sb/> loads (`serverboerse.js`). The older `live_data_sb_EUR.json` is
404. Same ids and prices (monthly, without VAT, setup 0) as Robot's
`GET /order/server_market/product`, but needs no credentials, so it never spends the bases' Robot
request budget. Auction boxes **can** be ordered through the Robot API
(`POST /order/server_market/transaction product_id=<id>`; checked with `test=true` on 20/10/2026 →
201, transaction created as `cancelled`), unlike the LTD models.

**Criteria** (defaults; override with `--config file.json` holding `criteria`, `model`, `bench`):
CPU Zen 4/5 (EPYC 9xx4/9xx5/4xx4/4xx5, Ryzen 9 7950X/9950X…) or Zen 3 EPYC (7xx3); ≥128 GB RAM
(ECC shown, not required); ≥2 NVMe of ≥1.92 TB (mirror); FSN1/HEL1/NBG1; **≤35 €/VM**.

**VMs per box (estimate):** min of
- CPU: `floor(14 × rel / 0.90)`, `rel` = PassMark / 96,000 (EPYC 9454P, AX162) × class factor
  (Zen 4/5 EPYC 1.0; other server chips 0.9; desktop chips 0.66, calibrated on the AX42 pilot);
- RAM: `0.85 × RAM / 17.9 GB` (mean floor of the SQX sales mix);
- disk: `1.8 × pool / 238 GB` (pool = smaller of the two largest NVMe × 0.9).

€/VM = price / VMs, compared with AX162-2-LTD 23 € and AX162-2 63 €. A CPU that matches the
regex but has no PassMark in the table is still emailed, flagged "rendimiento desconocido".

**Emails:** `[NeuraVPS] Subasta: N caja(s) útil(es) para SQX …` to `soporte@` and `ivan@` when a box
meets every criterion for the first time or its price drops (with the calculation per box);
`[NeuraVPS] Vigilante subasta sin datos` (only `soporte@`) after 6 failed runs, then once a day.

## Files
| Repo | On the base |
|---|---|
| `run_remotes/neuravps-auction-watch.py` | `/usr/local/sbin/neuravps-auction-watch.py` |
| `base/systemd/neuravps-auction-watch.{service,timer}` | `/etc/systemd/system/` |
| — | state `/var/lib/neuravps/auction-watch/state.json` |
| — | SMTP password: reuses `/etc/neuravps/hw-error-scan.env` (mode 600) |

## Install (b1 only)
```
scp run_remotes/neuravps-auction-watch.py b1:/usr/local/sbin/ && ssh b1 chmod 755 /usr/local/sbin/neuravps-auction-watch.py
scp base/systemd/neuravps-auction-watch.{service,timer} b1:/etc/systemd/system/
ssh b1 'systemctl daemon-reload && systemctl enable --now neuravps-auction-watch.timer'
```
Test without mail: `python3 /usr/local/sbin/neuravps-auction-watch.py --dry-run --report` (does not
save state; `--report` lists every ≥128 GB box and why it fails). Offline: `--json-file data.json`.
