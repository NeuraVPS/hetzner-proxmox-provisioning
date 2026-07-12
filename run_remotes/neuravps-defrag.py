#!/usr/bin/env python3
"""neuravps-defrag — daily automatic fleet reorganization (operator-approved
2026-07-12: "todo diario 06:30 UTC, directo en real").

Runs ON a BASE (b0) via systemd timer at 06:30 UTC, after the 06:00 salud
sweep has reconciled counters. Two phases, corrections first:

  1. CORRECTION — nodes over their placement limits:
       SQX (AX162): usedRam > 1.5xgbRam OR floors > 0.6xgbRam
       MT5 (AX102): usedCores > max_cores
     -> move the smallest RUNNING VMs out until back within limits.
  2. DEFRAG (SQX) — nodes where no sellable plan fits but >=5 GB remain
     stranded -> move the smallest RUNNING VM whose departure makes the hole
     plan-sized again (recovers the crumb as sellable capacity).

Safety rails:
  * kill-switch: Firestore config/defrag {enabled, dryRun, maxMovesPerRun}.
    Missing doc => DISABLED (fail-safe).
  * skips the run entirely if any migrate_vm/migrate_vms_batch is already
    running on this base (never fights a manual operation).
  * flock so two runs can never overlap.
  * every candidate re-verified qmpstatus==running on its node right before
    execution (a paused/stopped VM must NEVER take the offline path).
  * destinations: same node model only (SQX->SQX, MT5->MT5), not frozen, no
    status, must stay within THEIR limits after receiving, and (SQX) their
    leftover must stay plan-shaped (clean-fit dp) so we never create crumbs.
  * hard cap maxMovesPerRun (default 8), 1 in-flight per node, 2 total.
  * any migration FAILURE aborts the rest of the run; migrate_vm.sh itself
    rolls the VM back to source (stays running + RDP) on failure.
  * result journaled to Firestore defrag_runs/<YYYYMMDD-HHMM> + syslog.

The beneficios panel computes fullness live from the counters, which this
script re-aggregates for every touched node at the end of the run.
#NDFVER=4
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
import firebase_admin
from firebase_admin import credentials, firestore

COMMIT, FLOOR = 1.5, 0.60
PLAN_SIZES = [19, 23, 31, 35, 60]          # SQX sellable GB sizes (vps-a..e)
PLANS = [("vps-a", 19, 5.7), ("vps-b", 23, 6.9), ("vps-c", 31, 9.3),
         ("vps-d", 35, 10.5), ("vps-e", 60, 18.0)]
MIN_STRANDED = 5.0
BATCH = "/root/migrate_vms_batch.sh"
LOG_DIR = "/var/log/migrate_vm/defrag"
SSH = ["ssh", "-n", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
       "-o", "StrictHostKeyChecking=no"]


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {msg}", flush=True)
    subprocess.run(["logger", "-t", "neuravps-defrag", msg], check=False)


def leftover_clean(gb):
    """GB leftover still plan-shaped (fillable to within 4 GB) or sub-plan tiny."""
    L = int(gb)
    if L < 5:
        return True
    reach = [False] * (L + 1)
    reach[0] = True
    for g in range(1, L + 1):
        for s in PLAN_SIZES:
            if s <= g and reach[g - s]:
                reach[g] = True
                break
    return any(reach[s] for s in range(max(0, L - 4), L + 1))


def fits_any_plan(commit_h, floor_h):
    return any(r <= commit_h and f <= floor_h for _, r, f in PLANS)


def main():
    lock = open("/run/neuravps-defrag.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another defrag run holds the lock — exiting")
        return 0

    # never fight a manual migration on this base. Match only REAL script
    # executions (bash <path>/migrate_vm*.sh ...) — a plain pgrep -f also
    # matches watcher loops whose cmdline merely MENTIONS the script (stale
    # "while pgrep ..." shells false-blocked the very first run, 2026-07-12).
    ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True)
    busy = [ln.split()[0] for ln in ps.stdout.splitlines()
            if re.search(r"^\s*\d+\s+(\S*/)?bash\s+(\S*/)?migrate_vms?(_batch)?\.sh(\s|$)", ln)]
    if busy:
        log(f"manual migration in progress (pids {busy}) — skipping run")
        return 0

    firebase_admin.initialize_app(
        credentials.Certificate(os.environ["FIREBASE_CREDENTIALS_FILE"]))
    db = firestore.client()

    cfg = (db.collection("config").document("defrag").get().to_dict() or None)
    if not cfg or cfg.get("enabled") is not True:
        log("config/defrag missing or enabled!=true — disabled, exiting")
        return 0
    dry = bool(cfg.get("dryRun"))
    max_moves = int(cfg.get("maxMovesPerRun") or 8)

    # ---- fleet state ----
    nodes = {}
    for nd in db.collection("proxmox_nodes").stream():
        d = nd.to_dict() or {}
        model = "SQX" if "-AX162" in nd.id else ("MT5" if "-AX102" in nd.id else None)
        if not model:
            continue
        nodes[nd.id] = {
            "model": model, "ip": d.get("ip") or "", "frozen": bool(d.get("frozen")),
            "status": str(d.get("status") or "").strip(),
            "g": float(d.get("gbRam") or 0), "cap": int(d.get("max_cores") or 96),
            "ram": 0.0, "fl": 0.0, "cores": 0, "vms": [],
        }
    for s in db.collection("servers").stream():
        d = s.to_dict() or {}
        nid = d.get("nodeId")
        if nid not in nodes:
            continue
        n = nodes[nid]
        ram, fl = float(d.get("memoryGb") or 0), float(d.get("ramFloorGb") or 0)
        cores = int(d.get("cores") or 0)
        n["ram"] += ram
        n["fl"] += fl
        n["cores"] += cores
        # 2026-07-13: stopped VMs are migratable too (offline path now wraps
        # the temporary power-on with the vm_no_internet firewall group), so
        # nodes holding stopped trading VMs CAN reach 0%. Paused VMs are the
        # only exclusion (precheck rejects them; 1884 lesson).
        if d.get("status") in ("running", "stopped") and d.get("proxmoxId"):
            n["vms"].append({"vmid": int(d["proxmoxId"]), "ram": ram,
                             "fl": fl, "cores": cores,
                             "st": d.get("status")})

    def sqx_head(n):
        return COMMIT * n["g"] - n["ram"], FLOOR * n["g"] - n["fl"]

    def placeable(n):
        return not n["frozen"] and not n["status"]

    def pick_dest(model, ram, fl, cores, exclude):
        best = None
        for did, dn in nodes.items():
            if dn["model"] != model or did in exclude or not placeable(dn):
                continue
            if model == "SQX":
                if dn["g"] <= 0:
                    continue
                ch, fh = sqx_head(dn)
                if ch - ram < 5 or fh - fl < 2:
                    continue
                if not leftover_clean(ch - ram):
                    continue
                key = ch
            else:
                free = dn["cap"] - dn["cores"]
                if free - cores < 0:
                    continue
                key = free
            # BEST-FIT: fullest fitting node (min headroom), and NEVER an
            # empty node — empties are the operator's drain/maintenance
            # reserve (worst-fit here is what consumed them on 07-12).
            if len(dn["vms"]) == 0 and dn["ram"] <= 0 and dn["cores"] <= 0:
                continue
            if best is None or key < best[1]:
                best = (did, key)
        return best[0] if best else None

    moves = []   # (vmid, src_id, dst_id, reason)

    def book(vm, src, dst, reason):
        sn, dn = nodes[src], nodes[dst]
        sn["ram"] -= vm["ram"]
        sn["fl"] -= vm["fl"]
        sn["cores"] -= vm["cores"]
        dn["ram"] += vm["ram"]
        dn["fl"] += vm["fl"]
        dn["cores"] += vm["cores"]
        sn["vms"] = [v for v in sn["vms"] if v["vmid"] != vm["vmid"]]
        moves.append((vm["vmid"], src, dst, reason))

    # ---- phase 1: corrections ----
    for nid in sorted(nodes):
        n = nodes[nid]
        if n["model"] == "SQX" and n["g"] > 0:
            guard = 0
            while guard < 6:
                ch, fh = sqx_head(n)
                if ch >= 0 and fh >= 0:
                    break
                cand = sorted(n["vms"], key=lambda v: v["ram"])
                moved = False
                for vm in cand:
                    dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid})
                    if dst:
                        book(vm, nid, dst, f"over-gate ({ch:.0f}/{fh:.0f} headroom)")
                        moved = True
                        break
                if not moved:
                    log(f"WARN {nid} over-gate but no destination fits — leaving")
                    break
                guard += 1
        elif n["model"] == "MT5":
            guard = 0
            while n["cores"] > n["cap"] and guard < 6:
                cand = sorted(n["vms"], key=lambda v: v["cores"])
                moved = False
                for vm in cand:
                    if vm["cores"] <= 0:
                        continue
                    dst = pick_dest("MT5", vm["ram"], vm["fl"], vm["cores"], {nid})
                    if dst:
                        book(vm, nid, dst, f"over-cap ({n['cores']}/{n['cap']}c)")
                        moved = True
                        break
                if not moved:
                    log(f"WARN {nid} over-cap but no destination fits — leaving")
                    break
                guard += 1

    # ---- phase 1.5: relieve floor-starved SQX nodes ----
    # A node whose floors sit within one reconciler STEP (4 GB) of the 60%
    # budget BLOCKS the balloon reconciler ("NO BUDGET" — it cannot raise a
    # thrashing guest's floor). Moving one small VM out restores burst
    # headroom. Seen live on 0000192 (2026-07-12: vms 795/865/812 thrashing,
    # reconciler logging NO BUDGET for hours).
    for nid in sorted(nodes):
        if len(moves) >= max_moves:
            break
        n = nodes[nid]
        if n["model"] != "SQX" or n["g"] <= 0:
            continue
        ch, fh = sqx_head(n)
        if fh < 0 or fh >= 6.0:
            continue
        for vm in sorted(n["vms"], key=lambda v: v["ram"]):
            dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid})
            if dst:
                book(vm, nid, dst, f"floor-starved (headroom {fh:.1f}GB blocks reconciler)")
                break

    # ---- phase 2: SQX crumb defrag ----
    for nid in sorted(nodes):
        if len(moves) >= max_moves:
            break
        n = nodes[nid]
        if n["model"] != "SQX" or n["g"] <= 0:
            continue
        ch, fh = sqx_head(n)
        if ch < 0 or fh < 0 or fits_any_plan(ch, fh) or min(ch, fh) < MIN_STRANDED:
            continue
        # smallest VM whose departure makes some plan fit again
        for vm in sorted(n["vms"], key=lambda v: v["ram"]):
            if fits_any_plan(ch + vm["ram"], fh + vm["fl"]):
                dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid})
                if dst:
                    book(vm, nid, dst, f"defrag ({min(ch, fh):.0f}GB stranded)")
                break

    # ---- phase 3: consolidation (restore the empty-node reserve) ----
    CONSOL_MAX_VMS = int(cfg.get("consolidateMaxVms") or 6)
    for nid in sorted(nodes, key=lambda k: len(nodes[k]["vms"])):
        if len(moves) >= max_moves:
            break
        n = nodes[nid]
        total_vms = n["n_docs"] if "n_docs" in n else None
        # movable = running VMs we know; a node also holding STOPPED vms can
        # never fully empty (we never power-cycle stopped trading VMs) -> skip.
        if not (0 < len(n["vms"]) <= CONSOL_MAX_VMS):
            continue
        planned = []
        snapshot = {k: (v["ram"], v["fl"], v["cores"], len(v["vms"])) for k, v in nodes.items()}
        ok = True
        for vm in sorted(n["vms"], key=lambda v: -v["ram"]):
            dst = pick_dest(n["model"], vm["ram"], vm["fl"], vm["cores"],
                            {nid})
            if not dst:
                ok = False
                break
            book(vm, nid, dst, f"consolidación (vaciar {nid.split('-')[0]})")
            planned.append(vm)
        if not ok or len(moves) > max_moves:
            # rollback this node's bookings (couldn't fully empty)
            for k, (r, f, c, _) in snapshot.items():
                nodes[k]["ram"], nodes[k]["fl"], nodes[k]["cores"] = r, f, c
            moves[:] = [m for m in moves if m[1] != nid or "consolid" not in m[3]]
            continue

    moves[:] = moves[:max_moves]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    plan_txt = [f"{m[0]} {m[1].split('-')[0]}->{m[2].split('-')[0]} [{m[3]}]" for m in moves]
    log(f"plan: {len(moves)} move(s)" + (f" (DRY-RUN)" if dry else ""))
    for t in plan_txt:
        log("  " + t)

    doc = {"at": firestore.SERVER_TIMESTAMP, "dryRun": dry, "planned": plan_txt,
           "executed": 0, "ok": 0, "fail": 0, "status": "planned"}
    db.collection("defrag_runs").document(run_id).set(doc)
    if not moves:
        db.collection("defrag_runs").document(run_id).update({"status": "empty"})
        return 0
    if dry:
        db.collection("defrag_runs").document(run_id).update({"status": "dry-run"})
        return 0

    # ---- live qmpstatus precheck (never let a paused/stopped VM in) ----
    verified = []
    for vmid, src, dst, reason in moves:
        ip = nodes[src]["ip"]
        out = subprocess.run(
            SSH + [f"root@{ip}",
                   f"qm status {vmid} --verbose 2>/dev/null | awk -F': ' "
                   f"'/^status:/{{s=$2}} /^qmpstatus:/{{q=$2}} END{{print s, q}}'"],
            capture_output=True, text=True, timeout=25).stdout.split()
        st = out[0] if out else ""
        qmp = out[1] if len(out) > 1 else ""
        # stopped -> offline path (firewall-guarded power-on); running needs
        # qmpstatus==running (a PAUSED vm reports status running — never move).
        if st == "stopped" or (st == "running" and qmp == "running"):
            verified.append((vmid, src, dst, reason))
        else:
            log(f"SKIP vm {vmid}: status={st or '?'} qmp={qmp or '?'} (paused/raro)")
    if not verified:
        db.collection("defrag_runs").document(run_id).update({"status": "nothing-verified"})
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    pairs = "/root/defrag_pairs.txt"
    with open(pairs, "w") as f:
        for vmid, _s, dst, _r in verified:
            f.write(f"{vmid} {int(dst.split('-')[0])}\n")
    log(f"executing {len(verified)} migration(s) via {BATCH}")
    p = subprocess.run(["bash", BATCH, "-f", pairs, "-c", "1", "-m", "2",
                        "-l", LOG_DIR], capture_output=True, text=True)
    ok = fail = 0
    for ln in p.stdout.splitlines():
        if "SUMMARY" in ln:
            for tok in ln.split():
                if tok.startswith("ok="):
                    ok = int(tok[3:])
                if tok.startswith("fail="):
                    fail = int(tok[5:])
    log(f"batch done rc={p.returncode} ok={ok} fail={fail}")

    # ---- refresh counters for touched nodes (panel reads these live) ----
    touched = sorted({m[1] for m in verified} | {m[2] for m in verified})
    agg = {t: {"usedCores": 0, "usedVms": 0, "usedDiskGb": 0,
               "usedRamGb": 0.0, "usedRamFloorGb": 0.0} for t in touched}
    for s in db.collection("servers").stream():
        d = s.to_dict() or {}
        nid = d.get("nodeId")
        if nid in agg:
            a = agg[nid]
            a["usedCores"] += int(d.get("cores") or 0)
            a["usedVms"] += 1
            a["usedDiskGb"] += int(d.get("diskGb") or 0)
            a["usedRamGb"] += float(d.get("memoryGb") or 0)
            a["usedRamFloorGb"] += float(d.get("ramFloorGb") or 0)
    for nid, a in agg.items():
        a["usedRamGb"] = round(a["usedRamGb"], 2)
        a["usedRamFloorGb"] = round(a["usedRamFloorGb"], 2)
        db.collection("proxmox_nodes").document(nid).update(a)

    db.collection("defrag_runs").document(run_id).update(
        {"executed": len(verified), "ok": ok, "fail": fail,
         "status": "failed" if fail else "done"})
    if fail:
        log(f"!!! {fail} migration(s) FAILED — check {LOG_DIR}; VMs rolled back to source")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
