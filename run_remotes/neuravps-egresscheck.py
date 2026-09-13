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

IP DE SALIDA (pools de IPv4 por VM, 2026-09)
Con `config/egressPools.enabled`, cada VM tiene un PAR de IPv4 de salida
(`servers.egressIpv4`) y las bases la sacan por la de su region. La misma pasada
pregunta tambien a Cloudflare con que IPv4 LLEGA el invitado
(`/cdn-cgi/trace`, una peticion mas, ~0,1 s) y la compara con su par:
  propia   la de su region                     -> bien
  del_par  la otra de su par                   -> bien (failover o bloque moviendose)
  general  la IP principal de una base         -> alerta si los dos bloques estan
                                                  concedidos (el mapa no la lleva)
  ajena    ninguna de las anteriores           -> alerta siempre
Las VMs canario se sondean SIEMPRE, ademas del muestreo. Se presenta UN doc por
region (`EGRESSIP-<region>`, kind `egress_ip`). `config/egresscheck.verificarIpSalida`
= false lo apaga.

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
# Por debajo de esto, los 32 KB demuestran que el camino esta sano y un 1 KB
# lento se descarta como ruido de establecimiento de conexion. 1 s es holgado:
# las medidas buenas de la flota van en centesimas (0,06-0,25 s), asi que aqui
# solo cabe algo que de verdad volo.
GRANDE_SANO_S = 1.0
# Por encima de esta CPU en el invitado, un veredicto `lento` NO se cree: no se
# puede separar una red lenta de un `curl` que no llega a ejecutarse a tiempo.
# 90% y no 100% porque un invitado exprimido oscila. `mtu` SIGUE valiendo por
# encima de este umbral: ninguna carga de CPU puede hacer que 1 KB pase y 32 KB
# no — esa firma sigue siendo infalsificable.
CPU_INVITADO_SATURADO = 90
# Se lee de `config/egresscheck.umbralLentoS` si esta puesto. No es un capricho:
# el backoff de retransmision de TCP va en pasos de ~1 s, 3 s y 7 s, asi que un
# umbral de 3 s puede pillar DOS paquetes perdidos —ruido normal de red— ademas
# del camino roto. Lo sano medido va de 0,02 a 0,28 s y lo roto de 6,45 a 6,55,
# asi que hay sitio de sobra para subirlo; pero subirlo A OJO seria cambiar un
# numero por otro sin evidencia. Por eso primero se GUARDA LA MEDIDA (ver
# `analiza`) y se decide con ella.
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

# --- ANTI-RUIDO de 'lento' (medido el 13-09-2026) -----------------------
# Tres pasadas de b1 seguidas (09:47/10:47/12:47 UTC) marcaron "certeza alta"
# con nodos DISTINTOS cada vez a partir de UNA sola descarga de 32 KB por UNA
# sola pata (v4 o v6): 2-4 s, mientras el 1 KB de la misma pata y la otra pata
# iban finas. Los tres se resolvieron solos en la pasada siguiente y b1 tenia
# el uplink al 0,16% de uso — no era congestion, era ruido de establecimiento
# de conexion (un SYN perdido cuesta 1-2 s de reintento TCP, y eso solo ya
# roza el umbral). El caso REAL del clamp perdido, en cambio, entregaba 6,45-
# 6,55 s de forma REPETIDA: no hace falta perder sensibilidad para dejar de
# fiarse de UNA medida.
#
# Dos filtros, uno detras de otro, y los dos son necesarios por separado:
#
#  1. REINTENTO EN LA MISMA PASADA. Un 'lento' de la ronda 1 no se acepta a la
#     primera: se remide la MISMA VM dos veces mas, unos segundos aparte (no
#     los REPROBE_DELAY_S=45s de la ronda 2 general, que busca otra cosa: un
#     reinicio del invitado o un pico que ya paso). Se necesita MAYORIA
#     (2 de 3) para que siga contando como 'lento'. Un 'mtu' en cualquier
#     reintento manda: es la firma infalsificable y no necesita mayoria.
#
#  2. CONFIRMACION ENTRE PASADAS para una sola VM. Con >=2 VMs del mismo nodo
#     lentas en la MISMA pasada ya se corroboran solas (bastante improbable
#     que dos VMs distintas compartan el mismo SYN perdido) y se alerta ya.
#     Con una sola VM, el 22-08 y el 13-09 demostraron que hasta un `curl`
#     tres veces seguidas puede pillar el mismo hipo de unos segundos si el
#     hipo dura mas que el hueco entre reintentos. Asi que no se alerta a la
#     primera: se anota en `egress_lento_watch` (coleccion aparte, NO dispara
#     el trigger de correo) y solo se convierte en aviso si el MISMO nodo
#     vuelve a salir 'lento' de una sola VM en la pasada siguiente. Una averia
#     de verdad (clamp perdido, tunel caido) sigue lenta hora tras hora; un
#     hipo de red no.
#
# `mtu` y `cortado` (>=2 VMs) NO pasan por ninguno de los dos filtros: son las
# firmas que YA exigian evidencia fuerte (infalsificable, o >=2 VMs) y deben
# seguir alertando a la primera, tal como pide el diseño.
REINTENTOS_LENTO = 2                # medidas extra tras la primera, no la sustituyen
REINTENTOS_LENTO_DELAY_S = 6        # "unos segundos", no los 45s de la ronda 2
LENTO_MAYORIA = 2                   # de 3 medidas (1+REINTENTOS_LENTO), hacen falta 2
LENTO_WATCH_COLL = "egress_lento_watch"
# Cada base pasa una vez por hora; 3h cubre una pasada perdida sin arrastrar
# para siempre un hipo de hace una semana.
LENTO_WATCH_TTL_H = 3.0


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
URL_TRACE = "https://speed.cloudflare.com/cdn-cgi/trace"
# Las IPs principales de las bases: si un invitado con pool activo llega con una
# de estas, el mapa de la base no lo lleva. Sobrescribible sin desplegar codigo.
BASES_IPV4 = {x.strip() for x in os.environ.get(
    "EGRESS_BASE_IPV4S", "116.202.118.221,95.216.102.179").split(",") if x.strip()}
