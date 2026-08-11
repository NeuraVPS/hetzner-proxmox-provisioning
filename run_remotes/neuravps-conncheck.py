#!/usr/bin/env python3
#NCCVER=1
"""neuravps-conncheck — sweep horario de conectividad por VM (lado sonda, corre en cada BASE).

Sondea el forward RDP/SMB de cada VM gestionada A TRAVÉS DE LA IP PRINCIPAL v4
DE LA BASE PEER: una conexión originada localmente hacia la PROPIA main IP se
salta el hook prerouting (donde vive el DNAT), así que auto-sondearse no prueba
nada. Sondear a la peer ejercita SU ruta de cliente completa:
v4 DNAT -> jool NAT46 -> mapa ip6 dport -> nodo -> guest. Las dos bases juntas
cubren ambas pilas NAT en cada ciclo (b0 valida a b1 y viceversa).

Este script NO remedia. Las discrepancias confirmadas se escriben en Firestore
`connectivity_distress/{vmid}`; la Cloud Function conncheck es dueña de la
escalera de remediación (converge NAT -> re-probe -> drift IPv6 in-guest ->
email a soporte). Kill-switch: `config/conncheck` {enabled, dryRun} — doc
ausente o enabled!=true = APAGADO (sistema nuevo, fallo cerrado).

Exclusiones (spec del operador 2026-08-01):
  * VM con status != 'running' en Firestore (cliente puede apagarla).
  * maintenance == true (migración en curso).
  * reinstalling == true (reset_vm en curso: la VM pasa parada casi todo el
    rato y el guest se rehace, así que estar inalcanzable es lo ESPERADO).
  * firewall.rdpEnabled == false  -> no se sondea RDP (el cliente puede
    bloquearlo a propósito); ídem sambaEnabled para SMB. state.json refleja
    los flags, pero Firestore se re-consulta en los fallos como fuente de
    verdad.
  * Una VM 'running' SIN entrada en el state.json local ES una discrepancia
    (kind=nat_mapping_missing).

Guardas anti-falsa-alarma:
  * pre-flight TCP 22 a la peer; si no responde, se aborta el sweep.
  * si tras el re-probe fallan > ABORT_FAIL_PCT% de las VMs sondeadas (y más
    de ABORT_FAIL_MIN), se asume problema de ruta/base y se ABORTA sin
    escribir nada (una base caída ya la alerta el failover watchdog).
  * cada fallo se re-sondea a los REPROBE_DELAY_S para filtrar transitorios
    (p.ej. el blackout de cutover de una migración).

Requiere en la base: /var/lib/base-nat/state.json, /etc/firebase-credentials.json,
y la main v4 de esta base en el set `bf_allow` de la peer (si no, sweepguard
bloquearía la IP por barrer ~1800 puertos).
"""
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

STATE_FILE = os.environ.get("BASE_NAT_STATE", "/var/lib/base-nat/state.json")
CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
# main v4 de la OTRA base (constantes de topología; ver memoria dual-región)
PEER_V4 = {
    "0000000-BASE": "37.27.135.250",   # b0 sondea a través de b1 (HEL)
    "0000001-BASE": "188.40.153.120",  # b1 sondea a través de b0 (FSN)
}
CONNECT_TIMEOUT = 3.0
WORKERS = 48
REPROBE_DELAY_S = 60
ABORT_FAIL_PCT = 10.0
ABORT_FAIL_MIN = 20
DEDUPE_OPEN_H = 6    # doc abierto más joven que esto -> solo tocar lastSeenAt
DEDUPE_ALERT_H = 24  # doc con alertedAt más joven que esto -> no re-presentar


def log(msg: str) -> None:
    print(f"conncheck: {msg}", flush=True)


