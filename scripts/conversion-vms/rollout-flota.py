#!/usr/bin/env python3
"""Conversion de la flota al modelo nuevo, AGRUPADA POR USUARIO y en paralelo.
Corre en el SANDBOX.

Cada worker se lleva un usuario entero: convierte todas sus VMs encendidas y al
terminar dispara UN resync de enlaces SMB. Agrupar por usuario no es cosmetico
— el resync reconstruye los enlaces leyendo Firestore, asi que hacerlo al final
del usuario es lo unico que evita que el enlace hacia su ultima VM se quede
obsoleto en las demas.

Guardas:
  - Solo por el AGENTE. Si no ejecuta -> MANUAL, la VM NO se toca (por SSH se
    cortaria la sesion al cambiar la IP).
  - Nunca se reinicia nada.
  - Si una VM que respondia por las VIPs no vuelve -> ABORTA TODO el despliegue.
  - Reanudable: recalcula la lista pendiente en cada arranque.

Uso: rollout_flota.py [--workers N] [--usuarios N]
"""
import json
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

VIPS = ["94.130.3.118", "77.42.49.79"]
DIR = "/tmp/claude-1001/-home-neuravps-NeuraVPS/b087c788-bc91-4080-b3f8-f1b7348049c7/scratchpad/v2"
LOG = f"{DIR}/rollout_flota.log"

abortar = threading.Event()
lock = threading.Lock()
estado = {"ok": 0, "manual": [], "usuarios": 0}


def log(m):
    with lock:
        linea = f"{time.strftime('%H:%M:%S')} {m}"
        print(linea, flush=True)
        with open(LOG, "a") as f:
            f.write(linea + "\n")


def b1(cmd, t=300):
    try:
        r = subprocess.run(["ssh", "-n", "b1", cmd], capture_output=True, text=True, timeout=t)
        return "\n".join(l for l in (r.stdout + r.stderr).splitlines()
                         if "UserWarning" not in l and "return query" not in l).strip()
    except subprocess.TimeoutExpired:
        return ""


def abierto(ip, p):
    try:
        with socket.create_connection((ip, p), timeout=4):
            return True
    except Exception:
        return False


def rdp_ok(v):
    return all(abierto(ip, 20000 + v) for ip in VIPS)


def rdp_alguno(v):
    return any(abierto(ip, 20000 + v) for ip in VIPS)



def bound_correcto(v, salida):
    """¿El invitado quedó como debe? Compara el BOUND contra lo ESPERADO.

    Antes bastaba con que la VM siguiera respondiendo por las VIPs, y eso dejó
    pasar dos conversiones a medias (vm 460 y 1754): la VM respondía todavía por
    el DNAT VIEJO, que aún no se había movido, mientras por dentro tenía la IPv6
    nueva con /64 y la IPv4 y las puertas viejas. Al mover el DNAT quedaron
    incomunicadas. Comprobar el estado real es lo único que lo caza.
    """
    linea = next((l for l in salida.splitlines() if "BOUND:" in l), "")
    c = dict(kv.split("=", 1) for kv in linea.split() if "=" in kv)
    fallos = []
    esperado6 = f"2a01:4f9:c01f:e::{v:x}/128"
    esperado4 = f"10.64.{v // 256}.{v % 256}"
    if esperado6 not in (c.get("v6") or ""):
        fallos.append(f"v6={c.get('v6')} (esperaba {esperado6})")
    if esperado4 not in (c.get("v4") or ""):
        fallos.append(f"v4={c.get('v4')} (esperaba {esperado4})")
    if "fe80::1" not in (c.get("gw6") or ""):
        fallos.append(f"gw6={c.get('gw6')}")
    if "10.64.255.1" not in (c.get("gw4") or ""):
        fallos.append(f"gw4={c.get('gw4')}")
    return fallos


def haz_usuario(item):
    uid, vms = item
    if abortar.is_set():
        return
    convertida = False
    for v in vms:
        if abortar.is_set():
            return
        if not rdp_alguno(v):
            log(f"  vm {v}: no responde por ninguna VIP ANTES — la salto")
            continue
        out = b1(f"cd /root && python3 vmtool.py convert {v}")
        if "BOUND:" not in out:
            with lock:
                estado["manual"].append(v)
            log(f"  vm {v}: ⚠️ MANUAL ({(out.splitlines()[-1][:90] if out else 'sin salida')})")
            continue
        # ⚠️ Verificar ANTES de tocar Firestore: si el invitado quedó a medias y
        # movemos el DNAT, la VM se queda incomunicada.
        mal = bound_correcto(v, out)
        if mal:
            with lock:
                estado["manual"].append(v)
            log(f"  vm {v}: ⚠️ A MEDIAS, NO toco Firestore — {'; '.join(mal)}")
            continue
        b1(f"python3 /root/fs_set.py {v}")
        vuelve = False
        for _ in range(15):
            time.sleep(4)
            if rdp_ok(v):
                vuelve = True
                break
        if not vuelve:
            abortar.set()
            log(f"⛔⛔ vm {v}: NO vuelve por las VIPs tras convertir — ABORTO EL DESPLIEGUE")
            return
        convertida = True
        with lock:
            estado["ok"] += 1
        if estado["ok"] % 25 == 0:
            log(f"  ··· {estado['ok']} VMs convertidas")
    if convertida and not abortar.is_set():
        b1(f"python3 /root/resync_smb.py {vms[0]}")
    with lock:
        estado["usuarios"] += 1


workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 6
tope = int(sys.argv[sys.argv.index("--usuarios") + 1]) if "--usuarios" in sys.argv else None

lista = json.loads(b1("python3 /root/lista_pendientes.py", t=300).splitlines()[-1])
usuarios = sorted(lista.items(), key=lambda kv: (len(kv[1]), kv[0]))
if tope:
    usuarios = usuarios[:tope]

log(f"=== {sum(len(v) for _, v in usuarios)} VMs de {len(usuarios)} usuarios, {workers} en paralelo ===")
t0 = time.time()
with ThreadPoolExecutor(max_workers=workers) as ex:
    list(ex.map(haz_usuario, usuarios))

mins = (time.time() - t0) / 60
log(f"\n=== {'ABORTADO' if abortar.is_set() else 'FIN'}: {estado['ok']} VMs convertidas, "
    f"{estado['usuarios']} usuarios, {mins:.0f} min ===")
if estado["manual"]:
    log(f"=== {len(estado['manual'])} para aplicar A MANO (el agente no ejecuta) ===")
    log(f"    {sorted(estado['manual'])}")
