#!/usr/bin/env python3
"""neuravps-hw-error-scan — daily sweep of the netconsole capture for signs a
node's hardware is failing, run on each BASE (b0 AND b1, unlike the
single-base daemons: each base only ever sees the nodes that netconsole
each other) via a systemd timer.

Why this exists (operator, 2026-09-15): node 0000235-AX162-2-LTD died with
its NVMe logging kernel errors in /var/log/netconsole/<ipv6>.log for TEN DAYS
before it failed — nobody was reading the file. netconsole is the one source
that survives a hard hang (it is a raw UDP kernel console feed, independent
of the guest agent / SSH / anything in userspace on the node), so it is the
right place to watch. See docs/HW_ERROR_SCAN.md for the incident numbers.

Baseline swept live across ~200 files on both bases the day this was built:
ZERO background noise. Only the dead node and one node whose errors were all
from a boot before its hardware swap matched. That means no threshold is
needed — any error line found in the node's CURRENT boot is real signal.

THE TRAP (and the reason this isn't a one-line grep): the file is
cumulative across node reboots, and every kernel timestamp in it
(`[12345.678901]`) is uptime *of whatever boot logged that line*, which
resets to ~0 at every reboot. A node that had a bad NVMe, got power-cycled,
and now boots clean will have thousands of old error lines sitting in the
file forever. Blindly grepping the whole file (or trusting the file's mtime)
misattributes those to "now" — that's almost how node 0000207 got flagged
here by mistake: every one of its matching lines predated a hardware swap,
and the tell was that the LAST line in the file was a fresh
"netconsole: network logging started". So: only count lines after the LAST
boot marker in the file. If a node keeps re-erroring after each reboot, this
still catches it — a fresh run of errors starts accumulating right after its
own marker and shows up on the very next daily scan.

Kept deliberately dumb, per the operator's ask ("una sola cosa sencilla, no
un sistema"): no Firestore kill-switch, no state file, no dedup across runs.
Every day it either finds current-boot errors and emails, or it finds
nothing and says nothing. If a node keeps erroring for ten days, it should
get ten emails, growing — that IS the point (that's exactly the pattern that
went unread for 0000235).
"""
import argparse
import glob
import ipaddress
import json
import os
import re
import smtplib
import socket
import sys
from email.mime.text import MIMEText

# Same four patterns the operator used for the manual fleet sweep that found
# zero background noise. Do not add patterns here without re-running that
# sweep across the fleet first — that's what makes "any hit is real" true.
ERROR_RE = re.compile(r"Hardware Error|aer_status|RxErr|MC[0-9]+_STATUS")
BOOT_MARKER_RE = re.compile(r"netconsole: network logging started")
PCI_DEVICE_RE = re.compile(r"\b(0000:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])\b")
MC_STATUS_RE = re.compile(r"\b(MC[0-9]+_STATUS)\b")

DEFAULT_LOG_DIR = "/var/log/netconsole"
DEFAULT_NODES_MAP = "/var/lib/base-nat/pve_nodes.json"  # nodeId -> ipv6, kept fresh by sync-base-nat.py; reversed here
DEFAULT_ENV_FILE = "/etc/neuravps/hw-error-scan.env"
SAMPLE_LINES = 5

