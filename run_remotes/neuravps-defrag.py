#!/usr/bin/env python3
"""neuravps-defrag — daily automatic fleet reorganization (operator-approved
2026-07-12: "todo diario 06:30 UTC, directo en real").

Runs ON a BASE (b0) via systemd timer at 06:30 UTC, after the 06:00 salud
sweep has reconciled counters. Two phases, corrections first:

  1. CORRECTION — nodes over their placement limits:
       SQX (AX162): usedRam > 1.5xbase OR floors > 0.6xbase — base RAM =
                    max_base_ram DECLARADO en admin/nodos (2026-07-17), con
                    gbRam synced como fallback para docs antiguos
       MT5 (AX102): usedCores > max_cores (sin fallback fijo: cap ausente =
                    nodo NI over-gate NI destino)
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

RELIEF MODE (--relief, hourly timer): the harm-targeted intra-day pass. The
daily 06:30 run plans from Firestore counters, but balloon-reconciler floors
DRIFT during the day (raises consume budget), so a node can become
floor-starved with a thrashing guest hours before the next daily run (seen
live: node 0000053 vm 808, 2026-07-17 — reconciler logging "NO BUDGET —
placement should relieve this node" with nobody listening). Relief mode:
  * reads /var/run/neuravps-balloon-status.json from every SQX node (written
    by reconciler #NBRVER>=3): LIVE floors_sum + the VMs blocked-thrashing
    >= ~10 consecutive minutes (local automation exhausted, floor < max —
    floor==max cases are in-guest overload i.e. entitlement, never listed).
  * overrides each node's floors with the live value (Firestore is stale
    intra-day) so source detection AND destination margins use truth.
  * for each node with sustained-blocked guests: move the smallest RUNNING
    VM out (a stopped VM frees no live floors) -> the local reconciler gets
    budget and raises the thrasher within ~1 min.
  * blocked guests with NO fitting destination are ESCALATED in the log +
    run doc (capacity problem = operator decision); the node_health daily
    sweep independently warns when a thrasher persists >24h.
  * skips daily phases 1/1.5/2/3 entirely; cap reliefMaxMovesPerRun
    (default 4); journals to defrag_runs ONLY when it found work (hourly
    empty ticks stay out of Firestore).
#NDFVER=8
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

# FLOOR must match the reconciler's FLOOR_BUDGET_PCT (70 since 2026-07-28,
# prov PR#105). It is the ceiling floors ACTUALLY grow to on a node, so it is
# what "does this destination have room" has to be measured against. Leaving it
# at 0.60 while reconcilers filled to 0.70 made every node look over-budget and
# relief escalated "no destination" on a fleet that had plenty — caught live on
# 0000056 minutes after the rollout.
# NOT the same number as auto_provision.SQX_FLOOR_BUDGET_FACTOR (0.60), which
# answers a different question: "should we SELL another VM here". Defrag
# consolidates existing VMs tighter than we sell, on purpose.
COMMIT, FLOOR = 1.5, 0.70
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
    relief = "--relief" in sys.argv[1:]
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
    # Operator 2026-07-13: a failed migration is ESCALATED and excluded from
    # future planning (skip-list), but the run/chain continues with the rest.
    SKIP_FILE = "/var/lib/neuravps-defrag-skip.txt"
    try:
        skip_vmids = {int(x) for x in open(SKIP_FILE).read().split()}
    except Exception:
        skip_vmids = set()
    if skip_vmids:
        log(f"skip-list activa ({len(skip_vmids)} vmids escalados): {sorted(skip_vmids)}")

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
            "g": float(d.get("max_base_ram") or d.get("gbRam") or 0),
            "cap": int(d.get("max_cores") or 0),
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
        if (d.get("status") in ("running", "stopped") and d.get("proxmoxId")
                and int(d.get("proxmoxId")) not in skip_vmids):
            n["vms"].append({"vmid": int(d["proxmoxId"]), "ram": ram,
                             "fl": fl, "cores": cores,
                             "st": d.get("status")})

    def sqx_head(n):
        return COMMIT * n["g"] - n["ram"], FLOOR * n["g"] - n["fl"]

    def placeable(n):
        return not n["frozen"] and not n["status"]

    def pick_dest(model, ram, fl, cores, exclude, tidy=True):
        """tidy=False drops the leftover_clean() packing rule. Relief passes
        False: that rule exists to keep the fleet SELLABLE, and trading
        sellability against a customer who is thrashing is the wrong way round.
        It also fails at its own job here — refusing the move leaves the source
        over-full AND the destination's headroom unused, so nothing is
        preserved (0000214 vm 1868, 2026-07-29: blocked 2h at up to 3023
        faults/s while 0000209 sat on 37.5 GB, rejected only because moving a
        19 GB guest would have left an 18.5 GB gap no plan can fill)."""
        best = None
        for did, dn in nodes.items():
            if dn["model"] != model or did in exclude or not placeable(dn):
                continue
            if model == "SQX":
                if dn["g"] <= 0:
                    continue
                ch, fh = sqx_head(dn)
                # Margins (v6): leave dests room for the BALLOON RECONCILER,
                # which raises floors live in +4G steps. Packing to within
                # 2GB of the floor budget made passes oscillate (2026-07-14:
                # a 25-move pass left 5 nodes over-gate + 14 floor-starved as
                # soon as reconcilers acted). 12GB = 3 reconciler steps.
                if ch - ram < 8 or fh - fl < 12:
                    continue
                if tidy and not leftover_clean(ch - ram):
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

    # ---- relief mode: harm-targeted intra-day pass (see module docstring) ----
    live_blocked = {}   # nid -> [{vmid, rate, floor_mb, ...}] (sustained-blocked)
    if relief:
        import concurrent.futures as cf
        STATUS_MAX_AGE_S = 900

        def read_status(nid):
            ip = nodes[nid]["ip"]
            if not ip:
                return nid, None
            try:
                p = subprocess.run(
                    SSH + [f"root@{ip}",
                           "cat /var/run/neuravps-balloon-status.json 2>/dev/null"],
                    capture_output=True, text=True, timeout=12)
                return nid, json.loads(p.stdout.strip() or "null")
            except Exception:
                return nid, None

        sqx_ids = [nid for nid, n in nodes.items() if n["model"] == "SQX"]
        now_e = time.time()
        fresh = 0
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            for nid, st in ex.map(read_status, sqx_ids):
                if not isinstance(st, dict):
                    continue
                try:
                    if now_e - float(st.get("ts") or 0) > STATUS_MAX_AGE_S:
                        continue
                except (TypeError, ValueError):
                    continue
                fresh += 1
                n = nodes[nid]
                # Live floors (reconciler raises drift intra-day; Firestore is
                # the 06:00 sync). Take the max: live only counts RUNNING VMs,
                # Firestore also reserves stopped ones — both must hold.
                live_fl = float(st.get("floors_sum_mb") or 0) / 1024.0
                if live_fl > n["fl"]:
                    n["fl"] = live_fl
                if st.get("blocked"):
                    live_blocked[nid] = st["blocked"]
        log(f"relief: {fresh}/{len(sqx_ids)} nodes reporting, "
            f"{len(live_blocked)} with sustained-blocked thrashers")
        relief_max = min(int(cfg.get("reliefMaxMovesPerRun") or 4), max_moves)
        for nid in sorted(live_blocked):
            if len(moves) >= relief_max:
                log(f"relief: move cap {relief_max} reached — remaining nodes wait for next tick")
                break
            n = nodes[nid]
            vm_list = ",".join(str(b.get("vmid")) for b in live_blocked[nid][:3])
            worst = max((int(b.get("rate") or 0) for b in live_blocked[nid]), default=0)
            picked = None
            # smallest RUNNING VM out — a stopped VM frees no live floors
            for vm in sorted(n["vms"], key=lambda v: v["ram"]):
                if vm["st"] != "running":
                    continue
                dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid},
                                tidy=False)
                if dst:
                    picked = (vm, dst)
                    break
            if picked:
                book(picked[0], nid, picked[1],
                     f"thrash-relief (vm {vm_list} blocked @{worst}/s, reconciler NO BUDGET)")
            else:
                log(f"ESCALATION {nid}: vm {vm_list} blocked-thrashing @{worst}/s but no "
                    f"destination fits — needs operator (capacity)")

    # ---- phase 1: corrections ----
    for nid in sorted(nodes) if not relief else []:
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
            while n["cap"] > 0 and n["cores"] > n["cap"] and guard < 6:
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
    for nid in sorted(nodes) if not relief else []:
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
    for nid in sorted(nodes) if not relief else []:
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
    for nid in (sorted(nodes, key=lambda k: len(nodes[k]["vms"])) if not relief else []):
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
    if relief and not moves and not live_blocked:
        # hourly tick with nothing to do: syslog only, no Firestore clutter
        log("relief: no sustained-blocked thrashers — nothing to do")
        return 0
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    plan_txt = [f"{m[0]} {m[1].split('-')[0]}->{m[2].split('-')[0]} [{m[3]}]" for m in moves]
    log(f"plan: {len(moves)} move(s)" + (f" (DRY-RUN)" if dry else ""))
    for t in plan_txt:
        log("  " + t)

    doc = {"at": firestore.SERVER_TIMESTAMP, "dryRun": dry, "planned": plan_txt,
           "executed": 0, "ok": 0, "fail": 0, "status": "planned", "relief": relief}
    if relief and live_blocked:
        doc["blockedNodes"] = {
            nid: [f"vm {b.get('vmid')} @{b.get('rate')}/s" for b in bl[:5]]
            for nid, bl in live_blocked.items()}
    db.collection("defrag_runs").document(run_id).set(doc)
    if not moves:
        # relief with blocked thrashers but no fitting destination = capacity
        # problem — the one case that genuinely needs the operator.
        db.collection("defrag_runs").document(run_id).update(
            {"status": "no-dest" if (relief and live_blocked) else "empty"})
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

    failed_vmids = []
    if fail:
        import glob
        el = sorted(glob.glob(f"{LOG_DIR}/errors-*.log"))
        if el:
            failed_vmids = sorted({int(m) for m in re.findall(
                r"FAIL\s+vmid=(\d+)", open(el[-1]).read())})
        with open(SKIP_FILE, "a") as f:
            for v in failed_vmids:
                f.write(f"{v}\n")
        log(f"ESCALACIÓN: {fail} migración(es) FAILED (vmids {failed_vmids}) — "
            f"VMs a salvo en origen (rollback), añadidas a skip-list; la cadena "
            f"CONTINÚA con el resto (operador 2026-07-13)")
    db.collection("defrag_runs").document(run_id).update(
        {"executed": len(verified), "ok": ok, "fail": fail,
         "failedVmids": failed_vmids,
         "status": "done-with-failures" if fail else "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
