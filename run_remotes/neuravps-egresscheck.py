#!/usr/bin/env python3
#NEGVER=1
"""neuravps-egresscheck — ¿llegan los invitados a Internet? (lado sonda, corre en cada BASE).

POR QUE EXISTE
Toda la vigilancia de la flota pregunta lo mismo: "¿llego YO a la maquina?"
conncheck sondea el forward RDP/SMB, node_liveness el sshd del nodo, el
failover watchdog las IPs de las bases. Nadie preguntaba lo contrario.

Y esa es justo la averia que tuvimos: al perderse el clamp MSS de los nodos, el
RDP seguia entrando —el cliente accedia a su VPS, y nuestras sondas tambien—
pero desde dentro SQX no podia descargar datos ni validar licencia. Cuatro
semaforos en verde durante dias mientras el cliente veia "cannot connect to
internet", porque no habia ningun sitio donde eso pudiera aparecer.

`node_health` gano despues un contador de reglas maxseg, pero eso comprueba la
CONFIGURACION de esa causa concreta. Esto comprueba el COMPORTAMIENTO, y por
tanto tambien las causas que aun no conocemos.

LAS CUATRO DECISIONES QUE LO HACEN UTIL EN VEZ DE RUIDO

1. Se ejecuta DENTRO del invitado. El camino real es invitado -> nodo -> tunel
   -> base -> Internet. Una sonda que arranque en el nodo o en la base se salta
   hops, y son justo los hops donde se rompio.

2. Se ejecuta por el AGENTE (virtio-serial), no por SSH al invitado. El canal
   de control NO debe compartir el modo de fallo que se esta midiendo: si la
   red del invitado esta rota, un canal que dependa de esa red no da un
   diagnostico, da un silencio. El agente va por puerto serie virtual y llega
   igual con la red muerta. Sin agente = NO MEDIBLE, que no es lo mismo que
   fallo (y se cuenta aparte, ver COBERTURA).

3. Se transfieren BYTES DE VERDAD, no un ping. Esto es lo que no es obvio:
   durante el corte, un ping y un handshake TCP pasaban tan ricamente. Solo
   fallaban los paquetes grandes. Una sonda de "¿abre el puerto?" habria dado
   verde durante toda la averia. Se bajan 32 KB (~23 segmentos a tamaño
   completo), que es lo que obliga al camino a mover MTU llena.

4. La alerta distingue LO NUESTRO de lo del cliente por el TAMAÑO, no por el
   numero de VMs. Se bajan dos cosas: 1 KB (cabe en un segmento) y 32 KB (no).

     1 KB va y 32 KB no  -> el camino esta roto. Un firewall de cliente no
                            puede producir eso: bloquea los dos o ninguno.
                            Es NUESTRO, y basta UNA VM para alertar.
     van los dos pero
     tardando segundos   -> el MISMO camino roto, cuando el destino sabe
                            sortearlo retransmitiendo. Tambien NUESTRO. Sin
                            esta firma la sonda daba VERDE con el clamp
                            quitado: comprobado en produccion el 18-08-2026.
     fallan los dos      -> ambiguo (puede ser el cliente cerrandose el
                            firewall). Se exigen >=2 VMs del mismo nodo.

   Esto importa mas de lo que parece: 115 de los 217 nodos con VMs tienen UNA
   SOLA VM —son los VPS-E, dedicados, el plan mas caro— y con una regla de
   ">=2 VMs fallando" no habrian podido disparar una alerta jamas. El tamaño
   es lo que los devuelve al radar.

   Si falla media flota se presenta UN solo doc global en vez de 227.

EL CONTROL, QUE ES LO QUE EVITA LA FALSA ALARMA
El destino es de un tercero (speed.cloudflare.com/__down, que existe para que
le midan y sirve un numero exacto de bytes por v4 y v6). Si Cloudflare se cae,
la flota entera "fallaria" a la vez. Por eso la BASE se descarga lo mismo antes
de empezar: si la base tampoco puede, el destino esta caido y se aborta sin
escribir nada. Sin ese control esto seria una maquina de falsas alarmas.

COBERTURA
Si el agente muere en muchas VMs, la sonda mediria cada vez menos y seguiria
diciendo "verde" — el fallo silencioso mas caro de todos. Por eso cada pasada
registra cuantas midio y cuantas no pudo, y el resumen lo dice siempre.

Kill-switch `config/egresscheck` {enabled, dryRun, vmsPorNodo}: doc ausente o
enabled!=true = APAGADO (sistema nuevo, falla cerrado).

Reparto: cada base sondea los nodos de SU region (b0=Falkenstein,
b1=Helsinki). Que una base caida deje su region sin sondear no es un hueco:
eso ya lo alerta el failover watchdog, y mucho antes.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
NODES_FILE = os.environ.get("PVE_NODES_FILE", "/var/lib/base-nat/pve_nodes.json")

# Region que sonda cada base (constantes de topologia; ver memoria dual-region).
REGION_DE_BASE = {
    "0000000-BASE": "falkenstein",
    "0000001-BASE": "helsinki",
}

# Los dos tamaños son el corazon del diagnostico, no una optimizacion:
#   GRANDE 32 KB -> ~23 segmentos a tamaño completo. Obliga al camino a mover
#                   MTU llena, que es donde se rompio.
#   PEQUEÑO 1 KB -> cabe en UN segmento. Pasa por caminos donde el grande se
#                   atraganta, y es lo que separa "camino roto" (nuestro) de
#                   "bloqueado" (del cliente).
BYTES = 32768
BYTES_MIN = 1024
# ⚠️ EL RELOJ ES PARTE DEL VEREDICTO, no un adorno.
# Medido el 18-08-2026 quitando la tabla del clamp en un nodo sin clientes: las
# descargas SEGUIAN entregandose (200 y los bytes enteros) pero tardaban 6,5 s
# en vez de 0,06 s — el backoff de retransmision 1+2+4 que describe nvx-mss.sh.
# Cloudflare sortea muy bien los caminos con la PMTU rota y acaba sirviendo;
# api.strategyquant.com, que es lo que usa el cliente, se quedaba en 000 a los
# 25 s. O sea: mirando solo codigo y bytes, la sonda daba VERDE con la averia
# puesta. Con 1 KB por un camino sano se esta en centesimas desde cualquier
# sitio de Europa, asi que 3 s no es "lento": es un camino retransmitiendo.
UMBRAL_LENTO = 3.0
URL = f"https://speed.cloudflare.com/__down?bytes={BYTES}"
URL_MIN = f"https://speed.cloudflare.com/__down?bytes={BYTES_MIN}"
NOMBRE_DNS = "speed.cloudflare.com"

VMS_POR_NODO = 2
WORKERS = 24
AGENTE_TIMEOUT = 60
REPROBE_DELAY_S = 45
# Por encima de esto la culpa no es de los nodos: es nuestra o del destino, y
# se presenta UN doc global. 227 correos por una averia unica ya nos paso.
GLOBAL_PCT = 30.0
DEDUPE_OPEN_H = 6
DEDUPE_ALERT_H = 24


def log(msg: str) -> None:
    print(f"egresscheck: {msg}", flush=True)


# --- la sonda que corre DENTRO del invitado -----------------------------------
# Sale por las dos pilas a proposito. Los invitados son IPv6 nativos (modelo
# IDENT) y su IPv4 pasa por el NAT de la base: son dos caminos distintos que se
# rompen por motivos distintos, y saber CUAL fallo es medio diagnostico hecho.
# Se pide tambien el codigo de salida de curl porque distingue lo importante:
# 28 (timeout con la conexion hecha) es la firma del MSS roto, mientras que 7
# (no conecta) apunta a ruta o firewall.
_W = "-w '%{http_code}/%{size_download}/%{time_total}'"
_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ag=curl.exe -4 -s -o NUL --max-time 25 " + _W + " '" + URL + "';$rag=$LASTEXITCODE;"
    "$ap=curl.exe -4 -s -o NUL --max-time 15 " + _W + " '" + URL_MIN + "';$rap=$LASTEXITCODE;"
    "$bg=curl.exe -6 -s -o NUL --max-time 25 " + _W + " '" + URL + "';$rbg=$LASTEXITCODE;"
    "$bp=curl.exe -6 -s -o NUL --max-time 15 " + _W + " '" + URL_MIN + "';$rbp=$LASTEXITCODE;"
    "$d=try{(Resolve-DnsName " + NOMBRE_DNS + " -Type A -EA Stop|"
    "Select-Object -First 1).IPAddress}catch{'FALLO'};"
    "'v4g='+$ag+'/'+$rag+' v4p='+$ap+'/'+$rap+"
    "' v6g='+$bg+'/'+$rbg+' v6p='+$bp+'/'+$rbp+' dns='+$d"
)


def _enc(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode()


def por_agente(node_ip: str, vmid: int):
    """(ok, salida) ejecutando PowerShell por el agente. ok=False = no medible."""
    try:
        r = subprocess.run(
            ["ssh", "-n", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", f"root@{node_ip}",
             f"qm guest exec {vmid} --timeout {AGENTE_TIMEOUT} -- "
             f"powershell.exe -NoProfile -EncodedCommand {_enc(_PS)}"],
            capture_output=True, text=True, errors="replace", timeout=AGENTE_TIMEOUT + 30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"ssh/{type(e).__name__}"
    s = (r.stdout or "").strip()
    if '"exitcode"' not in s:
        return False, " ".join((s + " " + (r.stderr or "")).split())[:100]
    try:
        d = json.loads(s)
        return True, (d.get("out-data") or d.get("err-data") or "").strip()
    except (ValueError, AttributeError):
        return False, " ".join(s.split())[:100]


def _descarga(campo: str, esperados: int):
    """(ok, motivo, segundos) de UNA descarga."""
    try:
        http, bytes_, t, rc = campo.split("/")
        http, bytes_, rc, t = int(http), int(bytes_), int(rc), float(t)
    except (ValueError, AttributeError):
        return False, "ilegible", 0.0
    if http == 200 and bytes_ >= esperados:
        return True, "", t
    if rc == 28:
        return False, ("truncado" if bytes_ > 0 else "timeout"), t
    if rc == 7 or http == 0:
        return False, "sin_conexion", t
    return False, f"curl{rc}", t


def veredicto_pila(grande: str, pequeno: str):
    """'ok' | 'mtu' | 'lento' | 'cortado' | 'raro'  para una pila (v4 o v6).

    Aqui vive todo el diagnostico, y son TRES firmas distintas de lo mismo:

      mtu    el pequeño pasa y el grande no. Un cliente no puede fabricar eso:
             un firewall bloquea los dos tamaños o ninguno.
      lento  las dos entregan, pero tardando segundos. Es el mismo camino roto
             cuando el destino sabe sortearlo a base de retransmitir. Sin esta
             firma la sonda daba VERDE con el clamp quitado (medido).
      cortado  no pasa nada de nada. Ambiguo: puede ser el cliente.

    Las dos primeras señalan a nuestro lado sin ambiguedad y bastan por si
    solas para alertar, incluso en un nodo de una sola VM.
    """
    og, _mg, tg = _descarga(grande, BYTES)
    op, _mp, tp = _descarga(pequeno, BYTES_MIN)
    if og and op:
        # Un 1 KB no tarda segundos por un camino sano. Se mira sobre todo el
        # pequeño: si ESE va lento, no es ancho de banda, es retransmision.
        return "lento" if (tp > UMBRAL_LENTO or tg > UMBRAL_LENTO) else "ok"
    if op and not og:
        return "mtu"       # NUESTRO: el camino no traga paquete lleno
    if not op and not og:
        return "cortado"   # ambiguo: puede ser el firewall del cliente
    return "raro"          # el grande va y el pequeño no: ruido, no se alerta


def analiza(salida: str):
    """(ok, detalle). detalle['kind'] decide cuanta evidencia hace falta."""
    partes = dict(p.split("=", 1) for p in salida.split() if "=" in p)
    v4 = veredicto_pila(partes.get("v4g", ""), partes.get("v4p", ""))
    v6 = veredicto_pila(partes.get("v6g", ""), partes.get("v6p", ""))
    dns = "ok" if partes.get("dns", "FALLO") != "FALLO" else "FALLO"
    ok = (v4 == "ok" and v6 == "ok" and dns == "ok")
    # `mtu` y `lento` mandan sobre `cortado`: son las firmas que señalan
    # inequivocamente a nuestro lado, y una sola VM con ellas ya alerta.
    if "mtu" in (v4, v6):
        kind = "mtu"
    elif "lento" in (v4, v6):
        kind = "lento"
    else:
        kind = "cortado" if not ok else "ok"
    return ok, {"v4": v4, "v6": v6, "dns": dns, "kind": kind}


def control_desde_la_base() -> bool:
    """El destino es de un tercero. Si la base tampoco lo baja, el caido es el
    destino y no nosotros — y sondear la flota solo produciria 227 mentiras."""
    for intento in range(2):
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}/%{size_download}",
                 "--max-time", "20", URL],
                capture_output=True, text=True, timeout=30)
            http, bytes_ = (r.stdout or "0/0").split("/")
            if int(http) == 200 and int(bytes_) >= BYTES:
                return True
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        if intento == 0:
            time.sleep(10)
    return False


def main() -> int:
    hostname = socket.gethostname()
    region = REGION_DE_BASE.get(hostname)
    if not region:
        log(f"host {hostname} no es una BASE conocida — nada que hacer")
        return 0

    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(CREDS))
    db = firestore.client()

    cfg = (db.collection("config").document("egresscheck").get().to_dict() or {})
    if cfg.get("enabled") is not True:
        log("config/egresscheck.enabled != true — sonda apagada")
        return 0
    dry = bool(cfg.get("dryRun"))
    por_nodo = int(cfg.get("vmsPorNodo") or VMS_POR_NODO)

    if not control_desde_la_base():
        log(f"ABORT: la propia base no baja {URL} — el destino esta caido, "
            "no la flota. Sin escrituras.")
        return 0

    with open(NODES_FILE) as fh:
        ips_nodo = json.load(fh)

    # --- nodos de MI region -------------------------------------------------
    nodos = {}
    for snap in db.collection("proxmox_nodes").stream():
        d = snap.to_dict() or {}
        if d.get("decommissioned"):
            continue
        if str(d.get("location") or "").strip().lower() != region:
            continue
        ip = ips_nodo.get(snap.id)
        if ip:
            nodos[snap.id] = ip
    if not nodos:
        log(f"no hay nodos de {region} en el mapa NAT — nada que sondear")
        return 0

    # --- candidatas: VMs entregadas y encendidas ----------------------------
    # Mismas exclusiones que conncheck y por los mismos motivos: una VM
    # reinstalandose o a medio aprovisionar esta legitimamente sin red, y
    # medirla solo genera una alarma que se resuelve sola.
    try:
        from google.cloud.firestore_v1 import FieldFilter
        q = db.collection("servers").where(filter=FieldFilter("status", "==", "running"))
    except ImportError:
        q = db.collection("servers").where("status", "==", "running")
    por_nodo_cands = {}
    for snap in q.select(["proxmoxId", "nodeId", "maintenance",
                          "provisioningStatus", "reinstalling"]).stream():
        d = snap.to_dict() or {}
        nid = d.get("nodeId")
        if nid not in nodos or d.get("maintenance") or d.get("reinstalling") is True:
            continue
        prov = d.get("provisioningStatus")
        if prov is not None and prov != "provisioned":
            continue
        try:
            por_nodo_cands.setdefault(nid, []).append(int(d.get("proxmoxId")))
        except (TypeError, ValueError):
            continue

    # --- muestreo rotatorio -------------------------------------------------
    # Determinista dentro de la pasada (mismo nodo -> mismo orden) pero girando
    # con la hora, para que a lo largo del dia se acabe mirando toda la flota y
    # no siempre las mismas dos VMs — que serian justo las dos que podrian
    # estar sanas mientras el resto del nodo no lo esta.
    # EGRESS_HORA fija el giro para poder reproducir un muestreo concreto
    # (probar que la sonda SALTA exige elegir a dedo que VMs se miran). En
    # produccion no se define y manda el reloj.
    hora = int(os.environ.get("EGRESS_HORA") or datetime.now(timezone.utc).hour)
    trabajos = []
    for nid, vms in por_nodo_cands.items():
        vms.sort()
        if not vms:
            continue
        off = hora % len(vms)
        elegidas = [vms[(off + i) % len(vms)] for i in range(min(por_nodo, len(vms)))]
        for v in elegidas:
            trabajos.append((nid, v))

    log(f"{region}: {len(nodos)} nodos, {sum(len(v) for v in por_nodo_cands.values())} "
        f"VMs candidatas, {len(trabajos)} sondas (hasta {por_nodo}/nodo, giro h={hora})")

    def sondear(job):
        nid, vmid = job
        ok_canal, salida = por_agente(nodos[nid], vmid)
        if not ok_canal:
            return (nid, vmid, None, salida)      # no medible
        ok, det = analiza(salida)
        return (nid, vmid, ok, det if not ok else salida)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        res = list(ex.map(sondear, trabajos))
    medidas = [r for r in res if r[2] is not None]
    sin_agente = [r for r in res if r[2] is None]
    fallos = [r for r in medidas if r[2] is False]
    log(f"ronda 1: {len(medidas)} medidas, {len(sin_agente)} sin agente, "
        f"{len(fallos)} fallos, {time.time()-t0:.0f}s")

    # Re-sonda para filtrar transitorios (un reinicio del invitado, un pico).
    if fallos:
        time.sleep(REPROBE_DELAY_S)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            res2 = list(ex.map(sondear, [(r[0], r[1]) for r in fallos]))
        mudas = [r for r in res2 if r[2] is None]
        fallos = [r for r in res2 if r[2] is False]
        log(f"ronda 2 (tras {REPROBE_DELAY_S}s): {len(fallos)} fallos persistentes")
        if mudas:
            # Una VM que fallaba y ahora no contesta NO se cuenta como
            # recuperada: no lo sabemos. Se deja fuera (conservador) pero se
            # dice, porque si no un agente muriendose parece una mejoria.
            log(f"ronda 2: {len(mudas)} VM(s) dejaron de contestar al agente "
                f"tras fallar — no se alerta por ellas, pero NO son recuperadas: "
                f"{[m[1] for m in mudas][:10]}")

    # --- COBERTURA: decirlo siempre, aunque todo este verde -----------------
    cobertura = 100.0 * len(medidas) / max(len(trabajos), 1)
    log(f"cobertura: {cobertura:.0f}% ({len(medidas)}/{len(trabajos)} sondas "
        f"contestaron por el agente)")

    # --- agregacion POR NODO ------------------------------------------------
    medidas_por_nodo = {}
    for nid, _v, ok, _d in medidas:
        medidas_por_nodo.setdefault(nid, []).append(ok)
    fallos_por_nodo = {}
    for nid, vmid, _ok, det in fallos:
        fallos_por_nodo.setdefault(nid, []).append({"vmid": vmid, "detalle": det})

    # DOS reglas, y la primera es la que salva a los 115 nodos de una sola VM:
    #   `mtu`     -> el pequeño pasa y el grande no. Un cliente no puede
    #                fabricar eso. Basta UNA VM.
    #   `cortado` -> ambiguo. Se exigen >=2 VMs fallando del mismo nodo, con
    #                >=2 medidas, para no alertar por el cliente que se ha
    #                cerrado el firewall.
    culpables = {}
    for nid, v in fallos_por_nodo.items():
        nuestras = [x for x in v
                    if (x.get("detalle") or {}).get("kind") in ("mtu", "lento")]
        if nuestras:
            regla = (nuestras[0].get("detalle") or {}).get("kind")
            culpables[nid] = {"vms": v, "regla": regla, "certeza": "alta"}
        elif len(v) >= 2 and len(medidas_por_nodo.get(nid, [])) >= 2:
            culpables[nid] = {"vms": v, "regla": "varias_vms", "certeza": "media"}

    nodos_medidos = len(medidas_por_nodo)
    # Punto ciego DECLARADO, y ahora mucho mas estrecho: los nodos de una sola
    # VM SI pueden disparar por `mtu`; lo que no pueden es disparar por
    # `cortado`, que es el veredicto ambiguo. Se dice igualmente, porque
    # callarlo haria que "0 nodos con salida rota" se leyera como "toda la
    # flota comprobada", que no es lo mismo.
    ciegos = [n for n, v in medidas_por_nodo.items() if len(v) < 2]
    sin_medir = [n for n in nodos if n not in medidas_por_nodo]
    if ciegos or sin_medir:
        log(f"alcance: {len(ciegos)} nodo(s) con 1 sola VM medible (alertan por "
            f"`mtu`, no por `cortado`) y {len(sin_medir)} sin ninguna VM medida")
    pct = 100.0 * len(culpables) / max(nodos_medidos, 1)
    now = datetime.now(timezone.utc)
    coll = db.collection("egress_distress")

    def presenta(doc_id: str, payload: dict):
        if dry:
            log(f"DRY-RUN presentaria: {doc_id} {json.dumps(payload)[:200]}")
            return
        ref = coll.document(doc_id)
        snap = ref.get()
        if snap.exists:
            d = snap.to_dict() or {}
            created, alerted = d.get("createdAt"), d.get("alertedAt")
            fresco = (not d.get("resolvedAt") and created
                      and now - created < timedelta(hours=DEDUPE_OPEN_H))
            avisado = alerted and now - alerted < timedelta(hours=DEDUPE_ALERT_H)
            if fresco or avisado:
                ref.update({"lastSeenAt": firestore.SERVER_TIMESTAMP})
                log(f"ya en curso: {doc_id}")
                return
            ref.delete()  # recrear para re-disparar el trigger
        ref.set(dict(payload,
                     state="open", reportedBy=hostname, region=region,
                     createdAt=firestore.SERVER_TIMESTAMP,
                     lastSeenAt=firestore.SERVER_TIMESTAMP, resolvedAt=None))
        log(f"presentado: {doc_id}")

    if culpables and pct > GLOBAL_PCT:
        # Averia ancha: un solo doc. Presentar 227 seria enterrar la señal.
        presenta(f"REGION-{region}", {
            "kind": "egress_region",
            "nodosAfectados": sorted(culpables),
            "nodosMedidos": nodos_medidos,
            "pct": round(pct, 1),
            "muestra": culpables[sorted(culpables)[0]]["vms"][:2],
        })
    else:
        for nid, c in sorted(culpables.items()):
            presenta(nid, {
                "kind": "egress_node", "nodeId": nid,
                "vmsFallando": c["vms"],
                "vmsMedidas": len(medidas_por_nodo.get(nid, [])),
                # Para que el correo pueda decir POR QUE se fia: `mtu` señala
                # a nuestro lado sin ambiguedad; `varias_vms` es inferencia.
                "regla": c["regla"], "certeza": c["certeza"],
            })

    # --- cerrar lo que ya se recupero ---------------------------------------
    resueltos = 0
    if not dry:
        try:
            from google.cloud.firestore_v1 import FieldFilter
            abiertos = coll.where(filter=FieldFilter("resolvedAt", "==", None))
        except ImportError:
            abiertos = coll.where("resolvedAt", "==", None)
        for snap in abiertos.stream():
            d = snap.to_dict() or {}
            if str(d.get("region") or "") != region:
                continue  # el doc es de la otra base; no tengo datos frescos
            nid = d.get("nodeId") or snap.id
            if nid in culpables or (d.get("kind") == "egress_region" and culpables):
                continue
            # Solo cierro lo que ACABO de medir. Un nodo que no se muestreo
            # esta pasada no es un nodo recuperado, y cerrarlo seria mentir.
            if d.get("kind") != "egress_region" and nid not in medidas_por_nodo:
                continue
            snap.reference.update({
                "resolvedAt": firestore.SERVER_TIMESTAMP,
                "resolvedBy": hostname, "resolution": "recovered",
            })
            resueltos += 1
            log(f"resuelto: {snap.id}")

    log(f"fin: {nodos_medidos}/{len(nodos)} nodos medidos, {len(culpables)} con "
        f"salida rota, {resueltos} resueltos, cobertura {cobertura:.0f}%, "
        f"{len(sin_medir)} nodo(s) sin medir"
        f"{' [DRY-RUN]' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
