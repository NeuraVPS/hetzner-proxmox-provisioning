#!/usr/bin/env python3
#NESVER=1
"""neuravps-explorer-sweep — daily fleet sweep that closes orphaned
`explorer.exe /factory,{CLSID} -Embedding` hosts on Windows guests (runs once
daily on a BASE, currently b0 — see neuravps-defrag.py for the same
single-instance model).

THE LEAK (measured 2026-09-12, see
memory/neuravps-explorer-factory-fuga-ram.md — read that before touching this
file): when a CUSTOMER opens a folder from a program that is not the desktop
Explorer (almost always MetaTrader -> File -> Open Data Folder), Windows
spins up a `C:\\WINDOWS\\explorer.exe /factory,{CLSID} -Embedding` host via
svchost/DCOM in the CUSTOMER's own interactive session (never session 0, so
this is never our own automation). The window closes; the host never exits.
It sits there for weeks (median age 36 days, max 175) at ~45 MB commit each.
Sampled at 40% of VMs affected fleet-wide, ~9,200 processes, ~227 GB
resident, ~408 GB private commit. This script is the remedy the memory note
proposed and validated: the same three-condition filter, run daily instead
of by hand.

THE FILTER IS NOT ONE TO IMPROVE. Do not generalize it, do not drop a
condition, do not touch it without re-reading the memory note and getting
the operator's sign-off. All three conditions matter:
  * the literal CLSID {75dff2b7-6936-4c06-a8bb-676a7b00b24b}
    (CLSID_SeparateMultipleProcessExplorerHost) — other /factory hosts exist
    with a different CLSID and must never be touched;
  * -Embedding — part of the same validated signature;
  * CreationDate < now-24h — the ONLY thing that turns "almost certainly
    safe" into "safe": the one conceivable harm is closing a folder window
    the customer just opened, and 519/519 processes measured in the sample
    were older than 24h (median 36 DAYS). The real desktop shell never
    matches: it never carries /factory, and there is exactly one per
    session.

WHY A SIBLING DAEMON, NOT INSIDE welcome-boost
welcome-boost detects RDP login (conntrack poll on the BASE) and restores a
VM's balloon target over one fast SSH round-trip (`qm set` + one HMP
command). This sweep needs a completely different execution shape: walk
EVERY running Windows guest in the fleet once a day, each requiring its own
`qm guest exec` PowerShell round-trip (hundreds of ms to a few seconds,
sometimes more under load — see the CPU-saturation notes in
neuravps-egresscheck.py). Bolting that onto welcome-boost's per-login
critical path would (a) add multi-second latency to a mechanism whose whole
point is to react fast at the exact moment of login, and (b) mix two
independently risky responsibilities — "give this VM back its RAM" and "kill
a customer-session process on it" — in one already-fragile, carefully-tuned
daemon. welcome-boost is also scoped to AX162/AX102 dynamic-RAM guests only
(EX44/VPS-E guests are explicitly excluded there: "no dynamic RAM policy");
this leak affects all three families, so gating on welcome-boost's
eligibility would silently under-cover VPS-E. The daily sweep alone already
gives complete, uniform, login-independent coverage — a login hook would
only shave hours off cleanup for customers who happen to RDP in that day,
which does not justify entangling it with welcome-boost. This can be
revisited later as a small *additive* hook (never inside welcome-boost's own
balloon logic) if the operator wants faster convergence for active sessions.

HOW IT REACHES A GUEST
`ssh root@<node> "qm guest exec <vmid> --timeout T -- powershell -NoProfile
-EncodedCommand <b64 utf-16le>"` — no guest credentials, same pattern as
neuravps-egresscheck.py's `por_agente` and base/mt_portable_optout_sweep.py's
`guest_exec`. One SSH hop per VM, parallelized with a thread pool (see
WORKERS) the same way egresscheck parallelizes its per-VM probes.

WHY EVERY RESULT MUST CARRY OUR OWN MARKER (not a hypothetical — reproduced
live 2026-09-12): `qm guest exec` on a VM that ALSO has another probe
running against it concurrently (egresscheck's own agent probe, in this
case) can return that OTHER probe's `out-data` instead of ours — a real
qemu-ga exec-status cross-talk, reproduced 1-of-3 tries on the same VM in
manual testing. If this script trusted "exitcode 0, valid JSON" as success,
that cross-talk would silently read as "0 orphans found" and mask a VM with
dozens of them. Every result is required to contain the literal
`EXPLORERSWEEP:` marker before its payload is trusted; anything else —
wrong marker, no marker, malformed JSON, a bare pid (guest-side timeout),
"guest agent is not running" — is treated as UNKNOWN and the VM is skipped
and logged, never assumed clean and never retried within the same run. Fail
closed, exactly as asked: a VM we are not sure about is a VM we leave alone.

GUARDS
  * kill-switch: Firestore `config/explorerSweep`
    {enabled, dryRun, maxVmsPerRun, workers}. Missing doc or enabled!=true =
    DISABLED (fail-safe, same convention as every other daemon here).
  * dryRun (bool): when true, EVERY candidate only counts orphans — the
    kill branch of the PowerShell payload never runs anywhere. Deploy with
    dryRun:true first; only flip it once the counts in explorer_sweep_runs/
    look sane.
  * maxVmsPerRun (int, default DEFAULT_MAX_VMS_PER_RUN): hard cap on how
    many VMs receive the KILL-enabled payload in one run — the blast-radius
    limiter asked for. Counting (dryRun-style telemetry) still happens for
    every candidate regardless of the cap, because Get-CimInstance alone is
    read-only and safe; only the Stop-Process branch is capped. VMs beyond
    the cap are logged and picked up by rotation on a later run — see
    ROTATION below. This mirrors neuravps-defrag.py's maxMovesPerRun, scaled
    to a much lower-risk per-unit action (closing a background process,
    validated with zero errors and zero customer-visible impact across
    every VM tested, vs. defrag's VM migrations which freeze the guest).
  * flock (LOCK_FILE): two overlapping runs can never happen.
  * fail-closed per VM: any SSH/agent/parse failure, or output without our
    marker, skips that VM for this run and is logged with a reason. No
    retries within a run — a stuck/loaded box gets another try tomorrow.
  * never touches anything but matched explorer.exe hosts. No restarts, no
    logoffs, no other process, no registry, nothing else in this file talks
    to a guest.

ROTATION
Candidates are shuffled with a seed derived from today's UTC date (stable
within the run, different every day) before the maxVmsPerRun cap is applied,
so a persistent backlog above the cap does not always starve the same tail
of vmids — everyone gets a turn within a few days.

JOURNAL
Every real run (kill-switch enabled) writes one summary doc to
`explorer_sweep_runs/<YYYYMMDD-HHMM>`: counts, cap state, and a `perVm` map
containing ONLY the VMs that had something to report (found>0 or an error) —
keeping the doc small while still being fully auditable. Full detail also
goes to syslog (`journalctl -t neuravps-explorer-sweep`).
"""
import base64
import fcntl
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
NODES_FILE = os.environ.get("PVE_NODES_FILE", "/var/lib/base-nat/pve_nodes.json")
LOCK_FILE = "/run/neuravps-explorer-sweep.lock"