REGION_POOL = {"helsinki": "hel", "falkenstein": "fsn"}
_PS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ag=curl.exe -4 -s -o NUL --max-time 25 " + _W + " '" + URL + "';$rag=$LASTEXITCODE;"
    "$ap=curl.exe -4 -s -o NUL --max-time 15 " + _W + " '" + URL_MIN + "';$rap=$LASTEXITCODE;"
    "$bg=curl.exe -6 -s -o NUL --max-time 25 " + _W + " '" + URL + "';$rbg=$LASTEXITCODE;"
    "$bp=curl.exe -6 -s -o NUL --max-time 15 " + _W + " '" + URL_MIN + "';$rbp=$LASTEXITCODE;"
    # Con que IPv4 llega el invitado a Internet (pools de salida por VM).
    "$tr=(curl.exe -4 -s --max-time 10 '" + URL_TRACE + "') -join ' ';"
    "$ip4=([regex]::Match([string]$tr,'(?:^|\\s)ip=([0-9.]+)')).Groups[1].Value;"
    "if(-not $ip4){$ip4='NA'};"
    "$d=try{(Resolve-DnsName " + NOMBRE_DNS + " -Type A -EA Stop|"
    "Select-Object -First 1).IPAddress}catch{'FALLO'};"
    # ⚠️ LA CPU DEL INVITADO ES PARTE DE LA MEDIDA, no un extra.
    # Este curl corre DENTRO del invitado, asi que un invitado con la CPU al
    # tope hace que se planifique tarde y el reloj marque segundos que NO son
    # de red. Paso el 2026-08-21 con la vm587: alerta de 'salida rota' cuando
    # lo que habia era StrategyQuant llevando 2.303 HORAS de CPU al 100% — o
    # sea, el cliente usando el servidor exactamente para lo que lo compro.
    # Sin este dato, la sonda no puede distinguir una red lenta de un invitado
    # ocupado, y confundirlas convierte a cada cliente que exprime su VPS en
    # una falsa alarma.
    "$cpu=try{[int](Get-CimInstance Win32_Processor -EA Stop|"
    "Measure-Object LoadPercentage -Average).Average}catch{-1};"
    "'v4g='+$ag+'/'+$rag+' v4p='+$ap+'/'+$rap+"
    "' v6g='+$bg+'/'+$rbg+' v6p='+$bp+'/'+$rbp+' dns='+$d+' cpu='+$cpu+' ip4='+$ip4"
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
        # Un 1 KB no tarda segundos por un camino sano: si ESE va lento suele
        # ser retransmision y no ancho de banda, y por eso cuenta.
        #
        # PERO solo cuenta si el grande no lo desmiente. El 22-08-2026 el
        # 0000045 alerto con v6p=3.026s y v6g=0.123s: 32 KB por el MISMO camino
        # y en la MISMA pasada, 32 veces mas paquetes, 25 veces mas rapido. Si
        # el camino retransmitiera, el grande sufriria mas que el pequeño, no
        # menos. Esos 3,026 s son un SYN perdido (1 s + 2 s de reintento TCP da
        # casi el numero exacto), o sea establecimiento de conexion, no red
        # rota. Re-sondeado seis veces: todas limpias, la peor 0,73 s.
        #
        # Un grande RAPIDO es prueba positiva de que el camino esta sano, y una
        # prueba positiva gana a un unico dato malo. No se toca el caso del
        # grande lento: ese sigue alertando solo, que es la firma del clamp.
        if tg > UMBRAL_LENTO:
            return "lento"
        if tp > UMBRAL_LENTO and tg >= GRANDE_SANO_S:
            return "lento"
        return "ok"
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
    try:
        cpu = int(partes.get("cpu", "-1"))
    except (TypeError, ValueError):
        cpu = -1
    ok = (v4 == "ok" and v6 == "ok" and dns == "ok")
    # ⚠️ LA MEDIDA VIAJA CON EL VEREDICTO. Sin esto el aviso dice "lento" y no
    # cuanto, que es justo el dato que lo justifica y el unico con el que se
    # puede calibrar el umbral. Paso el 2026-08-20 con la vm773: salto la
    # alerta, se resolvio sola en una hora, y no habia forma de saber si habian
    # sido 3,1 segundos o 25. Son ~90 caracteres; guardarlos no cuesta nada.
    # `mtu` y `lento` mandan sobre `cortado`: son las firmas que señalan
    # inequivocamente a nuestro lado, y una sola VM con ellas ya alerta.
    if "mtu" in (v4, v6):
        kind = "mtu"
    elif "lento" in (v4, v6):
        # Un invitado saturado explica la lentitud sin que la red tenga nada
        # que ver. No se puede afirmar ni descartar: se marca `no_concluyente`
        # y NO cuenta como fallo. Se guarda la CPU para que el aviso lo diga.
        kind = "no_concluyente" if cpu >= CPU_INVITADO_SATURADO else "lento"
        if kind == "no_concluyente":
            ok = True
    else:
        kind = "cortado" if not ok else "ok"
    return ok, {"v4": v4, "v6": v6, "dns": dns, "kind": kind,
                "medida": salida, "cpuInvitado": cpu}


