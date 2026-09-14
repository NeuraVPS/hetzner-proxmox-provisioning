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

RDP se sonda con un saludo X.224 real (Connection Request -> Connection
Confirm), no solo `connect()`. Medido 2026-09-14: TermService puede
encravarse dentro del invitado sin que el puerto deje de escuchar — el TCP
`connect()` sigue devolviendo OK mientras el cliente lleva horas sin poder
entrar. Un `connect()` a secas no distingue esto de un forward sano, así
que confirmó "todo bien" en tres casos reales de soporte. Los dos fallos que
puede dar la sonda de RDP quedan DIFERENCIADOS en el doc (nunca fundidos):
`kind=unreachable` (ni siquiera abre el TCP -> NAT/reenvío roto) frente a
`kind=rdp_not_negotiating` (abre el TCP pero no completa el handshake ->
invitado encravado). Son averías distintas con remedios distintos: la
primera se arregla convergiendo el NAT, la segunda reiniciando TermService
dentro del invitado; fundirlas le quita a la CF la información para elegir.
SMB no lleva saludo de protocolo (no lo necesita para este caso, y tocarlo
no era parte del problema medido): sigue siendo un `connect()` puro.

Exclusiones (spec del operador 2026-08-01):
  * VM con status != 'running' en Firestore (cliente puede apagarla).
  * maintenance == true (migración en curso).
  * reinstalling == true (reset_vm en curso: la VM pasa parada casi todo el
    rato y el guest se rehace, así que estar inalcanzable es lo ESPERADO).
  * firewall.rdpEnabled == false  -> no se sondea RDP (el cliente puede
    bloquearlo a propósito); ídem sambaEnabled para SMB. state.json refleja
    los flags, pero Firestore se re-consulta en los fallos como fuente de
    verdad.
  * vmid en EXCLUDED_VMIDS: `devel` (2988898) es la caja personal del
    operador, fuera de la flota — nunca en Firestore, pero el filtro es
    explícito y no depende de que siga siendo así.
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
    "0000000-BASE": "95.216.102.179",   # b0 sondea a través de b1 ECC (HEL)
    "0000001-BASE": "116.202.118.221",  # b1 sondea a través de b0 ECC (FSN)
}
# devel (Hetzner Robot #2988898): caja personal del operador, fuera de la
# flota (ver memory/neuravps-fleet-and-ssh-access.md). Nunca se sondea.
EXCLUDED_VMIDS = {2988898}
CONNECT_TIMEOUT = 3.0
# Saludo X.224 completo (connect + send + recv), no solo el connect. Algo más
# generoso que CONNECT_TIMEOUT a propósito: un invitado sano responde el
# Connection Confirm en milisegundos, así que esto casi nunca se agota contra
# una VM buena — cuando se agota es la propia señal (TermService encravado no
# contesta nunca, ni rápido ni despacio).
RDP_NEGOTIATE_TIMEOUT = 4.0
WORKERS = 48
REPROBE_DELAY_S = 60
ABORT_FAIL_PCT = 10.0
ABORT_FAIL_MIN = 20
DEDUPE_OPEN_H = 6    # doc abierto más joven que esto -> solo tocar lastSeenAt
DEDUPE_ALERT_H = 24  # doc con alertedAt más joven que esto -> no re-presentar

# X.224 Connection Request válido (TPKT + CR TPDU + RDP_NEG_REQ pidiendo
# protocolo estándar). Calibrado a mano 2026-09-14 contra 3 VMs sanas (dan
# Connection Confirm) y 2 encravadas (no responden nada) — el primer intento
# tenía el PDU mal formado y daba "no negocia" hasta en máquinas buenas, así
# que este exacto byte a byte es el que quedó verificado, no uno "parecido".
RDP_X224_CR = bytes([
    0x03, 0x00, 0x00, 0x13, 0x0e, 0xe0, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00,
])


def log(msg: str) -> None:
    print(f"conncheck: {msg}", flush=True)


