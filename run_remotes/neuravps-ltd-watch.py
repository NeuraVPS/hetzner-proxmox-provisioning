#!/usr/bin/env python3
"""Watch Hetzner's public Robot order page for LTD stock and email soporte@.

Why this exists (2026-10-05): the Robot *webservice* API does not list LTD
products at all (GET /order/server/product has none; GET
/order/server/product/AX102-3-LTD is 404; POST /order/server/transaction with
test=true rejects product_id=AX102-3-LTD as INVALID_INPUT). The automatic
buyer in functions/ltd_procurement.py therefore never saw one: every LTD in the
fleet was bought by hand from the web. The web order page
(https://robot.hetzner.com/order) is public -- no login needed -- and lists
every product with an "Order product" button when it is in stock and a
disabled "Not available" button when it is not. Prices there include German
VAT (19 %).

Operator's ask: an email when an LTD we use is orderable, from the base (no
cloud job). This only reads that page and sends mail; it cannot order
anything.

Behaviour, one run (systemd timer, every 10 min, b1 only):
- watched model goes from not orderable (or unknown) to orderable -> one email;
- watched model goes from orderable to not available -> one short email
  ("se agotó"), so the operator knows the window closed;
- the page cannot be fetched or a watched model is missing from it for
  FAIL_EMAIL_AFTER consecutive runs -> one email, then at most one every
  FAIL_EMAIL_EVERY_H hours while it lasts (the page layout may have changed).
State lives in /var/lib/neuravps/ltd-watch/state.json. SMTP is the same
authenticated Gmail path as neuravps-hw-error-scan (SMTP_PASSWORD in
/etc/neuravps/hw-error-scan.env, mode 600).
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

ORDER_URL = "https://robot.hetzner.com/order"
DEFAULT_WATCH = ["AX102-3-LTD", "AX162-2-LTD"]
DEFAULT_STATE = "/var/lib/neuravps/ltd-watch/state.json"
DEFAULT_ENV_FILE = "/etc/neuravps/hw-error-scan.env"
SMTP_LOGIN = "soporte@neuravps.com"
SMTP_TO = "soporte@neuravps.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
VAT = 1.19
FAIL_EMAIL_AFTER = 6          # consecutive failed runs (= 1 h at 10 min)
FAIL_EMAIL_EVERY_H = 24

ORDERABLE, NOT_AVAILABLE = "orderable", "not_available"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (NeuraVPS ltd-watch; soporte@neuravps.com)",
        "Accept-Language": "en-GB,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(page):
    """{model: {"status", "gross", "productId"}} for every product box."""
    out = {}
    for m in re.finditer(r'<div id="(\d+)" class="box_product">(.*?)</form>', page, re.S):
        pid, body = m.group(1), m.group(2)
        t = re.search(r'<td class="title">(.*?)</td>', body, re.S)
        if not t:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip()
        model = title.split()[-1] if title else ""
        price = re.search(r'max\. per month:</td>\s*<td>from <span class="price[^"]*">€\s*([\d.,]+)', body)
        if re.search(r'value=["\']Not available["\']', body):
            status = NOT_AVAILABLE
        elif re.search(r"type=['\"]submit['\"]\s+value=['\"]Order product['\"]", body) or \
                re.search(r"value=['\"]Order product['\"]", body):
            status = ORDERABLE
        else:
            continue
        gross = float(price.group(1).replace(",", "")) if price else None
        out[model] = {"status": status, "gross": gross, "productId": pid, "title": title}
    return out


def load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


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
        log(f"ltd-watch: cannot read {env_file}: {e}")
    return None


def send_email(subject, body, env_file, dry_run):
    if dry_run:
        print(f"--- DRY RUN email ---\nSubject: {subject}\n\n{body}\n---")
        return True
    password = load_smtp_password(env_file)
    if not password:
        log(f"ltd-watch: SMTP_PASSWORD not available ({env_file}) — email NOT sent")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = "Soporte NeuraVPS <soporte@neuravps.com>"
        msg["To"] = SMTP_TO
        msg["Subject"] = subject
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.starttls()
        server.login(SMTP_LOGIN, password)
        server.sendmail(SMTP_LOGIN, [SMTP_TO], msg.as_string())
        server.quit()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"ltd-watch: email send failed: {e}")
        return False


def price_line(info):
    if not info.get("gross"):
        return "precio no leído"
    g = info["gross"]
    return f"{g:.2f} €/mes con IVA alemán (≈ {g / VAT:.2f} € sin IVA)"


def summary(seen, watch):
    lines = []
    for model in watch:
        info = seen.get(model)
        if not info:
            lines.append(f"- {model}: no aparece en la página")
        else:
            st = "DISPONIBLE" if info["status"] == ORDERABLE else "no disponible"
            lines.append(f"- {model}: {st} — {price_line(info)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", nargs="+", default=DEFAULT_WATCH)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--html-file", help="parse this file instead of fetching (tests)")
    ap.add_argument("--base-id", default=os.uname().nodename)
    ap.add_argument("--dry-run", "--no-send", action="store_true")
    args = ap.parse_args()

    now = time.time()
    stamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    state = load_state(args.state)
    models = state.setdefault("models", {})

    error = None
    seen = {}
    try:
        page = open(args.html_file, encoding="utf-8").read() if args.html_file else fetch(ORDER_URL)
        seen = parse(page)
        missing = [m for m in args.watch if m not in seen]
        if missing:
            error = f"la página no muestra {', '.join(missing)} (¿ha cambiado el diseño?)"
    except Exception as e:  # noqa: BLE001
        error = f"no se pudo leer {ORDER_URL}: {e}"

    if error:
        state["consecutiveFailures"] = int(state.get("consecutiveFailures") or 0) + 1
        state["lastError"] = error
        log(f"ltd-watch: {error} (fallo {state['consecutiveFailures']} seguido)")
        last_mail = float(state.get("lastFailureEmailAt") or 0)
        if (state["consecutiveFailures"] >= FAIL_EMAIL_AFTER
                and now - last_mail >= FAIL_EMAIL_EVERY_H * 3600):
            body = (f"El vigilante de servidores LTD de {args.base_id} lleva "
                    f"{state['consecutiveFailures']} comprobaciones seguidas sin poder leer la "
                    f"página de pedidos de Robot.\n\nÚltimo error: {error}\n\n"
                    f"Mientras dure, no avisará de stock. Revisar "
                    f"/usr/local/sbin/neuravps-ltd-watch.py (journalctl -u neuravps-ltd-watch).")
            if send_email(f"[NeuraVPS] Vigilante LTD sin datos ({args.base_id})", body,
                          args.env_file, args.dry_run):
                state["lastFailureEmailAt"] = now
    else:
        state["consecutiveFailures"] = 0
        state["lastError"] = None

    for model in args.watch:
        info = seen.get(model)
        if not info:
            continue
        prev = models.get(model) or {}
        prev_status = prev.get("status")
        cur = {"status": info["status"], "gross": info["gross"], "lastSeen": now,
               "since": prev.get("since") if prev_status == info["status"] else now}
        if info["status"] == ORDERABLE and prev_status != ORDERABLE:
            body = (f"{model} está DISPONIBLE en Hetzner ({stamp}).\n\n"
                    f"Precio: {price_line(info)}; instalación aparte.\n"
                    f"Pedir en: {ORDER_URL} (producto {info['productId']}, '{info['title']}').\n\n"
                    f"El comprador automático NO puede comprarlo: la API de Robot no ofrece "
                    f"modelos LTD. Hay que pedirlo desde la web.\n\n"
                    f"Estado de los modelos vigilados:\n{summary(seen, args.watch)}\n")
            if send_email(f"[NeuraVPS] Stock LTD: {model} disponible", body,
                          args.env_file, args.dry_run):
                cur["notifiedAt"] = now
            else:
                cur["status"] = prev_status  # retry the alert on the next run
        elif info["status"] == NOT_AVAILABLE and prev_status == ORDERABLE:
            hours = (now - float(prev.get("since") or now)) / 3600
            body = (f"{model} ya no está disponible en Hetzner ({stamp}). "
                    f"Estuvo disponible unas {hours:.1f} h desde el aviso.\n\n"
                    f"Estado de los modelos vigilados:\n{summary(seen, args.watch)}\n")
            send_email(f"[NeuraVPS] Stock LTD: {model} agotado", body,
                       args.env_file, args.dry_run)
        else:
            cur["notifiedAt"] = prev.get("notifiedAt")
        models[model] = cur

    state["lastRunAt"] = now
    if not args.dry_run:
        save_state(args.state, state)
    print(summary(seen, args.watch) if seen else f"ltd-watch: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