def _confirma_lento(node_ip: str, vmid: int, primer_det: dict, *,
                     probe_fn=por_agente, sleep_fn=time.sleep):
    """(ok, detalle) tras exigir MAYORIA a un veredicto 'lento' de una sola
    medida (ver ANTI-RUIDO arriba). No sustituye la firma 'mtu': si aparece en
    cualquier reintento, gana al momento porque un cliente no puede fabricarla
    y no necesita mayoria."""
    veredictos = [primer_det]
    for _ in range(REINTENTOS_LENTO):
        sleep_fn(REINTENTOS_LENTO_DELAY_S)
        ok_canal, salida = probe_fn(node_ip, vmid)
        if not ok_canal:
            continue  # esta vuelta no cuenta ni a favor ni en contra
        _ok, det = analiza(salida)
        if det.get("kind") == "mtu":
            return False, det
        veredictos.append(det)
    lentos = sum(1 for d in veredictos if d.get("kind") == "lento")
    if lentos >= LENTO_MAYORIA:
        return False, veredictos[-1]
    return True, primer_det  # no confirmado: un hipo de un momento, se descarta


def clasifica_culpables(fallos_por_nodo: dict, medidas_por_nodo: dict):
    """(culpables, lento_pendiente). Pura: no toca Firestore ni el reloj.

    TRES reglas, y la primera es la que salva a los 115 nodos de una sola VM:
      `mtu`     -> el pequeño pasa y el grande no. Un cliente no puede
                   fabricar eso. Basta UNA VM, a la primera.
      `lento`   -> ya paso por la mayoria de 3 en `_confirma_lento`. Con
                   >=2 VMs del mismo nodo se corroboran solas y alerta ya;
                   con UNA sola VM no basta (ver ANTI-RUIDO): vuelve en
                   `lento_pendiente`, y quien llama decide si ya se vio antes
                   en `egress_lento_watch` (confirmacion entre pasadas).
      `cortado` -> ambiguo. Se exigen >=2 VMs fallando del mismo nodo, con
                   >=2 medidas, para no alertar por el cliente que se ha
                   cerrado el firewall.
    """
    culpables = {}
    lento_pendiente = {}
    for nid, v in fallos_por_nodo.items():
        mtu_vms = [x for x in v if (x.get("detalle") or {}).get("kind") == "mtu"]
        lento_vms = [x for x in v if (x.get("detalle") or {}).get("kind") == "lento"]
        if mtu_vms:
            culpables[nid] = {"vms": v, "regla": "mtu", "certeza": "alta"}
        elif len(lento_vms) >= 2:
            culpables[nid] = {"vms": v, "regla": "lento", "certeza": "alta"}
        elif len(lento_vms) == 1:
            lento_pendiente[nid] = v
        elif len(v) >= 2 and len(medidas_por_nodo.get(nid, [])) >= 2:
            culpables[nid] = {"vms": v, "regla": "varias_vms", "certeza": "media"}
    return culpables, lento_pendiente


