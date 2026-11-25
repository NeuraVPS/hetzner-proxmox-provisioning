#!/usr/bin/env python3
"""Prueba real de nvx-node64.py en espacios de nombres privados; sin E/S de produccion.

    unshare -rn python3 base/snippets/test-node64-netns.py

El espacio actual hace de NODO (uplink enp5s0, puente vmbr0 con su /64, un
tun-fsn hacia una "base"). Hijos: INTERNET (desconocido, host de base, salida
SNAT de un cliente por la base, host de otro nodo, invitado de otro nodo,
transito spoofeado), TUN (transito legitimo por el tunel), y dos INVITADOS en el
puente: A = vmid 520 (excluida, ipv6Enabled=true) y B = vmid 1409 (direccion
heredada del /64 del nodo). Conexiones TCP de verdad.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SCRIPT = str(HERE / "nvx-node64.py")
N64 = "2001:db8:64"
LEGACY = f"{N64}::581"        # VM1409: ipv6Enabled=false
EXCL = f"{N64}::208"          # vmid 520 excluido
WAN_NODE, WAN_GW = "2001:db8:ffff::2", "2001:db8:ffff::1"
STRANGER = "2001:db8:aaaa::7"
BASE_HOST, BASE_SNAT = "2001:db8:bbbb::2", "2001:db8:bbbb::5ed"   # base ::2 / cliente vmid 1517 via SNAT
OTHER_HOST, OTHER_GUEST = "2001:db8:cccc::2", "2001:db8:cccc::99"
TRANSIT = "2001:db8:eeee:ffff::3"
ID_OK, ID_RESV = "2a01:4f9:c01f:e::589", "2a01:4f9:c01f:e::2"   # IDENT normal / reservada (::2)


def sh(*cmd, data=None, check=True, env=None):
    p = subprocess.run(cmd, input=data, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise RuntimeError(f"{cmd}: {p.stderr} {p.stdout}")
    return p


def main():
    procs = []

    def ns():
        p = subprocess.Popen(["unshare", "-n", "sleep", "600"])
        procs.append(p)
        time.sleep(.15)
        return str(p.pid)

    inet, tun, ga, gb = ns(), ns(), ns(), ns()

    def n(pid, *cmd, **kw):
        return sh("nsenter", "-t", pid, "-n", *cmd, **kw)

    tmp = Path(tempfile.mkdtemp())
    fw = tmp / "cluster.fw"
    FW = (f"[OPTIONS]\nenable: 1\n\n[IPSET base]\n\n2001:db8:bbbb::/64 # base\n"
          f"2001:db8:eeee:ffff::/112 # transito\n!2001:db8:aaaa::/64 # negada\n10.0.0.0/8 # v4\n\n"
          f"[IPSET hosts-ipv6]\n2001:db8:cccc::/64 # otro nodo\n{N64}::/64 # este nodo\n\n"
          f"[RULES]\nIN ACCEPT -source +dc/base\n")
    fw.write_text(FW)
    (tmp / "allow").write_text("520\n1712\nbasura\n")
    qd = tmp / "qemu"
    qd.mkdir()
    (qd / "520.conf").write_text("name: a\nnet0: virtio=52:54:00:00:00:01,bridge=vmbr0,firewall=0\n")
    env = dict(os.environ, NVX_NODE64_CLUSTER_FW=str(fw), NVX_NODE64_EXCL=str(tmp / "allow"),
               NVX_NODE64_STATE=str(tmp / "state.nft"), NVX_NODE64_QEMU_DIR=str(qd),
               NVX_NODE64_SIN64=str(tmp / "sin64"))
    fails = []

    def check(name, ok):
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    def run(*args, mode="drop"):
        return sh("python3", SCRIPT, *args, env=dict(env, MODE=mode), check=False)

    def mirar():
        return run("--mirar").stdout.strip()

    try:
        sh("ip", "link", "set", "lo", "up")
        for pid in (inet, tun, ga, gb):
            n(pid, "ip", "link", "set", "lo", "up")
        sh("ip", "link", "add", "enp5s0", "type", "veth", "peer", "name", "wan", "netns", inet)
        sh("ip", "link", "add", "tun-fsn", "type", "veth", "peer", "name", "eth0", "netns", tun)
        sh("ip", "link", "add", "vmbr0", "type", "bridge")
        sh("ip", "link", "add", "tap520i0", "type", "veth", "peer", "name", "eth0", "netns", ga)
        sh("ip", "link", "add", "tap1409i0", "type", "veth", "peer", "name", "eth0", "netns", gb)
        for t in ("tap520i0", "tap1409i0"):
            sh("ip", "link", "set", t, "master", "vmbr0")
        for d in ("enp5s0", "tun-fsn", "vmbr0", "tap520i0", "tap1409i0"):
            sh("ip", "link", "set", d, "up")
        n(inet, "ip", "link", "set", "wan", "up")
        for pid in (tun, ga, gb):
            n(pid, "ip", "link", "set", "eth0", "up")
        sh("ip", "-6", "addr", "add", f"{WAN_NODE}/64", "dev", "enp5s0", "nodad")
        sh("ip", "-6", "addr", "add", f"{N64}::1/64", "dev", "vmbr0", "nodad")
        sh("ip", "-6", "addr", "add", "fd00:7::1/64", "dev", "tun-fsn", "nodad")
        sh("ip", "-6", "route", "add", "default", "via", WAN_GW)
        sh("ip", "-6", "route", "add", "2001:db8:eeee:ffff::/112", "via", "fd00:7::2", "dev", "tun-fsn")
        n(inet, "ip", "-6", "addr", "add", f"{WAN_GW}/64", "dev", "wan", "nodad")
        for a in (STRANGER, BASE_HOST, BASE_SNAT, OTHER_HOST, OTHER_GUEST, TRANSIT):
            n(inet, "ip", "-6", "addr", "add", f"{a}/128", "dev", "lo")
        n(inet, "ip", "-6", "route", "add", f"{N64}::/64", "via", WAN_NODE)
        n(inet, "ip", "-6", "route", "add", "2a01:4f9:c01f:e::/64", "via", WAN_NODE)
        sh("ip", "-6", "route", "add", "2a01:4f9:c01f:e::/64", "dev", "vmbr0")
        n(tun, "ip", "-6", "addr", "add", "fd00:7::2/64", "dev", "eth0", "nodad")
        n(tun, "ip", "-6", "addr", "add", f"{TRANSIT}/128", "dev", "lo")
        n(tun, "ip", "-6", "route", "add", "default", "via", "fd00:7::1")
        n(ga, "ip", "-6", "addr", "add", f"{EXCL}/64", "dev", "eth0", "nodad")
        n(gb, "ip", "-6", "addr", "add", f"{LEGACY}/64", "dev", "eth0", "nodad")
        for a in (ID_OK, ID_RESV):
            n(gb, "ip", "-6", "addr", "add", f"{a}/64", "dev", "eth0", "nodad")
        for pid in (ga, gb):
            n(pid, "ip", "-6", "route", "add", "default", "via", f"{N64}::1")
        sh("sysctl", "-qw", "net.ipv6.conf.all.forwarding=1")
        time.sleep(1)

        srv = r'''import socket,sys,threading
def serve(c):
 c.sendall(b"hola")
 try:
  while True:
   d=c.recv(10)
   if not d:break
   c.sendall(d)
 except OSError:pass
 c.close()
def l(a,p):
 s=socket.socket(socket.AF_INET6);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_FREEBIND if hasattr(socket,"IPV6_FREEBIND") else 78,1)
 s.bind((a,p));s.listen()
 while True:
  c,_=s.accept();threading.Thread(target=serve,args=(c,),daemon=True).start()
for a in sys.argv[1:]:threading.Thread(target=l,args=(a,3389),daemon=True).start()
threading.Event().wait()
'''
        # Los dos invitados escuchan en ::208 (freebind) para poder mover la direccion.
        procs.append(subprocess.Popen(["nsenter", "-t", ga, "-n", "python3", "-c", srv, EXCL]))
        procs.append(subprocess.Popen(["nsenter", "-t", gb, "-n", "python3", "-c", srv, LEGACY, EXCL]))
        procs.append(subprocess.Popen(["nsenter", "-t", inet, "-n", "python3", "-c", srv, STRANGER]))
        procs.append(subprocess.Popen(["python3", "-c", srv, WAN_NODE]))
        time.sleep(.4)

        cli = r'''import socket,sys
s=socket.socket(socket.AF_INET6);s.settimeout(1.5)
try:
 s.bind((sys.argv[1],0));s.connect((sys.argv[2],3389));print(s.recv(10).decode())
except Exception as e:print("ERR",type(e).__name__)
'''

        def tcp(pid, src, dst):
            return n(pid, "python3", "-c", cli, src, dst, check=False).stdout.strip() == "hola"

        for _ in range(20):
            if tcp(inet, STRANGER, LEGACY) and tcp(inet, STRANGER, EXCL) and tcp(tun, TRANSIT, LEGACY):
                break
            time.sleep(.25)

        def matrix(closed):
            check("desconocido -> heredada", tcp(inet, STRANGER, LEGACY) != closed)
            check("invitado de otro nodo -> heredada", tcp(inet, OTHER_GUEST, LEGACY) != closed)
            check("cliente via SNAT de la base (base64::5ed) -> heredada", tcp(inet, BASE_SNAT, LEGACY) != closed)
            if closed:  # sin guarda tampoco completa: el nodo devuelve el transito por el tun
                check("transito spoofeado por la uplink -> heredada", not tcp(inet, TRANSIT, LEGACY))
            check("host de base (::2) -> heredada", tcp(inet, BASE_HOST, LEGACY))
            check("host de otro nodo (::2) -> heredada", tcp(inet, OTHER_HOST, LEGACY))
            check("transito por tun-* -> heredada", tcp(tun, TRANSIT, LEGACY))
            check("desconocido -> excluida residente (vmid 520)", tcp(inet, STRANGER, EXCL))
            check("invitado sale desde la heredada (respuesta)", tcp(gb, LEGACY, STRANGER))
            check("desconocido -> host del nodo (input)", tcp(inet, STRANGER, WAN_NODE))
            check("invitado sale con su IDENT normal", tcp(gb, ID_OK, STRANGER))
            check("invitado sale con IDENT reservada ::2 (suplantaria a la base)",
                  tcp(gb, ID_RESV, STRANGER) != closed)

        print("== sin guarda");  matrix(False)
        # Conexion ENTRANTE abierta durante count: debe morir al armar drop.
        hold = subprocess.Popen(["nsenter", "-t", inet, "-n", "python3", "-c", r'''import socket,sys,time
s=socket.socket(socket.AF_INET6);s.settimeout(2);s.bind((sys.argv[1],0));s.connect((sys.argv[2],3389));s.recv(10)
print("abierta",flush=True);sys.stdin.readline()
try:
 s.sendall(b"x");print("VIVA" if s.recv(10)==b"x" else "MUERTA")
except Exception as e:print("MUERTA",type(e).__name__)
''', STRANGER, LEGACY], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        procs.append(hold)
        hold.stdout.readline()
        r = run(mode="count")
        print("== MODE=count:", r.stdout.strip(), r.stderr.strip());  matrix(False)
        m = mirar(); print("  ", m)
        check("count: tabla completa", "TABLA=si MODO=count" in m)
        check("count registra los intentos de fuera", "FINAL=0" not in m)
        def vecina_con_excluida(closed):
            n(ga, "ip", "-6", "addr", "del", f"{EXCL}/64", "dev", "eth0")
            n(gb, "ip", "-6", "addr", "add", f"{EXCL}/64", "dev", "eth0", "nodad")
            sh("ip", "-6", "neigh", "flush", "dev", "vmbr0")
            time.sleep(.5)
            ok = tcp(inet, STRANGER, EXCL) != closed
            n(gb, "ip", "-6", "addr", "del", f"{EXCL}/64", "dev", "eth0")
            n(ga, "ip", "-6", "addr", "add", f"{EXCL}/64", "dev", "eth0", "nodad")
            sh("ip", "-6", "neigh", "flush", "dev", "vmbr0")
            time.sleep(.5)
            return ok
        check("count: vecina con la direccion de la excluida SI recibe (solo cuenta)", vecina_con_excluida(False))
        r = run()
        print("== MODE=drop:", r.stdout.strip(), r.stderr.strip());  matrix(True)
        hold.stdin.write("\n"); hold.stdin.flush()
        out = hold.stdout.readline().strip()
        check(f"conexion entrante abierta en count no sobrevive a drop ({out})", out.startswith("MUERTA"))
        m = mirar(); print("  ", m)
        check("drop: tabla completa y cuenta", "TABLA=si MODO=drop" in m and "FINAL=0" not in m)
        check("salida nueva desde node64 contada", "SALIDA=0" not in m)
        check("drop: vecina con la direccion de la excluida NO recibe", vecina_con_excluida(True))
        m = mirar()
        check("contadores nv-excl-ajena y nv-spoof", "AJENA=0" not in m and "SPOOF=0" not in m)
        check("N64OK=si", "N64OK=si" in m)
        # Pasadas sin cambios: no tocan la tabla.
        fin = lambda: int(mirar().split("FINAL=")[1].split()[0])
        f1 = fin()
        r2 = run()
        check("segunda pasada sin cambios no toca la tabla (contadores conservados)",
              r2.stdout == "" and r2.returncode == 0 and fin() >= f1 > 0)
        # La excluida se va del nodo (migracion): su direccion deja de estar exenta.
        (qd / "520.conf").rename(qd / "520.conf.fuera")
        run()
        check("excluida que ya no reside -> cerrada", not tcp(inet, STRANGER, EXCL))
        (qd / "520.conf.fuera").rename(qd / "520.conf")
        run()
        check("excluida que vuelve -> abierta", tcp(inet, STRANGER, EXCL))
        # cluster.fw ilegible con tabla puesta: error y la tabla se conserva.
        fw.rename(tmp / "cluster.fw.x")
        sh("nft", "flush", "set", "inet", "nvxnode64", "infra6")   # forzar diferencia
        r = run()
        check("cluster.fw ilegible -> error y no se toca", r.returncode == 1 and "TABLA=si" in mirar())
        (tmp / "cluster.fw.x").rename(fw)
        run("--quitar")
        # Sin cluster.fw y sin tabla: copia fija (aqui no cubre la base de prueba).
        fw.rename(tmp / "cluster.fw.x")
        r = run()
        check("sin tabla y sin cluster.fw -> copia fija y guarda puesta", r.returncode == 0 and "TABLA=si" in mirar())
        (tmp / "cluster.fw.x").rename(fw)
        run()
        # flush ruleset -> la pasada repone.
        sh("nft", "flush", "ruleset")
        run()
        check("tras flush ruleset la pasada la repone", not tcp(inet, STRANGER, LEGACY) and tcp(inet, STRANGER, EXCL))
        # Tabla incompleta con el mismo nombre -> se repara.
        sh("nft", "flush", "chain", "inet", "nvxnode64", "entrada")
        run()
        check("tabla incompleta -> reparada", "TABLA=si" in mirar() and not tcp(inet, STRANGER, LEGACY))
        # vmbr0 desaparece/renombrado: error, la tabla se conserva.
        sh("ip", "link", "set", "vmbr0", "down"); sh("ip", "link", "set", "vmbr0", "name", "vmbrX")
        r = run()
        check("sin vmbr0 -> error y no se toca", r.returncode == 1 and
              sh("nft", "list", "table", "inet", "nvxnode64", check=False).returncode == 0)
        sh("ip", "link", "set", "vmbrX", "name", "vmbr0"); sh("ip", "link", "set", "vmbr0", "up")
        sh("ip", "-6", "addr", "add", f"{N64}::1/64", "dev", "vmbr0", "nodad")  # el down la borra
        sh("ip", "-6", "route", "replace", "2a01:4f9:c01f:e::/64", "dev", "vmbr0")  # y la ruta IDENT
        time.sleep(1)
        run("--quitar")
        check("--quitar reabre (marcha atras)", any(tcp(inet, STRANGER, LEGACY) or time.sleep(.3)
                                                    for _ in range(10)) and
              "nvxnode64" not in sh("nft", "list", "tables").stdout)
        run()
        sh("ip", "-6", "addr", "del", f"{N64}::1/64", "dev", "vmbr0")
        r = run()
        check("vmbr0 sin /64 y SIN marcador -> error y conserva", r.returncode == 1 and
              sh("nft", "list", "table", "inet", "nvxnode64", check=False).returncode == 0)
        (tmp / "allow").rename(tmp / "allow.x")
        sh("ip", "-6", "addr", "add", f"{N64}::1/64", "dev", "vmbr0", "nodad")
        r = run()
        check("lista de excluidas desaparecida con tabla -> error y conserva", r.returncode == 1 and
              tcp(inet, STRANGER, EXCL))
        (tmp / "allow.x").rename(tmp / "allow")
        sh("ip", "-6", "addr", "del", f"{N64}::1/64", "dev", "vmbr0")
        (tmp / "sin64").write_text("")
        # La puerta de enlace sigue (como /128, no cuenta como /64 propio) para medir la salida.
        sh("ip", "-6", "addr", "add", f"{N64}::1/128", "dev", "vmbr0", "nodad")
        sh("ip", "-6", "route", "replace", f"{N64}::/64", "dev", "vmbr0")
        time.sleep(1)
        r = run()
        m = mirar()
        check("vmbr0 sin /64 CON marcador (Kademar): tabla con node64 vacio y nv-spoof activo",
              r.returncode == 0 and "TABLA=si" in m and "N64OK=si" in m and "NODE64=-" in m
              and not tcp(gb, ID_RESV, STRANGER) and tcp(gb, ID_OK, STRANGER))
    finally:
        for p in procs:
            p.kill()
    print("\nRESULTADO:", "OK" if not fails else f"{len(fails)} FALLOS: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
