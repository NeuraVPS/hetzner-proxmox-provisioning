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

VICTIM CHOICE (v9): every migration freezes the moved guest at cutover
(measured 2026-07-31: 12-90 s, median ~38 s over the last 25 moves), so WHO
gets moved is customer-facing, not an implementation detail. Two rules on
top of the old smallest-first:
  * among the 3 smallest eligible VMs, prefer the CALMEST (lowest sustained
    fault rate from the node's live rates.tsv): a calm guest has few dirty
    pages -> converges in one pass -> shortest freeze, and its owner is the
    least likely to be at the keyboard. Rates unreadable -> size order.
  * a VM moved within the last moveCooldownH hours (default 72) goes LAST,
    never first — vm 518 was moved 7 times in 14 days by always being the
    smallest on whatever node ran hot. Cooldown DEPRIORITIZES, it never
    excludes: relief must always be able to act on a thrashing node.
State in /var/lib/neuravps-defrag-recent.json ({vmid: last-move epoch}).

--dry-run flag: plan + journal (as dryRun) but never execute, without
touching the config/defrag kill-switch the hourly timers read.
#NDFVER=10
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

os.environ.setdefault("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
import firebase_admin
from firebase_admin import credentials, firestore

# FLOOR must match the reconciler's FLOOR_BUDGET_PCT (90 since 2026-08-16 —
# justified by the live sweep: 40% of physical RAM idle, KSM ~73 GB/node,
# PSI 0.00 fleet-wide; 80 from 2026-07-29, 70 for one day, 60 before that).
# It is the ceiling floors ACTUALLY grow to on a node, so it is
# what "does this destination have room" has to be measured against. Leaving it
# at 0.60 while reconcilers filled to 0.70 made every node look over-budget and
# relief escalated "no destination" on a fleet that had plenty — caught live on
# 0000056 minutes after the rollout.
# FLOOR_SALES mirrors auto_provision.SQX_FLOOR_BUDGET_FACTOR
# (config/capacityGates.json, 0.80 since 2026-08-16; 0.70 before), which answers a
# DIFFERENT question: "could a sale still land here". Phase 2 (crumb
# recovery) exists to recover SELLABLE capacity, so its arithmetic has to use
# the sales budget: measured against FLOOR (0.80) it would call the ~30 nodes
# sitting at 95-100 % of the operational ceiling "recoverable crumbs" and
# freeze a customer per node per day chasing holes no sale can ever fill
# (audit 2026-07-31). Destination margins and over-gate corrections keep
# using FLOOR: existing VMs are deliberately packed tighter than we sell.
COMMIT, FLOOR, FLOOR_SALES = 1.5, 0.90, 0.80
# Ocupacion REAL del pool por encima de la cual un nodo deja de valer como
# DESTINO. Es el mismo numero y la misma fuente que usa la venta
# (auto_provision._DISK_REAL_MAX_PCT, alimentado por proxmox_nodes.zpoolCapPct),
# y eso es deliberado: no tiene sentido que la venta se niegue a poner una VM
# en un nodo y el rebalanceador se la mande igual media hora despues.
#
# ⚠️ ESTO NO REDUCE CUANTAS VMs CABEN EN UN NODO. No es un gate de venta y no
# toca capacityGates: solo decide donde ATERRIZA una VM que ya se iba a mover
# de todas formas. El sobrecompromiso de la flota (112% de media, permitido
# hasta 180% por sqxDiskThinFactor) se queda exactamente donde esta.
#
# Por que hace falta: pick_dest filtraba por RAM y cores y no miraba disco en
# absoluto, asi que podia amontonar VMs en un nodo que se esta llenando. Medido
# el 2026-08-21 en 0000199-AX102-1, el mas lleno de la flota: 82% de pool, 71%
# de fragmentacion (media de la flota 30%), presion de E/S 3,11 frente a
# 0,10-0,25 de sus iguales y latencia de lectura de 313 us frente a 104-209.
# Degradacion medible, no teorica.
#
# FAIL-OPEN a proposito: un nodo cuya ocupacion aun no ha sincronizado NO puede
# volverse incolocable. Misma regla que usa la base de RAM.
DISK_REAL_MAX_PCT = 80.0


def disk_ok(n):
    """False si el pool del destino esta realmente cerca de lleno.

    Desconocido -> True: ver la nota de DISK_REAL_MAX_PCT sobre el fail-open.
    Un nodo cuya ocupacion no ha sincronizado no debe quedarse sin destinos.

    A nivel de modulo y no anidada en main() para que se pueda probar: no
    necesita nada del contexto, solo el dict del nodo.
    """
    pct = n.get("zpool_pct")
    return pct is None or pct < DISK_REAL_MAX_PCT
# Sellable SQX catalog — mirror of pricingPlans.json (ram, expected floor =
# max(ram_min, ram_min_observed), the same number auto_provision reserves).
# vps-e is 48 GB since 2026-07-25; the old table still carried 60, and
# nominal floors (5.7-18.0) that understate the measured settle points 1.66x,
# so fits_any_plan() under-estimated what a destination must reserve.
# RE-MEASURE together with pricingPlans.json ram_min_observed.
PLAN_SIZES = [16, 19, 31, 33, 45]          # SQX sellable GB sizes (vps-a..e;
                                           # catalogue-wide respec 2026-08-16)
PLANS = [("vps-a", 16, 8.2), ("vps-b", 19, 9.7), ("vps-c", 31, 15.5),
         ("vps-d", 33, 23.8), ("vps-e", 45, 43.1)]

# Floor a guest of each plan ACTUALLY settles at — pricingPlans.json
# max(ram_min, ram_min_observed), measured 2026-07-29 over 657 live guests.
# Used ONLY to charge a server doc that has no ramFloorGb yet.
#
# `float(d.get("ramFloorGb") or 0)` used to charge such a doc ZERO, so a VM
# with no floor made its node look emptier than empty and the daily pass
# (which plans from Firestore, unlike the hourly relief pass that reads the
# reconciler's LIVE floors_sum_mb) would happily pick it as a destination.
# Real case 2026-07-30: four vps-e created outside auto_provision had no
# ramFloorGb and hid 180.8 GB of floor on 0000185-AX162-2-LTD.
# vps-d stays 25.2 here (not the 23.8 of the new 33 GB generation): this
# table charges docs with NO ramFloorGb, and a floor-less D is more likely
# a legacy 35/45 GB box — charge the conservative generation.
EXPECTED_FLOOR = {"mt": 2.0, "mt-plus": 4.0, "vps-a": 9.7, "vps-b": 11.9,
                  "vps-c": 15.5, "vps-d": 25.2, "vps-e": 52.2}
WORST_FLOOR = max(EXPECTED_FLOOR.values())
MIN_STRANDED = 5.0
# How often the run doc gets a liveness beat while the batch runs. Small
# enough that "the service died" is visible in minutes, large enough that a
# 9 h batch costs ~110 Firestore writes and not 10.000.
_HEARTBEAT_S = 300

BATCH = "/root/migrate_vms_batch.sh"
LOG_DIR = "/var/log/migrate_vm/defrag"
RECENT_FILE = "/var/lib/neuravps-defrag-recent.json"
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


def load_recent_moves(cooldown_h):
    """vmids moved within the last cooldown_h hours ({} state file -> none)."""
    try:
        data = json.load(open(RECENT_FILE))
    except Exception:
        return set()
    cutoff = time.time() - cooldown_h * 3600
    try:
        return {int(v) for v, ts in data.items() if float(ts) >= cutoff}
    except (TypeError, ValueError):
        return set()


def record_recent_moves(vmids):
    """Merge just-moved vmids into the state file; prune entries >30 days."""
    try:
        data = json.load(open(RECENT_FILE))
    except Exception:
        data = {}
    now_e = time.time()
    for v in vmids:
        data[str(v)] = now_e
    data = {v: ts for v, ts in data.items()
            if isinstance(ts, (int, float)) and now_e - ts < 30 * 86400}
    try:
        with open(RECENT_FILE, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        log(f"WARN could not persist recent-moves state: {exc}")


def main():
    relief = "--relief" in sys.argv[1:]
    force_dry = "--dry-run" in sys.argv[1:]
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
    dry = bool(cfg.get("dryRun")) or force_dry
    max_moves = int(cfg.get("maxMovesPerRun") or 8)
    cooldown_h = float(cfg.get("moveCooldownH") or 72)
    recent_vmids = load_recent_moves(cooldown_h)
    if recent_vmids:
        log(f"cooldown {cooldown_h:.0f}h: {len(recent_vmids)} VM(s) moved "
            f"recently go LAST in victim choice: {sorted(recent_vmids)}")
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
            # Ocupacion real del pool, la escribe node_health en el doc del nodo.
            # None = todavia no sincronizada, y entonces no bloquea (fail-open).
            "zpool_pct": (float(d["zpoolCapPct"])
                          if d.get("zpoolCapPct") is not None else None),
            "ram": 0.0, "fl": 0.0, "cores": 0, "vms": [], "n_all": 0,
        }
    for s in db.collection("servers").stream():
        d = s.to_dict() or {}
        nid = d.get("nodeId")
        if nid not in nodes:
            continue
        n = nodes[nid]
        ram = float(d.get("memoryGb") or 0)
        # A missing floor is charged its PLAN's real floor (or the largest
        # plan's when the plan is unknown too) — never 0, which would make the
        # node look emptier than it is and invite more VMs onto it.
        fl = d.get("ramFloorGb")
        fl = (float(fl) if fl is not None
              else EXPECTED_FLOOR.get(d.get("serverType"), WORST_FLOOR))
        cores = int(d.get("cores") or 0)
        n["ram"] += ram
        n["fl"] += fl
        n["cores"] += cores
        n["n_all"] += 1
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
            if not disk_ok(dn):
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

    _rates_cache = {}

    def node_rates(nid):
        """vmid -> sustained faults/s from the node's live rates.tsv ({} on
        any error — victim choice then falls back to pure size order). One
        SSH per node per run, and only for nodes we are about to move a VM
        off, so the daily planner stays Firestore-only in the common case."""
        if nid in _rates_cache:
            return _rates_cache[nid]
        rates = {}
        ip = nodes[nid]["ip"]
        if ip:
            try:
                p = subprocess.run(
                    SSH + [f"root@{ip}",
                           "cat /var/run/neuravps-balloon-rates.tsv 2>/dev/null"],
                    capture_output=True, text=True, timeout=12)
                for ln in p.stdout.splitlines():
                    cols = ln.split("\t")
                    if len(cols) >= 2:
                        rates[int(cols[0])] = float(cols[1])
            except Exception:
                rates = {}
        _rates_cache[nid] = rates
        return rates

    def agent_alive(nid, vmid):
        """A RUNNING victim with a dead guest agent must not be picked: the
        post-move in-guest IPv6 reconfig cannot run and the customer lands
        UNREACHABLE while the VM runs fine (vms 854/1023, 2026-08-01 — 2 of
        23 in an otherwise clean batch). One quick ping; migrate_vm.sh's own
        pre-check gives a flaky agent a longer second chance. Fail-OPEN on
        ssh/node trouble: an unreachable node is not the victim's fault."""
        # exit-code 255 es ambiguo (ssh roto O qm fallando) — marcador explícito:
        # sin línea RC= -> el ssh no llegó -> fail-open; RC=0 -> vivo; resto -> muerto.
        try:
            r = subprocess.run(
                ["ssh", "-n", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 f"root@{nodes[nid]['ip']}",
                 f"timeout 12 qm agent {vmid} ping >/dev/null 2>&1; echo RC=$?"],
                capture_output=True, text=True, timeout=25)
        except Exception:  # noqa: BLE001
            return True
        out = r.stdout or ""
        if "RC=" not in out:
            return True
        return "RC=0" in out

    def order_victims(vms, nid, sizekey=lambda v: v["ram"]):
        """Candidates in move-preference order (v10): not-recently-moved
        first; among the 3 smallest, calmest first (fewest dirty pages ->
        shortest cutover freeze, least-present owner); then smallest-first.
        Cooldown deprioritizes, never excludes — see module docstring.
        v10: lazy GENERATOR that additionally skips RUNNING candidates whose
        guest agent doesn't answer (one ping per candidate actually
        considered — callers stop at the first bookable one). Stopped VMs
        pass ungated: the offline path boots them fresh, agent included."""
        by_size = sorted(vms, key=lambda v: (sizekey(v), v["vmid"]))
        head, tail = by_size[:3], by_size[3:]
        if len(head) > 1:
            rates = node_rates(nid)
            head.sort(key=lambda v: (rates.get(v["vmid"], 0.0),
                                     sizekey(v), v["vmid"]))
        ordered = head + tail
        for v in ([v for v in ordered if v["vmid"] not in recent_vmids]
                  + [v for v in ordered if v["vmid"] in recent_vmids]):
            # Never hand the same VM to the batch twice. book() rebinds
            # sn["vms"] to a fresh list, but every pass materializes its
            # candidates BEFORE iterating, so a booked VM survives inside the
            # local list and can be booked again for a second destination.
            # Run 20260820-0630 did exactly that: vm 1269 was planned
            # 0000237->0000046 AND 0000237->0000223. The first move won, the
            # second could only fail ("not found on source") and paged the
            # operator a 🔴 FALLIDA for a fleet that was working correctly.
            # Worse than the noise: the planner had already counted 1269's RAM
            # as relief for 0000237 twice, so it believed it freed double.
            if v["vmid"] in booked:
                continue
            if v.get("st") == "running" and not agent_alive(nid, v["vmid"]):
                log(f"victim-skip vm {v['vmid']} ({nid}): guest agent no "
                    f"contesta — el reconfig IPv6 fallaría; probando la siguiente")
                continue
            yield v

    moves = []   # (vmid, src_id, dst_id, reason)
    booked = set()   # vmids already planned this run — order_victims skips them

    def book(vm, src, dst, reason):
        sn, dn = nodes[src], nodes[dst]
        sn["ram"] -= vm["ram"]
        sn["fl"] -= vm["fl"]
        sn["cores"] -= vm["cores"]
        dn["ram"] += vm["ram"]
        dn["fl"] += vm["fl"]
        dn["cores"] += vm["cores"]
        sn["vms"] = [v for v in sn["vms"] if v["vmid"] != vm["vmid"]]
        booked.add(vm["vmid"])
        moves.append((vm["vmid"], src, dst, reason))

    # ---- relief mode: harm-targeted intra-day pass (see module docstring) ----
    live_blocked = {}   # nid -> [{vmid, rate, floor_mb, ...}] (sustained-blocked)
    live_io = {}        # nid -> (pswpin_ps, psi_io_pct) — real-I/O evidence
    gate_skipped = {}   # nid -> blocked list, suppressed by the zswap gate
    dest_failed = set()  # nids whose victim search genuinely found NO dest
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
                    # Real-I/O evidence, when the node's reconciler exports it.
                    # Absent (older reconciler) -> stays None -> we FAIL OPEN
                    # and behave exactly as before.
                    live_io[nid] = (st.get("pswpin_ps"), st.get("psi_io_pct"))
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

            # `worst` comes from major_page_faults, which scores a zswap hit
            # like a disk read. With zswap on fleet-wide most of those faults
            # are compressed RAM, so a high rate does NOT mean the guest is
            # stalling — and this branch migrates ANOTHER customer's VM live.
            # Real case 2026-07-29 (run 20260729-1421): vm1710 read 419/s and
            # booked a move of vm778, while its node did 0 pages/s from disk.
            #
            # Ships in LOG-ONLY mode: it reports what it would have suppressed
            # and changes nothing, so the decision can be made on a few days of
            # real counts. Flip reliefRequireDiskIo to enforce. Missing signals
            # (older reconciler) always count as real — fail open, never
            # suppress on absence of data.
            psw, psi = live_io.get(nid, (None, None))
            min_psw = int(cfg.get("reliefMinPswpinPs") or 50)
            min_psi = float(cfg.get("reliefMinPsiIoPct") or 1.0)
            if psw is None or psi is None:
                real_io = True
            else:
                try:
                    real_io = int(psw) >= min_psw or float(psi) >= min_psi
                except (TypeError, ValueError):
                    real_io = True
            if not real_io:
                if cfg.get("reliefRequireDiskIo"):
                    log(f"relief: SKIP {nid} — vm {vm_list} @{worst}/s but node shows "
                        f"no real disk pressure (pswpin={psw}/s, psi_io={psi}%); "
                        f"faults are zswap hits, not I/O")
                    # A gate-skip is NOT a capacity problem: pick_dest was never
                    # even consulted. Recording these under blockedNodes made
                    # the run classify as "no-dest" and paged the operator with
                    # a phantom "no room in the whole fleet" (run 20260801-1121
                    # while six empty LTDs sat placeable). Track separately.
                    gate_skipped[nid] = live_blocked[nid]
                    continue
                log(f"relief: WOULD-SKIP {nid} — vm {vm_list} @{worst}/s with "
                    f"pswpin={psw}/s psi_io={psi}% (below {min_psw}/s and "
                    f"{min_psi}%); proceeding because reliefRequireDiskIo is off")

            picked = None
            # RUNNING VMs only — a stopped VM frees no live floors. The
            # thrasher itself sorts last naturally: its fault rate is the
            # highest on the node (and moving a hot guest = longest freeze).
            for vm in order_victims([v for v in n["vms"]
                                     if v["st"] == "running"], nid):
                dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid},
                                tidy=False)
                if dst:
                    picked = (vm, dst)
                    break
            if picked:
                book(picked[0], nid, picked[1],
                     f"thrash-relief (vm {vm_list} blocked @{worst}/s, reconciler NO BUDGET)")
            else:
                dest_failed.add(nid)
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
                cand = order_victims(n["vms"], nid)
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
                cand = order_victims(n["vms"], nid,
                                     sizekey=lambda v: v["cores"])
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
        for vm in order_victims(n["vms"], nid):
            dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid})
            if dst:
                book(vm, nid, dst, f"floor-starved (headroom {fh:.1f}GB blocks reconciler)")
                break

    # ---- phase 2: SQX crumb defrag (SALES basis — see FLOOR_SALES) ----
    # A crumb is capacity a SALE could use but no plan fits: measured against
    # the 0.70 sales budget, not the 0.80 operational ceiling. A node whose
    # floors already exceed the sales budget has nothing recoverable — it is
    # simply full by sales standards, and "recovering" its holes would freeze
    # a customer for capacity auto_provision can never sell.
    for nid in sorted(nodes) if not relief else []:
        if len(moves) >= max_moves:
            break
        n = nodes[nid]
        if n["model"] != "SQX" or n["g"] <= 0:
            continue
        ch, fh = sqx_head(n)
        fh_s = FLOOR_SALES * n["g"] - n["fl"]
        if ch < 0 or fh < 0:
            continue          # over the op ceiling: phase 1 territory
        if min(ch, fh_s) < MIN_STRANDED or fits_any_plan(ch, fh_s):
            continue
        # smallest VM whose departure makes some plan fit again
        for vm in order_victims(n["vms"], nid):
            if fits_any_plan(ch + vm["ram"], fh_s + vm["fl"]):
                dst = pick_dest("SQX", vm["ram"], vm["fl"], vm["cores"], {nid})
                if dst:
                    book(vm, nid, dst, f"defrag ({min(ch, fh_s):.0f}GB stranded)")
                break

    # ---- phase 3: consolidation (restore the empty-node reserve) ----
    CONSOL_MAX_VMS = int(cfg.get("consolidateMaxVms") or 6)
    for nid in (sorted(nodes, key=lambda k: len(nodes[k]["vms"])) if not relief else []):
        if len(moves) >= max_moves:
            break
        n = nodes[nid]
        if not (0 < len(n["vms"]) <= CONSOL_MAX_VMS):
            continue
        # Consolidation only pays off when EVERY server doc on the node is
        # movable. n_all also counts what n["vms"] excludes (paused,
        # skip-listed, proxmoxId-less docs): moving the movable ones off such
        # a node costs the migrations and the node STILL can't empty.
        if len(n["vms"]) != n["n_all"]:
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
        real_blocked = {nid: bl for nid, bl in live_blocked.items()
                        if nid not in gate_skipped}
        if real_blocked:
            doc["blockedNodes"] = {
                nid: [f"vm {b.get('vmid')} @{b.get('rate')}/s" for b in bl[:5]]
                for nid, bl in real_blocked.items()}
        if gate_skipped:
            doc["zswapSkipped"] = {
                nid: [f"vm {b.get('vmid')} @{b.get('rate')}/s" for b in bl[:5]]
                for nid, bl in gate_skipped.items()}
    db.collection("defrag_runs").document(run_id).set(doc)
    if not moves:
        # "no-dest" (the one case that genuinely pages the operator: capacity)
        # requires a victim search to have actually FAILED — not a zswap-gate
        # skip, which by definition needed no move at all.
        if relief and dest_failed:
            status = "no-dest"
        elif relief and gate_skipped:
            status = "zswap-quiet"
        else:
            status = "empty"
        db.collection("defrag_runs").document(run_id).update({"status": status})
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
    # Second line of defence behind the `booked` guard in order_victims: a
    # duplicate vmid reaching the batch costs a guaranteed FAIL and a false
    # 🔴 alert, so it is worth asserting at the boundary too.
    seen_vmids = set()
    deduped = []
    for mv in verified:
        if mv[0] in seen_vmids:
            log(f"plan-dedup: vm {mv[0]} ya estaba planificado — descartando "
                f"el segundo destino ({mv[2]})")
            continue
        seen_vmids.add(mv[0])
        deduped.append(mv)
    verified = deduped
    with open(pairs, "w") as f:
        for vmid, _s, dst, _r in verified:
            f.write(f"{vmid} {int(dst.split('-')[0])}\n")
    log(f"executing {len(verified)} migration(s) via {BATCH}")
    run_ref = db.collection("defrag_runs").document(run_id)

    # STREAM the batch; do not block on it. A full day is ~9 h of wall clock —
    # a VPS E drags ~300 GB of disk over a 1 Gbit link, so ~48 min of storage
    # copy before the 60 GiB of RAM state even starts (measured on vm1910,
    # 2026-08-20). With the old capture_output=True the run doc sat at
    # status="planned"/executed=0 for that entire ride, so the stall check —
    # which reads exactly those fields — paged ATASCADA on every long-but-
    # healthy batch. 20260820-0630 did it with 27 OK and 2 still in flight.
    #
    # Two DIFFERENT timestamps, because there are two different failures and
    # one cannot stand in for the other:
    #   heartbeatAt    — the defrag process is alive (ticked on a timer)
    #   lastProgressAt — a migration actually finished (ticked on OK/FAIL)
    # A dead service freezes both. A hung migrate_vm.sh child freezes only
    # lastProgressAt while the parent keeps breathing, and that is the case
    # the old single-timestamp check could never name.
    proc = subprocess.Popen(
        ["bash", BATCH, "-f", pairs, "-c", "1", "-m", "2", "-l", LOG_DIR],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    stop_beat = threading.Event()

    def _beat() -> None:
        while not stop_beat.wait(_HEARTBEAT_S):
            try:
                run_ref.update({"heartbeatAt": firestore.SERVER_TIMESTAMP})
            except Exception:  # pylint: disable=broad-except
                pass          # a lost beat is not worth killing the batch over

    run_ref.update({"status": "running",
                    "heartbeatAt": firestore.SERVER_TIMESTAMP,
                    "lastProgressAt": firestore.SERVER_TIMESTAMP})
    beat = threading.Thread(target=_beat, daemon=True)
    beat.start()

    out_lines = []
    live_ok = live_fail = 0
    try:
        for ln in proc.stdout:                      # type: ignore[union-attr]
            ln = ln.rstrip("\n")
            out_lines.append(ln)
            tok = ln.split()
            # batch log() format: "<ts> OK   vmid=..." / "<ts> FAIL vmid=..."
            if len(tok) > 1 and tok[1] in ("OK", "FAIL") and "vmid=" in ln:
                if tok[1] == "OK":
                    live_ok += 1
                else:
                    live_fail += 1
                try:
                    run_ref.update({"executed": live_ok + live_fail,
                                    "ok": live_ok, "fail": live_fail,
                                    "heartbeatAt": firestore.SERVER_TIMESTAMP,
                                    "lastProgressAt": firestore.SERVER_TIMESTAMP})
                except Exception:  # pylint: disable=broad-except
                    pass
    finally:
        stop_beat.set()
        proc.wait()

    ok = fail = 0
    saw_summary = False
    for ln in out_lines:
        if "SUMMARY" in ln:
            saw_summary = True
            for t in ln.split():
                if t.startswith("ok="):
                    ok = int(t[3:])
                if t.startswith("fail="):
                    fail = int(t[5:])
    # No SUMMARY means the batch was killed mid-flight (systemd TimeoutStartSec,
    # OOM, operator). The streamed counts are then the only honest record — the
    # old code left ok=fail=0 there, which the batchNoop guard below reads as
    # "the runner executed nothing" and shouts about plumbing that is fine.
    if not saw_summary:
        ok, fail = live_ok, live_fail
    p = proc
    log(f"batch done rc={p.returncode} ok={ok} fail={fail}"
        + ("" if saw_summary else " (no SUMMARY — batch cut short; streamed counts)"))

    # A batch that migrated NOTHING while we handed it work is broken plumbing,
    # not a quiet day — and it is invisible without this check: a job the runner
    # cannot resolve is SKIPPED, the runner still exits rc=0 ("no runnable
    # jobs"), and the run is journalled status="done" with ok=0. That is exactly
    # how the identity-IPv6 change silently stopped every fleet migration from
    # 2026-08-15 to the 18th while the record looked healthy. Shout instead.
    if verified and ok == 0 and fail == 0:
        log(f"ALERTA: se entregaron {len(verified)} migracion(es) al batch y NO se "
            f"ejecuto ninguna (ok=0 fail=0, rc={p.returncode}). El runner las esta "
            f"SALTANDO — revisa {LOG_DIR} y la resolucion de nodo origen; el defrag "
            f"no esta reorganizando nada.")
        try:
            db.collection("defrag_runs").document(run_id).set(
                {"batchNoop": True,
                 "batchNoopDetail": f"handed {len(verified)}, executed 0 (rc={p.returncode})"},
                merge=True)
        except Exception:  # pylint: disable=broad-except
            pass

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

    # Feed the victim-choice cooldown BEFORE reading failures: a failed
    # migration still froze/burdened its guest, so it counts as "recently
    # touched" too (and it lands on the skip-list anyway).
    record_recent_moves([v for v, *_ in verified])

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