def lento_confirmado(watch_doc: dict | None, now, ttl_horas: float = LENTO_WATCH_TTL_H) -> bool:
    """True si un 'lento' de una sola VM ya se habia visto en la pasada
    anterior (documento leido de `egress_lento_watch`) y sigue dentro del
    plazo de confirmacion. Pura: decide solo con lo que ya se leyo."""
    return bool(watch_doc and watch_doc.get("createdAt")
                and now - watch_doc["createdAt"] < timedelta(hours=ttl_horas))


def ip_vista(salida: str) -> str | None:
    """La IPv4 con la que Cloudflare vio llegar al invitado, o None."""
    for p in (salida or "").split():
        if p.startswith("ip4="):
            v = p[4:]
            partes = v.split(".")
            if len(partes) == 4 and all(x.isdigit() and int(x) < 256 for x in partes):
                return v
    return None


def pools_activos(raw) -> dict | None:
    """Lo minimo de `config/egressPools` que necesita la sonda, o None si los
    pools no estan encendidos (entonces no hay nada que verificar)."""
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    pools = raw.get("pools") if isinstance(raw.get("pools"), dict) else {}
    try:
        canary = {int(v) for v in (raw.get("canaryVmids") or [])}
    except (TypeError, ValueError):
        return None
    return {
        "fleet": raw.get("fleetWide") is True,
        "canary": canary,
        "concedidos": all((pools.get(r) or {}).get("activeServerIp") for r in ("hel", "fsn")),
    }


