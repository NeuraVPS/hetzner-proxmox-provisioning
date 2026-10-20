#!/usr/bin/env python3
"""Watch Hetzner's server auction (Serverbörse) for boxes useful for SQX and email soporte@ and ivan@.

Why this exists (2026-10-20): operator's capacity policy -- no price rises,
no non-LTD AX162 unless critical, buy every AX162-2-LTD and "watch the
auction and buy what is useful for SQX" (after testing one with real SQX).
This only reads Hetzner's public auction data and sends mail: it cannot order,
cancel or touch a node.

Source: https://www.hetzner.com/_resources/app/data/app/live_data_sb.json
(the one the public page https://www.hetzner.com/sb/ loads; the older
live_data_sb_EUR.json is 404 since at least 20/10/2026). Same ids and
prices as Robot's GET /order/server_market/product, but without credentials,
so the watcher never spends the bases' Robot request budget (the failover
needs it). Prices are monthly, without VAT, auction setup is 0 EUR.

How many SQX VMs fit (estimate; the box must still be tested with real SQX):
- CPU: SQX Build is CPU-bound (AX42 pilot, 16/10). rel = PassMark CPU Mark /
  REF_BENCH (AX162 = EPYC 9454P, ~96,000) x class factor (desktop chips 0.66,
  calibrated on the AX42 pilot: PassMark said 0.29, SQX measured 0.194;
  server chips 0.9, uncalibrated). The AX162 runs 14 VMs; another box
  keeps >= 90 % per-VM throughput up to floor(14 x rel / 0.90) VMs (the
  EX131 rule: box at 84 % -> 13 VMs).
- RAM: sum of floors <= 0.85 x RAM (capacityGates); mean floor of the SQX
  sales mix = 17.9 GB (12 weeks of sign-ups to 14/10: A 87, B 29, C 46,
  D 23, E 48 with floors 8.2/9.7/15.5/23.1/40.2 GB).
- Disk: thin provisioning <= 1.8 x pool; mean plan disk 238 GB; the pool is
  a mirror of the two largest NVMe (x 0.9 for TB -> usable).
EUR per VM = monthly price / min(cpu, ram, disk). References: AX162-2-LTD
23 EUR/VM, AX162-2 (regular) 63 EUR/VM.

Behaviour, one run (systemd timer, every 10 min, b1 only):
- a box that meets every criterion and was not notified -> it goes in the
  email; a notified box whose price dropped -> it goes in the email again;
  one email per run at most, with the calculation for each box;
- the data cannot be fetched or parsed for FAIL_EMAIL_AFTER consecutive
  runs -> "sin datos" email to soporte@, then at most one a day.
State lives in /var/lib/neuravps/auction-watch/state.json. SMTP is the same
authenticated Gmail path as neuravps-hw-error-scan / neuravps-ltd-watch.
"""

import argparse
import json
import math
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

DATA_URL = "https://www.hetzner.com/_resources/app/data/app/live_data_sb.json"
PAGE_URL = "https://www.hetzner.com/sb/"
DEFAULT_STATE = "/var/lib/neuravps/auction-watch/state.json"
DEFAULT_ENV_FILE = "/etc/neuravps/hw-error-scan.env"
SMTP_LOGIN = "soporte@neuravps.com"
SMTP_TO = ["soporte@neuravps.com"]
STOCK_TO = ["soporte@neuravps.com", "ivan@neuravps.com"]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
FAIL_EMAIL_AFTER = 6          # consecutive failed runs (= 1 h at 10 min)
FAIL_EMAIL_EVERY_H = 24

