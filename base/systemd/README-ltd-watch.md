# neuravps-ltd-watch — email when an LTD model is in stock

Operator's ask (2026-10-05): an email when the LTD servers we buy can be ordered,
run from the base, nothing in the cloud.

**Why it is needed:** the Robot webservice API does not offer LTD products at all
(`GET /order/server/product` lists none, `GET /order/server/product/AX102-3-LTD` is
404, and `POST /order/server/transaction` with `test=true` rejects
`product_id=AX102-3-LTD` as invalid). The public web order page
<https://robot.hetzner.com/order> does list them, no login needed: an LTD in stock has
an `Order product` submit button, one out of stock a disabled `Not available` button.
Prices on that page include German VAT (19 %).

**What it does:** every 10 min on **b1 only** (`0000001-BASE`), fetch that page, parse
the product boxes, and email `soporte@neuravps.com`:
- `[NeuraVPS] Stock LTD: <model> disponible` when a watched model becomes orderable
  (once per stock window);
- `[NeuraVPS] Stock LTD: <model> agotado` when it stops being orderable;
- `[NeuraVPS] Vigilante LTD sin datos` after 6 consecutive runs (1 h) unable to read the
  page or find a watched model, then at most once a day while it lasts.

Watched by default: `AX102-3-LTD` (MT5) and `AX162-2-LTD` (SQX); change with `--watch`.
It only reads a public page and sends mail: it cannot order, cancel or touch a node.

## Files
| Repo | On the base |
|---|---|
| `run_remotes/neuravps-ltd-watch.py` | `/usr/local/sbin/neuravps-ltd-watch.py` |
| `base/systemd/neuravps-ltd-watch.{service,timer}` | `/etc/systemd/system/` |
| — | state `/var/lib/neuravps/ltd-watch/state.json` |
| — | SMTP password: reuses `/etc/neuravps/hw-error-scan.env` (mode 600) |

## Install (b1)
```
scp run_remotes/neuravps-ltd-watch.py b1:/usr/local/sbin/ && ssh b1 chmod 755 /usr/local/sbin/neuravps-ltd-watch.py
scp base/systemd/neuravps-ltd-watch.{service,timer} b1:/etc/systemd/system/
ssh b1 'systemctl daemon-reload && systemctl enable --now neuravps-ltd-watch.timer'
```
Test without mail: `python3 /usr/local/sbin/neuravps-ltd-watch.py --dry-run` (does not
save state). Offline: `--html-file page.html --state /tmp/s.json --dry-run`.