def tcp_open(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def rdp_negotiates(host: str, port: int,
                   timeout: float = RDP_NEGOTIATE_TIMEOUT) -> "tuple[bool, str]":
    """Saludo X.224 real, no solo `connect()`. Devuelve (ok, reason).

    `reason` distingue las DOS averías que esto puede encontrar, a propósito
    para que quien escriba el doc de distress no las funda en una:
      * "tcp_closed"    -> ni abre el TCP (NAT/reenvío roto en la base).
      * "no_negotiate"  -> abre el TCP pero no completa el Connection
                            Confirm (TermService encravado en el invitado:
                            el puerto escucha, pero nadie habla el protocolo).
    Éxito: ok=True, reason="".
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            try:
                s.sendall(RDP_X224_CR)
                r = s.recv(64)
            except OSError:
                return False, "no_negotiate"
    except OSError:
        return False, "tcp_closed"
    if len(r) >= 11 and r[:2] == b"\x03\x00" and r[5] == 0xd0:
        return True, ""
    return False, "no_negotiate"


def fw_enabled(firewall, key) -> bool:
    # Espeja _firewall_flag_enabled de functions/n8n_handlers.py: ausente = ON.
    if not isinstance(firewall, dict):
        return True
    v = firewall.get(key)
    return True if v is None else bool(v)


def kind_for(reasons: dict) -> str:
    """`kind` del doc a partir de las razones por servicio de esa VM.

    Prioriza `rdp_not_negotiating`: si CUALQUIER servicio de la VM falló por
    no negociar el protocolo (solo puede ser rdp — SMB no lleva saludo), eso
    es un invitado encravado y hay que decirlo, aunque otro servicio de la
    misma VM haya fallado por TCP cerrado. `unreachable` es el valor de
    siempre (TCP cerrado) — sin razones "no_negotiate" el comportamiento no
    cambia respecto al script anterior, así que no rompe a quien ya lee ese
    valor.
    """
    if any(r == "no_negotiate" for r in reasons.values()):
        return "rdp_not_negotiating"
    return "unreachable"


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
        if vmid in EXCLUDED_VMIDS:
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

    def probe_job(job):
        """(ok, reason) para UN job. RDP hace el saludo X.224; SMB sigue
        siendo `connect()` puro — no se toca su lógica. Nunca deja escapar
        una excepción: una sonda rota no puede tumbar el barrido entero."""
        vmid, service, port = job
        try:
            if service == "rdp":
                return rdp_negotiates(peer, port)
            ok = tcp_open(peer, port)
            return ok, ("" if ok else "tcp_closed")
        except Exception as exc:  # noqa: BLE001
            log(f"excepción sondeando vm {vmid} {service}:{port} — {exc!r}")
            return False, "probe_error"

    def round_probe(job_list):
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = list(ex.map(probe_job, job_list))
        return [(j, reason) for j, (ok, reason) in zip(job_list, results) if not ok]

    t0 = time.time()
    fails = round_probe(jobs)
    log(f"ronda 1: {len(jobs)} sondas via {peer}, {len(fails)} fallos, {time.time()-t0:.0f}s")
    if fails:
        time.sleep(REPROBE_DELAY_S)
        fails = round_probe([j for j, _reason in fails])
        log(f"ronda 2 (tras {REPROBE_DELAY_S}s): {len(fails)} fallos persistentes")

    # --- confirmación contra Firestore fresco ------------------------------
    by_vm = {}  # vmid -> {service: reason}
    for (vmid, service, _port), reason in fails:
        by_vm.setdefault(vmid, {})[service] = reason
    confirmed = {}
    for vmid, svc_reasons in by_vm.items():
        snap = db.collection("servers").document(running[vmid]["docId"]).get()
        d = snap.to_dict() or {}
        prov = d.get("provisioningStatus")
        if ((d.get("status") or "").strip().lower() != "running"
                or d.get("maintenance")
                or d.get("reinstalling") is True
                or (prov is not None and prov != "provisioned")):
            continue  # cambió mientras sondeábamos (migración, reinstall, o aún instalándose)
        fw = d.get("firewall") or {}
        keep = [s for s in svc_reasons
                if fw_enabled(fw, "rdpEnabled" if s == "rdp" else "sambaEnabled")]
        if keep:
            confirmed[vmid] = {
                "services": sorted(keep),
                "ipv6": d.get("ipv6"),
                "reasons": {s: svc_reasons[s] for s in keep},
            }

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

    def file_doc(vmid: int, kind: str, services, ipv6, reasons=None):
        nonlocal filed, touched
        payload = {
            "vmid": vmid, "kind": kind, "services": list(services),
            "expectedIpv6": ipv6, "probedVia": peer, "reportedBy": hostname,
            "state": "open", "createdAt": firestore.SERVER_TIMESTAMP,
            "lastSeenAt": firestore.SERVER_TIMESTAMP, "resolvedAt": None,
        }
        # Campo aditivo — nadie más lo lee hoy, no rompe a quien solo mira
        # `kind`/`services`. Le da a la CF el detalle por servicio (p.ej. rdp
        # sin negociar + smb con TCP cerrado a la vez) sin tener que
        # reconstruirlo a partir de un único `kind` por VM.
        if reasons:
            payload["serviceReasons"] = dict(reasons)
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
    rdp_wedged = 0
    for vmid, info in sorted(confirmed.items()):
        kind = kind_for(info["reasons"])
        if kind == "rdp_not_negotiating":
            rdp_wedged += 1
        file_doc(vmid, kind, info["services"], info["ipv6"], info["reasons"])

    log(f"fin: {probed_vms} VMs sondeadas, {len(missing)} sin entrada NAT, "
        f"{len(confirmed)} inalcanzables confirmadas "
        f"(tcp_closed={len(confirmed) - rdp_wedged} rdp_not_negotiating={rdp_wedged}), "
        f"{filed} presentadas, {touched} ya en curso, "
        f"{resolved} resueltas{' [DRY-RUN]' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