# Criteria (all overridable with --config <json>).
CRITERIA = {
    # Zen 4/5 (EPYC 9xx4/9xx5/4xx4/4xx5, Ryzen 7000/9000) or Zen 3 EPYC (7xx3).
    "cpuRegex": r"EPYC\s+(9\d\d[45]|4\d\d[45]|7\d\d3)|Ryzen\s+(Threadripper\s+(PRO\s+)?7\d\d\d|9\s+(7|9)9\d0X)",
    "minRamGb": 128,
    "minNvme": 2,
    "minNvmeGb": 1920,       # each of the two mirror disks
    "locations": ["FSN1", "HEL1", "NBG1"],
    "maxEurPerVm": 35.0,
    "requireEcc": False,     # shown in the email; ECC is preferred, not required
}
MODEL = {
    "refBench": 96000,       # PassMark CPU Mark, EPYC 9454P (AX162)
    "refVms": 14,
    "density": 0.90,
    "desktopFactor": 0.66,
    "serverFactor": 0.90,     # other server chips (Zen 2/3 EPYC, Xeon); Zen 4/5 EPYC = 1.0
    "ramGate": 0.85,
    "meanFloorGb": 17.9,
    "diskGate": 1.8,
    "meanDiskGb": 238,
    "poolEfficiency": 0.9,
    "ltdEurPerVm": 23.0,
    "ax162EurPerVm": 63.0,
}
# PassMark CPU Mark (Hetzner's cpu_benchmark where the auction showed it,
# public PassMark otherwise). A CPU that matches cpuRegex but is not here is
# still emailed, flagged "rendimiento desconocido".
BENCH = {
    "AMD EPYC 9454P": 96000, "AMD EPYC 9354P": 72000, "AMD EPYC 9254": 52000,
    "AMD EPYC 9554P": 106700, "AMD EPYC 9654P": 122000, "AMD EPYC 9124": 36000,
    "AMD EPYC 9455P": 115000, "AMD EPYC 9355P": 85000, "AMD EPYC 9555P": 130000,
    "AMD EPYC 4464P": 42000, "AMD EPYC 4564P": 64000, "AMD EPYC 4584PX": 66000,
    "AMD EPYC 7443P": 57000, "AMD EPYC 7543P": 70000, "AMD EPYC 7713P": 87000,
    "AMD EPYC 7313P": 41000, "AMD EPYC 7413": 53000, "AMD EPYC 7763": 88000,
    "AMD EPYC 7502P": 48724, "AMD EPYC 7401P": 23813,
    "AMD Ryzen 9 7950X": 62500, "AMD Ryzen 9 7950X3D": 62000, "AMD Ryzen 9 9950X": 66000,
    "AMD Ryzen 9 7900": 47000, "AMD Ryzen 7 7700": 34399, "AMD Ryzen 9 5950X": 45895,
    # cpu_benchmark of Robot's GET /order/server_market/product, 20/10/2026:
    "Intel Xeon Gold 5412U": 51599, "Intel Core i9-13900": 45251, "Intel Core i5-13500": 30866,
    "AMD Ryzen 9 3900": 30649, "Intel Xeon W-2295": 30508, "AMD Ryzen Threadripper 2950X": 30191,
    "AMD Ryzen 7 3700X": 22719, "Intel Core i5-12500": 19901, "Intel Core i9-9900K": 18879,
    "Intel Xeon W-2145": 18292, "AMD Ryzen 5 3600": 17769, "AMD Ryzen 7 1700X": 15622,
    "AMD Ryzen 7 PRO 1700X": 15398, "Intel XEON E-2276G": 13803, "Intel XEON E-2176G": 13635,
    "Intel Core i7-8700": 13180, "Intel Xeon E5-1650V3": 10382, "Intel Core i7-7700": 8581,
    "Intel Xeon E3-1275V6": 8564, "Intel Xeon E3-1275v5": 8292, "Intel Core i7-6700": 8048,
    "Intel Xeon E3-1271V3": 7470, "Intel Xeon E3-1270V3": 7277,
}
SERVER_RE = re.compile(r"EPYC|Xeon\s+(Gold|Silver|Platinum|W-|\d)|Threadripper", re.I)
REF_GEN_RE = re.compile(r"EPYC\s+[94]\d\d[45]", re.I)   # Zen 4/5 EPYC, same generation as the AX162


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (NeuraVPS auction-watch; soporte@neuravps.com)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def parse(data):
    """[{id, cpu, ramGb, ecc, nvme[], price, setup, dc, description}] from live_data_sb.json."""
    out = []
    for s in data["server"]:
        hw, det = s["Hardware"], s["Details"]
        out.append({
            "id": int(s["Id"]),
            "cpu": hw["CPU"]["Name"].strip(),
            "ramGb": int(hw["RAM"]["Size"]),
            "ecc": bool(hw["RAM"].get("ecc")),
            "nvme": sorted((int(x) for x in hw["Storage"]["Details"].get("nvme") or []), reverse=True),
            "price": float(s["Prices"]["monthly"]["EUR"]),
            "setup": float(s["Prices"]["setup"]["EUR"]),
            "fixed": bool(s["Prices"].get("fixed")),
            "dc": det["Datacenter"]["Name"],
            "description": det.get("Description") or [],
        })
    if not out:
        raise ValueError("lista de servidores vacía")
    return out


def estimate(box, model):
    bench = BENCH.get(box["cpu"])
    if REF_GEN_RE.search(box["cpu"]):
        factor = 1.0
    elif SERVER_RE.search(box["cpu"]):
        factor = model["serverFactor"]
    else:
        factor = model["desktopFactor"]
    rel = bench / model["refBench"] * factor if bench else None
    n_cpu = math.floor(model["refVms"] * rel / model["density"] + 1e-9) if rel else None
    n_ram = math.floor(box["ramGb"] * model["ramGate"] / model["meanFloorGb"])
    pool = box["nvme"][1] * model["poolEfficiency"] if len(box["nvme"]) >= 2 else 0
    n_disk = math.floor(pool * model["diskGate"] / model["meanDiskGb"])
    limits = {"cpu": n_cpu, "ram": n_ram, "disco": n_disk}
    known = [v for v in limits.values() if v is not None]
    n = min(known) if known else 0
    monthly = box["price"] + box["setup"] / 12
    return {"bench": bench, "factor": factor, "rel": rel, "limits": limits, "vms": n,
            "poolGb": pool, "eurPerVm": monthly / n if n > 0 else None,
            "benchKnown": bench is not None}