# CLSID_SeparateMultipleProcessExplorerHost. Literal on purpose — see the
# module docstring. Never widen this to a bare '/factory,'.
CLSID = "{75dff2b7-6936-4c06-a8bb-676a7b00b24b}"
MARKER = "EXPLORERSWEEP:"

AGENT_TIMEOUT = 40           # per-VM qm guest exec timeout (seconds)
WORKERS = 24                 # matches neuravps-egresscheck.py's proven value
# First backlog is ~40% of ~1900 VMs (~760). This clears it over a handful of
# daily runs while bounding day-one blast radius to well under half the
# fleet even if the filter turned out to be wrong somewhere. Raise via
# config/explorerSweep.maxVmsPerRun once the dryRun counts and the first real
# runs look sane.
DEFAULT_MAX_VMS_PER_RUN = 250

# The exact validated three-condition filter from
# memory/neuravps-explorer-factory-fuga-ram.md, wrapped so ONE template
# produces both the dry-run (count-only) and real (kill) payload — the
# filter text itself exists exactly once in this file.
_PS_TEMPLATE = r'''
$ErrorActionPreference='SilentlyContinue'
$doKill = __DO_KILL__
$clsid = '__CLSID__'
$cutoff = (Get-Date).AddHours(-24)
$victims = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" |
  Where-Object { $_.CommandLine -like "*/factory,$clsid*" -and
                 $_.CommandLine -like '*-Embedding*' -and
                 $_.CreationDate -lt $cutoff }
$found = @($victims).Count
$killed = 0
$errors = @()
if ($doKill) {
  foreach ($p in $victims) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $killed++ }
    catch { $errors += "$($p.ProcessId):$($_.Exception.Message)" }
  }
}
$mode = if ($doKill) { 'kill' } else { 'count-only' }
$r = @{ found = $found; killed = $killed; errors = $errors; mode = $mode } |
  ConvertTo-Json -Compress
Write-Output "''' + MARKER + r'''$r"
'''


