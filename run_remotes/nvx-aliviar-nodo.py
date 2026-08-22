#!/usr/bin/env python3
"""Saca VMs de un nodo cuyo POOL se esta llenando, hasta bajarlo al objetivo.

SE EJECUTA A MANO. No hay timer y no se programa solo: decision del operador
(21-08-2026). El aviso de /admin/salud salta al 75% y a partir de ahi se decide
caso por caso, que es justo lo que esta herramienta permite hacer sin tener que
escribir un script nuevo para cada nodo — que es como se hizo el del 0000199 y
no se puede repetir cada vez.

POR QUE IMPORTA EL DISCO
El defrag reparte por RAM y cores. El disco solo lo mira para NO llenar un
destino, asi que nada saca VMs de un nodo que se llena: se queda donde esta o
sube. Y llenarse no es cosmetico — medido en el 0000199 el 21-08 a 82%: 71% de
fragmentacion frente al 30% de la flota, presion de E/S 3,11 frente a 0,10-0,25
y latencia de lectura 313 us frente a 104-209.

EL USO VENDIDO NO SIRVE
La flota es thin-provisioned (112% de media). La vm1002 tiene 45 GB vendidos y
usa 28,6 reales. Mover por `diskGb` movería VMs que no liberan casi nada, asi
que aqui se mide SIEMPRE el uso real de los zvols en el propio nodo.

MODO POR DEFECTO: PLAN. Mide, enseña exactamente que movería y sale sin tocar
nada. Hay que pedir `--ejecutar` a proposito.

  nvx-aliviar-nodo.py 0000048-AX102
  nvx-aliviar-nodo.py 0000048-AX102 --objetivo 70 --ejecutar

SALVAGUARDAS
  · No hace NADA si el defrag o un lote de migracion esta corriendo:
    competirian por los mismos destinos.
  · Solo saca del nodo que se le nombra. Nunca de otro.
  · El destino tiene que ser del MISMO modelo, pasar el gate de disco del 80%
    (el mismo que la venta y el defrag) y tener holgura de cores.
  · Se mueve de UNA EN UNA, remidiendo el pool despues de cada una. Un fallo
    para la tanda en vez de arrastrarla.
"""
import argparse
import json
import subprocess
import sys
import time

DISK_REAL_MAX_PCT = 80.0     # mismo umbral que auto_provision y el defrag
NODES_FILE = "/var/lib/base-nat/pve_nodes.json"
CRED = "/etc/firebase-credentials.json"
LOG = "/var/log/nvx-aliviar-nodo.log"


def log(msg):
    linea = "%s  %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(linea, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def firestore():
    """Cliente propio, SIN depender de ningun ayudante suelto en /root: ese
    directorio se ordena de vez en cuando y lo de produccion no puede caerse
    con ello."""
    from google.cloud import firestore as _fs
    from google.oauth2 import service_account
    c = service_account.Credentials.from_service_account_file(CRED)
    return _fs.Client(credentials=c, project=c.project_id)


def hay_defrag():
    r = subprocess.run(["pgrep", "-f", "neuravps-defrag.py|migrate_vms_batch.sh"],
                       capture_output=True, text=True)
    return r.returncode == 0