def check(box, est, crit):
    """List of failed criteria (empty = useful)."""
    fails = []
    if not re.search(crit["cpuRegex"], box["cpu"], re.I):
        fails.append("CPU")
    if box["ramGb"] < crit["minRamGb"]:
        fails.append("RAM")
    if crit.get("requireEcc") and not box["ecc"]:
        fails.append("ECC")
    big = [x for x in box["nvme"] if x >= crit["minNvmeGb"]]
    if len(big) < crit["minNvme"]:
        fails.append("NVMe")
    if not any(box["dc"].startswith(loc) for loc in crit["locations"]):
        fails.append("ubicación")
    if est["benchKnown"] and (est["eurPerVm"] is None or est["eurPerVm"] > crit["maxEurPerVm"]):
        fails.append("€/VM")
    return fails


def describe(box, est, model):
    lim = est["limits"]
    rel = f"{est['rel'] * 100:.0f} % del AX162" if est["rel"] else "rendimiento desconocido: MEDIR"
    eur = f"{est['eurPerVm']:.1f} €/VM" if est["eurPerVm"] else "€/VM sin calcular"
    if not est["benchKnown"] and est["eurPerVm"]:
        eur += " como mínimo (sin contar la CPU)"
    nv = " + ".join(f"{x / 1000:.2f} TB" for x in box["nvme"]) or "sin NVMe"
    return (
        f"#{box['id']} — {box['cpu']}, {box['ramGb']} GB{' ECC' if box['ecc'] else ' sin ECC'}, NVMe {nv}, "
        f"{box['dc']}\n"
        f"  Precio: {box['price']:.2f} €/mes sin IVA, alta {box['setup']:.2f} €"
        f"{' (precio fijo)' if box['fixed'] else ' (baja con el tiempo)'}\n"
        f"  CPU: PassMark {est['bench'] or '?'} × {est['factor']} / {model['refBench']} = {rel} → "
        f"{lim['cpu'] if lim['cpu'] is not None else '?'} VMs (14 × rel / {model['density']})\n"
        f"  RAM: {box['ramGb']} × {model['ramGate']} / {model['meanFloorGb']} GB de suelo medio → {lim['ram']} VMs; "
        f"disco: {est['poolGb']:.0f} GB × {model['diskGate']} / {model['meanDiskGb']} GB → {lim['disco']} VMs\n"
        f"  Caben ≈{est['vms']} VMs de la mezcla SQX → {eur} "
        f"(LTD {model['ltdEurPerVm']:.0f} €, AX162-2 {model['ax162EurPerVm']:.0f} €)\n"
        f"  Detalle: {'; '.join(box['description'])}\n"
    )


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_smtp_password(env_file):
    try:
        with open(env_file) as fh:
            for line in fh:
                if line.startswith("SMTP_PASSWORD="):
                    return line.split("=", 1)[1].strip()
    except OSError as e:
        log(f"auction-watch: cannot read {env_file}: {e}")
    return None