def log(msg: str) -> None:
    print(f"explorer-sweep: {msg}", flush=True)


def _ps_payload(do_kill: bool) -> str:
    return (_PS_TEMPLATE.replace("__DO_KILL__", "$true" if do_kill else "$false")
                        .replace("__CLSID__", CLSID))


def _enc(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode()


def parse_agent_output(raw: str) -> dict:
    """Pure parser for the JSON `qm guest exec` prints on stdout.

    Never assumes success. Anything that is not unambiguously our own
    marked payload comes back as {"ok": False, "reason": ...} — including
    the qemu-ga exec-status cross-talk case reproduced live (a concurrent
    probe's output surfacing here instead of ours).
    """
    raw = (raw or "").strip()
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        if "guest agent is not running" in raw.lower():
            return {"ok": False, "reason": "no-agent"}
        return {"ok": False, "reason": "bad-json:" + " ".join(raw.split())[:120]}
    if not isinstance(d, dict) or ("exitcode" not in d and "exited" not in d):
        return {"ok": False, "reason": "guest-timeout"}  # qm returned only a pid
    if d.get("exitcode") not in (0, None):
        return {"ok": False, "reason": f"ps-exitcode-{d.get('exitcode')}"}
    data = d.get("out-data") or ""
    idx = data.find(MARKER)
    if idx == -1:
        return {"ok": False, "reason": "no-marker"}
    tail = data[idx + len(MARKER):].splitlines()[0] if data[idx + len(MARKER):] else ""
    try:
        payload = json.loads(tail)
        found = int(payload.get("found", 0))
        killed = int(payload.get("killed", 0))
    except (ValueError, TypeError, IndexError, AttributeError):
        return {"ok": False, "reason": "unparseable-payload"}
    return {"ok": True, "found": found, "killed": killed,
            "errors": list(payload.get("errors") or [])[:5],
            "mode": payload.get("mode")}


def sweep_vm(node_ip: str, vmid: int, do_kill: bool) -> dict:
    """One SSH hop, one qm guest exec. IO only — parsing lives in parse_agent_output."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
           "-o", "BatchMode=yes", f"root@{node_ip}",
           f"qm guest exec {vmid} --timeout {AGENT_TIMEOUT} -- "
           f"powershell -NoProfile -EncodedCommand {_enc(_ps_payload(do_kill))}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=AGENT_TIMEOUT + 30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "reason": f"ssh/{type(e).__name__}"}
    # qm's own JSON envelope is always on stdout — never widen parsing to
    # stderr while it still has a chance of being valid JSON. Only once
    # stdout alone fails to parse do we also look at stderr, purely to catch
    # "guest agent is not running" (which some qm/ssh error paths print
    # there instead of on stdout).
    result = parse_agent_output(r.stdout)
    if not result["ok"] and r.stderr:
        widened = parse_agent_output((r.stdout or "") + " " + r.stderr)
        if widened["reason"] == "no-agent":
            return widened
    return result


def load_candidates(db, nodes_ip: dict) -> list:
    """(nodeId, vmid) for every running, non-maintenance, provisioned VM whose
    node we can resolve. Same exclusions as neuravps-egresscheck.py, for the
    same reasons: a VM mid-reinstall or in maintenance being unreachable is
    expected, not a finding.

    De-duplicated on purpose: neuravps-munones-migracion-y-docs-duplicados.md
    documents `servers` docs that share a proxmoxId (stub + real doc for the
    same VM). Sweeping the same VM twice per run would waste a guest-exec
    round-trip and double count it in the aggregates.
    """
    try:
        from google.cloud.firestore_v1 import FieldFilter
        q = db.collection("servers").where(filter=FieldFilter("status", "==", "running"))
    except ImportError:
        q = db.collection("servers").where("status", "==", "running")
    live_nodes = {snap.id for snap in db.collection("proxmox_nodes").stream()
                 if not (snap.to_dict() or {}).get("decommissioned")}
    out = set()
    for snap in q.select(["proxmoxId", "nodeId", "maintenance",
                          "provisioningStatus", "reinstalling"]).stream():
        d = snap.to_dict() or {}
        nid = d.get("nodeId")
        if nid not in nodes_ip or nid not in live_nodes:
            continue
        if d.get("maintenance") or d.get("reinstalling") is True:
            continue
        prov = d.get("provisioningStatus")
        if prov is not None and prov != "provisioned":
            continue
        try:
            out.add((nid, int(d.get("proxmoxId"))))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def resolve_ip(nodes_ip: dict, nid: str):
    v = nodes_ip.get(nid)
    if isinstance(v, str):
        return v or None
    if isinstance(v, dict):
        return v.get("ip") or None
    return None


def main() -> int:
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run holds the lock — exiting")
        return 0

    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(CREDS))
    db = firestore.client()

    cfg = (db.collection("config").document("explorerSweep").get().to_dict() or None)
    if not cfg or cfg.get("enabled") is not True:
        log("config/explorerSweep missing or enabled!=true — disabled, exiting")
        return 0
    dry = bool(cfg.get("dryRun"))
    max_vms = int(cfg.get("maxVmsPerRun") or DEFAULT_MAX_VMS_PER_RUN)
    workers = int(cfg.get("workers") or WORKERS)

    with open(NODES_FILE) as fh:
        nodes_ip = json.load(fh)

    candidates = load_candidates(db, nodes_ip)
    if not candidates:
        log("no candidates — nothing to do")
        return 0

    # Deterministic-per-day rotation: a persistent cap must not always starve
    # the same tail of the (sorted, therefore stable) candidate list.
    seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    random.Random(seed).shuffle(candidates)

    kill_set = set() if dry else set(candidates[:max_vms])
    cap_applied = (not dry) and len(candidates) > max_vms
    log(f"{len(candidates)} candidatas, workers={workers}, "
        f"maxVmsPerRun={max_vms}{' [DRY-RUN]' if dry else ''}"
        f"{' [CAP: ' + str(max_vms) + '/' + str(len(candidates)) + ']' if cap_applied else ''}")

    def job(item):
        nid, vmid = item
        ip = resolve_ip(nodes_ip, nid)
        if not ip:
            return (nid, vmid, {"ok": False, "reason": "unresolved-node"})
        return (nid, vmid, sweep_vm(ip, vmid, item in kill_set))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(job, candidates))
    duration = time.time() - t0

    measured = [r for r in results if r[2]["ok"]]
    unreachable = [r for r in results if not r[2]["ok"]]
    affected = [r for r in measured if r[2]["found"] > 0]
    total_found = sum(r[2]["found"] for r in measured)
    total_killed = sum(r[2]["killed"] for r in measured)
    reason_counts = Counter(r[2]["reason"] for r in unreachable)

    per_vm = {}
    for nid, vmid, res in measured:
        if res["found"] > 0 or res["errors"]:
            per_vm[str(vmid)] = {"node": nid, "found": res["found"],
                                 "killed": res["killed"], "mode": res.get("mode"),
                                 "errors": res["errors"]}

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    doc = {
        "at": firestore.SERVER_TIMESTAMP,
        "dryRun": dry,
        "candidates": len(candidates),
        "measured": len(measured),
        "unreachable": len(unreachable),
        "unreachableByReason": dict(reason_counts),
        "unreachableSample": [f"vm{vmid}@{nid}:{res['reason']}"
                              for nid, vmid, res in unreachable[:20]],
        "affected": len(affected),
        "totalFound": total_found,
        "totalKilled": total_killed,
        "capApplied": cap_applied,
        "maxVmsPerRun": max_vms,
        "perVm": per_vm,
        "durationS": round(duration, 1),
    }
    db.collection("explorer_sweep_runs").document(run_id).set(doc)

    log(f"fin: {len(candidates)} candidatas, {len(measured)} medidas, "
        f"{len(unreachable)} sin medir ({dict(reason_counts)}), "
        f"{len(affected)} con huerfanos, {total_found} encontrados, "
        f"{total_killed} cerrados, {duration:.0f}s"
        f"{' [DRY-RUN]' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