def tcp_open(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def fw_enabled(firewall, key) -> bool:
    # Espeja _firewall_flag_enabled de functions/n8n_handlers.py: ausente = ON.
    if not isinstance(firewall, dict):
        return True
    v = firewall.get(key)
    return True if v is None else bool(v)


def resolution_for(vmid, confirmed_vmids, probed_vmids, running_vmids):
    """Motivo para CERRAR un doc de distress abierto, o None para dejarlo.

    Un doc de distress solo se cerraba antes cuando la CF lo arreglaba
    (converge/auto-fix). Si la VM se recuperaba SOLA entre barridos (parpadeo
    de RDP, la caja terminó de arrancar, el cliente rebooteó), el doc quedaba
    abierto para siempre como `exhausted_alerted` — ensuciando la vista y tras
    haber mandado ya un email. Este barrido, que acaba de re-sondear toda la
    flota, es quien tiene la verdad para cerrarlo. SOLO se llama en un barrido
    COMPLETO (el guard de abort ya nos habría sacado antes).
      * en `confirmed`  -> sigue caída, se deja abierta.
      * la sondeamos y NO está en `confirmed` -> responde -> `recovered`.
      * ya no está entre las running/entregadas (parada, borrada, en
        mantenimiento, reinstalando) -> el aviso dejó de aplicar.
      * running pero no sondeada este ciclo (p.ej. el cliente deshabilitó el
        servicio) -> conservador, se deja abierta.
    """
    if vmid in confirmed_vmids:
        return None
    if vmid in probed_vmids:
        return "recovered"
    if vmid not in running_vmids:
        return "no_longer_applicable"
    return None


def main() -> int:
    hostname = socket.gethostname()
    peer = PEER_V4.get(hostname)
    if not peer:
        log(f"host {hostname} no es una BASE conocida — nada que hacer")
        return 0

    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(CREDS))
    db = firestore.client()

    cfg = (db.collection("config").document("conncheck").get().to_dict() or {})
    if cfg.get("enabled") is not True:
        log("config/conncheck.enabled != true — sweep apagado")
        return 0
    dry = bool(cfg.get("dryRun"))

    with open(STATE_FILE) as fh:
        state = {int(k): v for k, v in json.load(fh).items()}

    # --- VMs running según Firestore (fuente de verdad de status/flags) ----
    try:
        from google.cloud.firestore_v1 import FieldFilter
        q = db.collection("servers").where(filter=FieldFilter("status", "==", "running"))
    except ImportError:
        q = db.collection("servers").where("status", "==", "running")
    running = {}  # vmid -> {docId, maintenance, rdpEnabled, sambaEnabled}
    for snap in q.select(["proxmoxId", "maintenance", "firewall",
                          "provisioningStatus", "reinstalling"]).stream():
        d = snap.to_dict() or {}
        try:
            vmid = int(d.get("proxmoxId"))
        except (TypeError, ValueError):
            continue
        # Una VM REINSTALÁNDOSE está parada casi todo el proceso y su guest se
        # rehace entero (adiós IPv6 in-guest, adiós RDP/SMB). `reset_vm` deja
        # `status` en 'running' y `provisioningStatus` en 'provisioned', así que
        # sin este filtro se cuela (vm 2032 el 2026-08-11: reinstall 11:44→12:08
        # UTC, doc presentado a las 12:06 y email a las 12:08:09, dos segundos
        # después de que la VM volviera a arrancar).
        if d.get("reinstalling") is True:
            continue
        # Una VM a MEDIO APROVISIONAR está legítimamente inalcanzable: el
        # doc ya dice status=running (la VM arrancó) minutos antes de que el
        # instalador le configure la IPv6. Sondearla genera una alerta que se
        # resuelve sola (vms 1069/2008/2016 el 2026-08-03: la de 2016 se
        # presentó a las 18:36 y el provisioning acabó a las 18:38). Solo se
        # vigila lo YA ENTREGADO; los docs viejos sin el campo pasan.
        prov = d.get("provisioningStatus")
        if prov is not None and prov != "provisioned":
            continue
        fw = d.get("firewall") or {}
        running[vmid] = {
            "docId": snap.id,
            "maintenance": bool(d.get("maintenance")),
            "rdpEnabled": fw_enabled(fw, "rdpEnabled"),
            "sambaEnabled": fw_enabled(fw, "sambaEnabled"),
        }

    # --- running sin entrada NAT local = discrepancia por sí misma ---------
    missing = [
        v for v, r in sorted(running.items())
        if v not in state and not r["maintenance"]
    ]

    # --- plan de sondas ----------------------------------------------------
    jobs = []  # (vmid, service, port)
    for vmid, st in sorted(state.items()):
        r = running.get(vmid)
        if not r or r["maintenance"]:
            continue  # apagada/pausada/borrada o migrando: fuera del chequeo
        if st.get("rdpEnabled") and r["rdpEnabled"] and st.get("rdp"):
            jobs.append((vmid, "rdp", int(st["rdp"])))
        if st.get("sambaEnabled") and r["sambaEnabled"] and st.get("samba"):
            jobs.append((vmid, "smb", int(st["samba"])))

    if not tcp_open(peer, 22, 4.0):
        log(f"ABORT: peer {peer} no responde ni al 22 — sweep cancelado")
        return 0

    def round_probe(job_list):
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            oks = list(ex.map(lambda j: tcp_open(peer, j[2]), job_list))
        return [j for j, ok in zip(job_list, oks) if not ok]

    t0 = time.time()
    fails = round_probe(jobs)
    log(f"ronda 1: {len(jobs)} sondas via {peer}, {len(fails)} fallos, {time.time()-t0:.0f}s")
    if fails:
        time.sleep(REPROBE_DELAY_S)
        fails = round_probe(fails)
        log(f"ronda 2 (tras {REPROBE_DELAY_S}s): {len(fails)} fallos persistentes")

    # --- confirmación contra Firestore fresco ------------------------------
    by_vm = {}
    for vmid, service, _port in fails:
        by_vm.setdefault(vmid, []).append(service)
    confirmed = {}
    for vmid, services in by_vm.items():
        snap = db.collection("servers").document(running[vmid]["docId"]).get()
        d = snap.to_dict() or {}
        prov = d.get("provisioningStatus")
        if ((d.get("status") or "").strip().lower() != "running"
                or d.get("maintenance")
                or d.get("reinstalling") is True
                or (prov is not None and prov != "provisioned")):
            continue  # cambió mientras sondeábamos (migración, reinstall, o aún instalándose)
        fw = d.get("firewall") or {}
        keep = [s for s in services
                if fw_enabled(fw, "rdpEnabled" if s == "rdp" else "sambaEnabled")]
        if keep:
            confirmed[vmid] = {"services": sorted(keep), "ipv6": d.get("ipv6")}

    probed_ids = {j[0] for j in jobs}
    probed_vms = len(probed_ids)
    if len(confirmed) > ABORT_FAIL_MIN and probed_vms and \
            100.0 * len(confirmed) / probed_vms > ABORT_FAIL_PCT:
        log(f"ABORT: {len(confirmed)}/{probed_vms} VMs fallan — huele a ruta/base, "
            "no a VMs individuales. Sin escrituras (base caída la alerta el watchdog).")
        return 0

    # --- presentar discrepancias ------------------------------------------
    now = datetime.now(timezone.utc)
    coll = db.collection("connectivity_distress")
    filed = touched = resolved = 0

    # Cerrar los docs abiertos de VMs que ya se recuperaron (o dejaron de
    # aplicar). Barrido completo => tenemos la verdad fresca. Sin esto, un
    # parpadeo que se auto-cura deja el doc abierto para siempre (vms 1205,
    # 2004, 2016, 337, 719 el 2026-08-05: accesibles pero con doc rancio).
    confirmed_ids = set(confirmed)
    running_ids = set(running)
    if not dry:
        try:
            from google.cloud.firestore_v1 import FieldFilter
            openq = coll.where(filter=FieldFilter("resolvedAt", "==", None))
        except ImportError:
            openq = coll.where("resolvedAt", "==", None)
        for snap in openq.stream():
            d = snap.to_dict() or {}
            try:
                vmid = int(d.get("vmid", snap.id))
            except (TypeError, ValueError):
                continue
            reason = resolution_for(vmid, confirmed_ids, probed_ids, running_ids)
            if reason is None:
                continue
            snap.reference.update({
                "resolvedAt": firestore.SERVER_TIMESTAMP,
                "resolvedBy": hostname,
                "resolution": reason,
            })
            resolved += 1
            log(f"resuelto: vm {vmid} ({reason})")

    def file_doc(vmid: int, kind: str, services, ipv6):
        nonlocal filed, touched
        payload = {
            "vmid": vmid, "kind": kind, "services": list(services),
            "expectedIpv6": ipv6, "probedVia": peer, "reportedBy": hostname,
            "state": "open", "createdAt": firestore.SERVER_TIMESTAMP,
            "lastSeenAt": firestore.SERVER_TIMESTAMP, "resolvedAt": None,
        }
        if dry:
            log(f"DRY-RUN presentaría: vm {vmid} {kind} {services}")
            return
        ref = coll.document(str(vmid))
        snap = ref.get()
        if snap.exists:
            d = snap.to_dict() or {}
            created = d.get("createdAt")
            alerted = d.get("alertedAt")
            fresh_open = (not d.get("resolvedAt") and created
                          and now - created < timedelta(hours=DEDUPE_OPEN_H))
            recently_alerted = (alerted
                                and now - alerted < timedelta(hours=DEDUPE_ALERT_H))
            if fresh_open or recently_alerted:
                ref.update({"lastSeenAt": firestore.SERVER_TIMESTAMP})
                touched += 1
                return
            ref.delete()  # resuelto/viejo: recrear para re-disparar el trigger
        ref.set(payload)
        filed += 1
        log(f"presentado: vm {vmid} {kind} {services}")

    for vmid in missing:
        r = running[vmid]
        svcs = [s for s, on in (("rdp", r["rdpEnabled"]), ("smb", r["sambaEnabled"])) if on]
        if svcs:
            file_doc(vmid, "nat_mapping_missing", svcs, None)
    for vmid, info in sorted(confirmed.items()):
        file_doc(vmid, "unreachable", info["services"], info["ipv6"])

    log(f"fin: {probed_vms} VMs sondeadas, {len(missing)} sin entrada NAT, "
        f"{len(confirmed)} inalcanzables confirmadas, {filed} presentadas, "
        f"{touched} ya en curso, {resolved} resueltas{' [DRY-RUN]' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