def send_email(subject, body, env_file, dry_run, to=SMTP_TO):
    if dry_run:
        print(f"--- DRY RUN email ---\nTo: {', '.join(to)}\nSubject: {subject}\n\n{body}\n---")
        return True
    password = load_smtp_password(env_file)
    if not password:
        log(f"auction-watch: SMTP_PASSWORD not available ({env_file}) — email NOT sent")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = "Soporte NeuraVPS <soporte@neuravps.com>"
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.starttls()
        server.login(SMTP_LOGIN, password)
        server.sendmail(SMTP_LOGIN, list(to), msg.as_string())
        server.quit()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"auction-watch: email send failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--config", help="JSON con claves 'criteria' y/o 'model' que sustituyen a las de serie")
    ap.add_argument("--json-file", help="parse this file instead of fetching (tests)")
    ap.add_argument("--base-id", default=os.uname().nodename)
    ap.add_argument("--dry-run", "--no-send", action="store_true")
    ap.add_argument("--report", action="store_true", help="list every box with >= minRamGb and why it fails")
    args = ap.parse_args()

    crit, model = dict(CRITERIA), dict(MODEL)
    if args.config:
        cfg = load_json(args.config, {})
        crit.update(cfg.get("criteria") or {})
        model.update(cfg.get("model") or {})
        BENCH.update(cfg.get("bench") or {})

    now = time.time()
    stamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    state = load_json(args.state, {})
    notified = state.setdefault("notified", {})

    try:
        data = load_json(args.json_file, None) if args.json_file else fetch(DATA_URL)
        boxes = parse(data)
    except Exception as e:  # noqa: BLE001
        error = f"no se pudo leer {DATA_URL}: {e}"
        state["consecutiveFailures"] = int(state.get("consecutiveFailures") or 0) + 1
        state["lastError"] = error
        log(f"auction-watch: {error} (fallo {state['consecutiveFailures']} seguido)")
        last_mail = float(state.get("lastFailureEmailAt") or 0)
        if (state["consecutiveFailures"] >= FAIL_EMAIL_AFTER
                and now - last_mail >= FAIL_EMAIL_EVERY_H * 3600):
            body = (f"El vigilante de la subasta de Hetzner de {args.base_id} lleva "
                    f"{state['consecutiveFailures']} comprobaciones seguidas sin poder leer los datos.\n\n"
                    f"Último error: {error}\n\nMientras dure, no avisará de cajas útiles. Revisar "
                    f"/usr/local/sbin/neuravps-auction-watch.py (journalctl -u neuravps-auction-watch); "
                    f"la URL sale de {PAGE_URL} (busca live_data en serverboerse.js).")
            if send_email(f"[NeuraVPS] Vigilante subasta sin datos ({args.base_id})", body,
                          args.env_file, args.dry_run):
                state["lastFailureEmailAt"] = now
        state["lastRunAt"] = now
        if not args.dry_run:
            save_state(args.state, state)
        return 0

    state["consecutiveFailures"] = 0
    state["lastError"] = None

    useful, alerts, report = [], [], []
    for box in boxes:
        est = estimate(box, model)
        fails = check(box, est, crit)
        if args.report and box["ramGb"] >= crit["minRamGb"]:
            report.append((est["eurPerVm"] or 9e9, box, est, fails))
        if fails:
            continue
        useful.append(box["id"])
        prev = notified.get(str(box["id"]))
        if prev is None:
            alerts.append(("NUEVA", box, est))
        elif box["price"] < float(prev["price"]) - 0.005:
            alerts.append((f"BAJA de {float(prev['price']):.2f} €", box, est))

    if alerts:
        alerts.sort(key=lambda a: (not a[2]["benchKnown"], a[2]["eurPerVm"] or 9e9))
        body = (f"Subasta de Hetzner ({stamp}): {len(alerts)} caja(s) que cumplen los criterios SQX "
                f"(CPU Zen 4/5 o Zen 3 EPYC, ≥{crit['minRamGb']} GB, ≥{crit['minNvme']}×"
                f"{crit['minNvmeGb'] / 1000:.2f} TB NVMe, {'/'.join(crit['locations'])}, "
                f"≤{crit['maxEurPerVm']:.0f} €/VM).\n\n")
        for tag, box, est in alerts:
            body += f"[{tag}] " + describe(box, est, model) + "\n"
        body += (f"El número de VMs es una ESTIMACIÓN (PassMark): antes de comprar varias, probar una con SQX real "
                 f"(método EX131). Se puede pedir por la API de Robot: POST /order/server_market/transaction "
                 f"product_id=<id> (comprobado con test=true el 20/10/2026), o en {PAGE_URL}.\n"
                 f"Este vigilante no compra nada.\n")
        best = alerts[0][1]
        eur = alerts[0][2]["eurPerVm"]
        subject = (f"[NeuraVPS] Subasta: {len(alerts)} caja(s) útil(es) para SQX — mejor #{best['id']} "
                   f"{best['cpu']} " + (f"{eur:.1f} €/VM" if alerts[0][2]["benchKnown"] else "(CPU sin estimar)"))
        if send_email(subject, body, args.env_file, args.dry_run, to=STOCK_TO):
            for _, box, _ in alerts:
                notified[str(box["id"])] = {"price": box["price"], "at": now, "cpu": box["cpu"]}

    # Forget boxes that left the auction (sold or withdrawn): if one comes back it is news.
    present = {str(b["id"]) for b in boxes}
    for key in [k for k in notified if k not in present]:
        del notified[key]

    state["lastRunAt"] = now
    state["lastCount"] = len(boxes)
    state["lastUseful"] = useful
    if not args.dry_run:
        save_state(args.state, state)

    print(f"auction-watch: {len(boxes)} cajas en la subasta, {len(useful)} cumplen, {len(alerts)} avisos")
    if args.report:
        for _, box, est, fails in sorted(report, key=lambda r: r[0]):
            verdict = "CUMPLE" if not fails else "falla " + ",".join(fails)
            print(f"\n[{verdict}] " + describe(box, est, model).rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