SMTP_LOGIN = "soporte@neuravps.com"
SMTP_TO = "soporte@neuravps.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def load_ip_to_node(path: str) -> dict | None:
    """nodeId -> ipv6 map (same file base-nat already keeps fresh) reversed
    to ipv6 -> nodeId. An unreadable/missing map returns None so the caller
    scans every log; a valid map is authoritative for live membership."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("node map is not an object")
        if not data:
            raise ValueError("node map is empty")
        ip_to_node = {}
        for node_id, address in data.items():
            if not isinstance(node_id, str) or not node_id.strip():
                raise ValueError("node map has an empty or non-string node ID")
            if not isinstance(address, str):
                raise ValueError(f"node map address for {node_id!r} is not a string")
            parsed = ipaddress.ip_address(address)
            if parsed.version != 6:
                raise ValueError(f"node map address for {node_id!r} is not IPv6")
            ip_to_node[address] = node_id
        return ip_to_node
    except Exception as e:  # noqa: BLE001
        # Do not turn a temporary map-read failure into a blind spot.  In that
        # exceptional case preserve the old behavior and inspect every log.
        log(f"hw-error-scan: node map unavailable ({path}): {e} — scanning all logs")
        return None


def current_boot_lines(lines: list[str]) -> list[str]:
    """Only the lines from (and after) the LAST boot marker. If the file
    has no marker at all (shouldn't happen in steady state — netconsole logs
    it on every boot — but a brand new file mid-stream could lack one), the
    whole file is treated as the current boot: we cannot prove any of it is
    stale, so we do not suppress it."""
    last = None
    for i, line in enumerate(lines):
        if BOOT_MARKER_RE.search(line):
            last = i
    if last is None:
        return lines
    return lines[last:]


def scan_file(path: str) -> dict | None:
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as e:  # noqa: BLE001
        log(f"hw-error-scan: cannot read {path}: {e}")
        return None

    boot_lines = current_boot_lines(lines)
    errors = [l.rstrip("\n") for l in boot_lines if ERROR_RE.search(l)]
    if not errors:
        return None

    devices: list[str] = []
    for l in errors:
        for m in PCI_DEVICE_RE.findall(l):
            if m not in devices:
                devices.append(m)
        for m in MC_STATUS_RE.findall(l):
            if m not in devices:
                devices.append(m)

    return {
        "count": len(errors),
        "devices": devices,
        "sample": errors[-SAMPLE_LINES:],
    }


def build_report(log_dir: str, nodes_map_path: str) -> list[dict]:
    ip_to_node = load_ip_to_node(nodes_map_path)
    findings = []
    for path in sorted(glob.glob(os.path.join(log_dir, "*.log"))):
        ip = os.path.basename(path)[: -len(".log")]
        # Netconsole files outlive the host that wrote them.  Once the live
        # BASE inventory no longer contains that IPv6, it cannot describe a
        # current fleet node and must not page hardware operations.  The map
        # is usable only after full non-empty validation; otherwise None makes
        # this a conservative scan of every log.
        if ip_to_node is not None and ip not in ip_to_node:
            log(f"hw-error-scan: skipping retired/unmapped netconsole log {ip}")
            continue
        result = scan_file(path)
        if result is None:
            continue
        result["ip"] = ip
        result["node"] = ip_to_node.get(ip, ip) if ip_to_node is not None else ip
        findings.append(result)
    return findings


def format_email_body(findings: list[dict], base_id: str) -> str:
    parts = [
        f"Barrido diario de netconsole en {base_id}: {len(findings)} nodo(s) con "
        "errores de hardware en su arranque ACTUAL.",
        "",
    ]
    for f in findings:
        parts.append(f"== {f['node']} ({f['ip']}) ==")
        parts.append(f"  Errores (arranque actual): {f['count']}")
        parts.append(f"  Dispositivo(s): {', '.join(f['devices']) if f['devices'] else '(no identificado)'}")
        parts.append(f"  Últimas {len(f['sample'])} líneas:")
        for line in f["sample"]:
            parts.append(f"    {line}")
        parts.append("")
    parts.append(
        "-- \nneuravps-hw-error-scan, run_remotes/neuravps-hw-error-scan.py "
        f"(base {base_id})"
    )
    return "\n".join(parts)


def load_smtp_password(env_file: str) -> str | None:
    try:
        with open(env_file) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("SMTP_PASSWORD="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:  # noqa: BLE001
        log(f"hw-error-scan: cannot read {env_file}: {e}")
    return None


def send_email(subject: str, body: str, env_file: str) -> bool:
    password = load_smtp_password(env_file)
    if not password:
        log(f"hw-error-scan: SMTP_PASSWORD not available ({env_file}) — email NOT sent")
        return False
    try:
        msg = MIMEText(body, "plain")
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
        log(f"hw-error-scan: email send failed: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--nodes-map", default=DEFAULT_NODES_MAP)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--base-id", default=os.uname().nodename)
    ap.add_argument("--dry-run", action="store_true", help="print the report, never send email")
    ap.add_argument("--no-send", action="store_true", help="alias for --dry-run")
    args = ap.parse_args()
    dry_run = args.dry_run or args.no_send

    findings = build_report(args.log_dir, args.nodes_map)

    if not findings:
        log("hw-error-scan: clean sweep, no current-boot hardware errors — silent")
        return 0

    subject = f"[NeuraVPS] Errores de hardware en {len(findings)} nodo(s) ({args.base_id})"
    body = format_email_body(findings, args.base_id)

    log(f"hw-error-scan: {len(findings)} node(s) flagged: "
        + ", ".join(f"{f['node']}={f['count']}" for f in findings))

    if dry_run:
        print(f"SUBJECT: {subject}\n")
        print(body)
        return 0

    ok = send_email(subject, body, args.env_file)
    if ok:
        log("hw-error-scan: email sent")
        return 0
    log("hw-error-scan: email send FAILED (see above) — exiting non-zero so it's visible in journalctl/systemd status")
    return 1


if __name__ == "__main__":
    sys.exit(main())