def veredicto_ip(vista, par: dict, region: str, pools: dict) -> str:
    """propia | del_par | general | ajena | sin_dato  (ver cabecera)."""
    if not vista:
        return "sin_dato"
    propia = par.get(REGION_POOL.get(region, ""))
    otra = par.get("fsn" if REGION_POOL.get(region) == "hel" else "hel")
    if vista == propia:
        return "propia"
    if vista == otra:
        return "del_par"
    if vista in BASES_IPV4:
        return "general"
    return "ajena"


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
    global UMBRAL_LENTO
    UMBRAL_LENTO = float(cfg.get("umbralLentoS") or UMBRAL_LENTO)

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
    pools = None
    if cfg.get("verificarIpSalida", True) is not False:
        snap_pools = db.collection("config").document("egressPools").get()
        pools = pools_activos(snap_pools.to_dict() if snap_pools.exists else None)
    pares = {}
    por_nodo_cands = {}
    for snap in q.select(["proxmoxId", "nodeId", "maintenance",
                          "provisioningStatus", "reinstalling", "egressIpv4"]).stream():
        d = snap.to_dict() or {}
        nid = d.get("nodeId")
        if nid not in nodos or d.get("maintenance") or d.get("reinstalling") is True:
            continue
        prov = d.get("provisioningStatus")
        if prov is not None and prov != "provisioned":
            continue
        try:
            vmid_c = int(d.get("proxmoxId"))
        except (TypeError, ValueError):
            continue
        por_nodo_cands.setdefault(nid, []).append(vmid_c)
        par = d.get("egressIpv4")
        if pools and isinstance(par, dict) and par.get("hel") and par.get("fsn") \
                and (pools["fleet"] or vmid_c in pools["canary"]):
            pares[vmid_c] = {"hel": par["hel"], "fsn": par["fsn"]}

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
        # Los canarios de los pools se miran SIEMPRE: son pocos y son justo
        # los que dicen si el cambio de IP de salida funciona.
        if pools and not pools["fleet"]:
            elegidas += [v for v in vms if v in pools["canary"] and v not in elegidas]
        for v in elegidas:
            trabajos.append((nid, v))

    log(f"{region}: {len(nodos)} nodos, {sum(len(v) for v in por_nodo_cands.values())} "
        f"VMs candidatas, {len(trabajos)} sondas (hasta {por_nodo}/nodo, giro h={hora})")

    ips_vistas = {}

    def sondear(job):
        nid, vmid = job
        ok_canal, salida = por_agente(nodos[nid], vmid)
        if not ok_canal:
            return (nid, vmid, None, salida)      # no medible
        ips_vistas[vmid] = ip_vista(salida)
        ok, det = analiza(salida)
        if det.get("kind") == "lento":
            # No se acepta un 'lento' a la primera medida (ver ANTI-RUIDO):
            # se remide la MISMA VM un par de veces mas, unos segundos aparte.
            ok, det = _confirma_lento(nodos[nid], vmid, det)
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

    culpables, lento_pendiente = clasifica_culpables(fallos_por_nodo, medidas_por_nodo)

    # --- CONFIRMACION ENTRE PASADAS de un 'lento' de una sola VM ------------
    # Ver ANTI-RUIDO: ni la mayoria de 3 dentro de la pasada basta si el hipo
    # dura mas que el hueco entre reintentos (pasaba el 13-09 y sobrevivia
    # incluso a la ronda 2 de 45s). Se exige que el MISMO nodo reaparezca en
    # la pasada siguiente antes de convertirlo en aviso; mientras tanto se
    # anota en una coleccion aparte que NO dispara el trigger de correo.
    now = datetime.now(timezone.utc)
    watch = db.collection(LENTO_WATCH_COLL)
    for nid, v in lento_pendiente.items():
        if dry:
            log(f"DRY-RUN 'lento' de 1 VM en {nid}: quedaria pendiente de "
                "confirmar en la pasada siguiente")
            continue
        wref = watch.document(nid)
        wsnap = wref.get()
        wd = wsnap.to_dict() if wsnap.exists else None
        if lento_confirmado(wd, now):
            culpables[nid] = {"vms": v, "regla": "lento", "certeza": "alta"}
            wref.delete()
            log(f"'lento' confirmado entre pasadas: {nid}")
        else:
            wref.set({"createdAt": firestore.SERVER_TIMESTAMP, "region": region,
                      "vms": v[:5]})
            log(f"'lento' de 1 VM en {nid}: primera vez, pendiente de "
                "confirmar en la pasada siguiente")
    if not dry:
        # Limpiar candidatos que esta pasada ya salen limpios: si no, un hipo
        # de hace semanas confirmaria uno nuevo sin relacion. Basta con
        # `watch.stream()` sin filtrar por region: el id es el nodeId, y un
        # nodo de la otra region nunca aparece en `medidas_por_nodo` de esta
        # base, asi que nunca se borra por error.
        for snap in watch.stream():
            nid = snap.id
            if nid in lento_pendiente:
                continue  # sigue acumulando evidencia, no tocar
            if nid in medidas_por_nodo:
                # Ya confirmado (y borrado arriba), o culpable por otra via
                # (mtu/varias VMs), o simplemente limpio: en los tres casos un
                # watch de un 'lento' viejo ya no debe sobrevivir, para que no
                # confirme sin querer un hipo nuevo sin relacion con el de antes.
                snap.reference.delete()
                log(f"'lento' watch limpiado: {nid}")

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

    # --- IP de salida: pools por VM ----------------------------------------
    ip_fuera = []
    ip_medidas = 0
    if pools:
        cuenta = {"propia": 0, "del_par": 0, "general": 0, "ajena": 0, "sin_dato": 0}
        nodo_de = {v: n for n, v in trabajos}
        for vmid, par in pares.items():
            if vmid not in ips_vistas:
                continue
            v = veredicto_ip(ips_vistas[vmid], par, region, pools)
            cuenta[v] += 1
            if v == "ajena" or (v == "general" and pools["concedidos"]):
                ip_fuera.append({"vmid": vmid, "nodeId": nodo_de.get(vmid), "vista": ips_vistas[vmid],
                                 "veredicto": v, "hel": par["hel"], "fsn": par["fsn"]})
        ip_medidas = sum(cuenta[k] for k in ("propia", "del_par", "general", "ajena"))
        log(f"ip de salida: {cuenta} (con pool activo y medidas: {ip_medidas})")
        if ip_fuera:
            presenta(f"EGRESSIP-{region}", {
                "kind": "egress_ip", "vmsFuera": ip_fuera[:30], "vmsConPool": ip_medidas,
                "correctas": cuenta["propia"], "delPar": cuenta["del_par"],
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
            if d.get("kind") == "egress_ip":
                # Solo lo cierra una pasada que SI midio VMs con pool y no vio
                # ninguna fuera de su par.
                if ip_fuera or not ip_medidas:
                    continue
                snap.reference.update({
                    "resolvedAt": firestore.SERVER_TIMESTAMP,
                    "resolvedBy": hostname, "resolution": "recovered",
                })
                resueltos += 1
                log(f"resuelto: {snap.id}")
                continue
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