def en_nodo(nid, cmd, timeout=90):
    ip = json.load(open(NODES_FILE)).get(nid)
    if not ip:
        return None
    try:
        r = subprocess.run(["ssh", "-n", "-o", "ConnectTimeout=10",
                            "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                            "root@" + ip, cmd],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip()


def pool(nid):
    """(pct, bytes_ocupados, bytes_totales). En bytes para no redondear el
    calculo de cuanto hay que sacar."""
    out = en_nodo(nid, "zpool list -Hp -o cap,alloc,size rpool 2>/dev/null")
    try:
        cap, alloc, size = out.split()
        return float(cap), int(alloc), int(size)
    except (ValueError, AttributeError):
        return None, None, None


def uso_real(nid):
    """vmid -> GB realmente ocupados por sus zvols. Lo que se transfiere y lo
    que se libera; el tamano vendido no dice nada con thin provisioning."""
    out = en_nodo(nid, "zfs list -Hp -o name,used -t volume 2>/dev/null")
    uso = {}
    for linea in (out or "").split("\n"):
        partes = linea.split()
        if len(partes) != 2 or "/vm-" not in partes[0]:
            continue
        try:
            vmid = int(partes[0].split("/vm-")[1].split("-disk")[0])
            uso[vmid] = uso.get(vmid, 0.0) + int(partes[1]) / (1024.0 ** 3)
        except (ValueError, IndexError):
            continue
    return uso


def elige_destino(nid_origen, vmid, db, reservado):
    """Mismo criterio que el defrag: mismo modelo, gate de disco, holgura de
    cores. Se prefiere el pool MAS VACIO — el objetivo es repartir presion, no
    compactar. `reservado` descuenta lo ya prometido a destinos en esta tanda,
    porque zpoolCapPct solo se refresca una vez al dia (06:00 UTC) y sin eso
    las seis migraciones irian todas al mismo sitio creyendolo vacio."""
    from google.cloud.firestore_v1 import FieldFilter
    modelo = "-AX102" if "-AX102" in nid_origen else "-AX162"
    cores_vm = 2
    for d in db.collection("servers").where(
            filter=FieldFilter("proxmoxId", "==", vmid)).limit(1).stream():
        cores_vm = int((d.to_dict() or {}).get("cores") or 2)
    usados = {}
    for d in db.collection("servers").select(["nodeId", "cores"]).stream():
        s = d.to_dict() or {}
        if s.get("nodeId"):
            usados[s["nodeId"]] = usados.get(s["nodeId"], 0) + int(s.get("cores") or 2)
    mejor = None
    for nd in db.collection("proxmox_nodes").stream():
        n = nd.to_dict() or {}
        if modelo not in nd.id or nd.id == nid_origen:
            continue
        if n.get("frozen") or str(n.get("status") or "").strip():
            continue
        pct = n.get("zpoolCapPct")
        if pct is None:
            continue
        pct = float(pct) + reservado.get(nd.id, 0.0)
        if pct >= DISK_REAL_MAX_PCT:
            continue
        if int(n.get("max_cores") or 0) - usados.get(nd.id, 0) - cores_vm < 0:
            continue
        if mejor is None or pct < mejor[1]:
            mejor = (nd.id, pct, float(n.get("zpoolMainPoolSizeGb") or 0))
    return mejor


def main():
    p = argparse.ArgumentParser(description="Saca VMs de un nodo lleno hasta el objetivo.")
    p.add_argument("nodo")
    p.add_argument("--objetivo", type=float, default=70.0,
                   help="ocupacion a la que se quiere dejar el pool (por defecto 70)")
    p.add_argument("--max", type=int, default=10, help="tope de VMs a mover")
    p.add_argument("--ejecutar", action="store_true",
                   help="mueve de verdad; sin esto solo enseña el plan")
    a = p.parse_args()

    if a.nodo not in json.load(open(NODES_FILE)):
        log("no conozco el nodo %s" % a.nodo)
        return 2
    if a.ejecutar and hay_defrag():
        log("defrag o lote de migracion en curso — no toco nada")
        return 1

    pct, alloc, size = pool(a.nodo)
    if pct is None:
        log("no puedo leer el pool de %s" % a.nodo)
        return 2
    gb = 1024.0 ** 3
    log("%s al %.0f%% (%.0f/%.0f GB), objetivo %.0f%%" %
        (a.nodo, pct, alloc / gb, size / gb, a.objetivo))
    if pct <= a.objetivo:
        log("ya esta por debajo del objetivo — nada que hacer")
        return 0

    sacar = (alloc - size * a.objetivo / 100.0) / gb
    uso = uso_real(a.nodo)
    if not uso:
        log("no puedo leer el uso real de los zvols — abandono")
        return 2
    # De mayor a menor uso REAL: asi se molesta a los MENOS clientes posibles
    # para liberar los mismos GB.
    cola = sorted(uso.items(), key=lambda x: -x[1])
    plan, acum = [], 0.0
    for vmid, g in cola:
        if acum >= sacar or len(plan) >= a.max:
            break
        plan.append((vmid, g))
        acum += g
    log("hay que sacar %.0f GB; plan de %d VM(s) que liberan %.0f GB:" %
        (sacar, len(plan), acum))
    for vmid, g in plan:
        log("   vm%-6d %6.1f GB" % (vmid, g))
    if acum < sacar:
        log("AVISO: con el tope de %d VMs no se llega al objetivo (faltan %.0f GB)"
            % (a.max, sacar - acum))

    if not a.ejecutar:
        log("modo PLAN — no se ha tocado nada. Repite con --ejecutar para moverlas.")
        return 0

    db = firestore()
    reservado = {}
    for vmid, g in plan:
        dest = elige_destino(a.nodo, vmid, db, reservado)
        if not dest:
            log("vm%d: ningun destino pasa el gate y tiene cores — paro aqui" % vmid)
            return 1
        # migrate_vm.sh quiere el NUMERO del nodo, no su id: pasarle
        # "0000198-AX102-1" entero lo rechaza con "NEW_NODE_ID must be a
        # non-negative integer" (visto el 21-08 con la vm1139).
        num = str(int(dest[0].split("-")[0]))
        log("moviendo vm%d (%.0f GB) -> %s (n.%s, destino al %.0f%%)" %
            (vmid, g, dest[0], num, dest[1]))
        r = subprocess.run(["/root/migrate_vm.sh", str(vmid), num],
                           capture_output=True, text=True, timeout=7200)
        if r.returncode != 0:
            log("vm%d -> %s : FALLO rc=%d" % (vmid, dest[0], r.returncode))
            log("  ultimas lineas: %s" %
                " | ".join((r.stdout + r.stderr).strip().split("\n")[-3:])[:300])
            return 1
        log("vm%d -> %s : OK" % (vmid, dest[0]))
        if dest[2]:
            reservado[dest[0]] = reservado.get(dest[0], 0.0) + 100.0 * g / dest[2]
        pct, alloc, size = pool(a.nodo)
        if pct is None:
            log("no puedo remedir el pool — paro por prudencia")
            return 1
        log("%s ahora al %.0f%%" % (a.nodo, pct))
        if pct <= a.objetivo:
            log("OBJETIVO ALCANZADO")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
