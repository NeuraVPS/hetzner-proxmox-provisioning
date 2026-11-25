#!/usr/bin/env python3
"""Prueba real de nvx-node64.py en espacios de nombres privados; sin E/S de produccion.

    unshare -rn python3 base/snippets/test-node64-netns.py

El espacio actual hace de NODO (uplink enp5s0 + puente vmbr0 con su /64), uno
hijo de INTERNET (desconocido, base, host de otro nodo, invitado de otro nodo) y
otro de INVITADO (direccion heredada del /64 del nodo + la derivada de una VM
excluida). Conexiones TCP de verdad antes y despues de poner la guarda.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SCRIPT = str(HERE / "nvx-node64.py")
N64 = "2001:db8:64"           # /64 del nodo (on-link en vmbr0)
LEGACY = f"{N64}::581"        # VM1409-like: ipv6Enabled=false
EXCL = f"{N64}::208"          # vmid 520 excluido (ipv6Enabled=true)
WAN_NODE, WAN_GW = "2001:db8:ffff::2", "2001:db8:ffff::1"
STRANGER, BASE, OTHER_HOST, OTHER_GUEST = (
    "2001:db8:aaaa::7", "2001:db8:bbbb::2", "2001:db8:cccc::2", "2001:db8:cccc::99")


def sh(*cmd, data=None, check=True, env=None):
    p = subprocess.run(cmd, input=data, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise RuntimeError(f"{cmd}: {p.stderr} {p.stdout}")
    return p


def main():
    procs = []

    def ns():
        p = subprocess.Popen(["unshare", "-n", "sleep", "300"])
        procs.append(p)
        time.sleep(.15)
        return str(p.pid)

    inet, guest = ns(), ns()

    def n(pid, *cmd, **kw):
        return sh("nsenter", "-t", pid, "-n", *cmd, **kw)

    tmp = tempfile.mkdtemp()
    fw = Path(tmp, "cluster.fw")
    fw.write_text(f"[OPTIONS]\nenable: 1\n\n[IPSET base]\n\n2001:db8:bbbb::/64 # base\n"
                  f"10.0.0.0/8 # v4 ignorada\n\n[IPSET hosts-ipv6]\n2001:db8:cccc::/64 # otro nodo\n"
                  f"{N64}::/64 # este nodo\n\n[RULES]\nIN ACCEPT -source +dc/base\n")
    Path(tmp, "allow").write_text("520\nbasura\n")
    env = dict(os.environ, NVX_NODE64_CLUSTER_FW=str(fw), NVX_NODE64_EXCL=str(Path(tmp, "allow")),
               NVX_NODE64_STATE=str(Path(tmp, "state.nft")))
    fails = []

    def check(name, ok):
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    try:
        sh("ip", "link", "set", "lo", "up")
        for pid in (inet, guest):
            n(pid, "ip", "link", "set", "lo", "up")
        sh("ip", "link", "add", "enp5s0", "type", "veth", "peer", "name", "wan", "netns", inet)
        sh("ip", "link", "add", "vmbr0", "type", "bridge")
        sh("ip", "link", "add", "tap520i0", "type", "veth", "peer", "name", "eth0", "netns", guest)
        sh("ip", "link", "set", "tap520i0", "master", "vmbr0")
        for d in ("enp5s0", "vmbr0", "tap520i0"):
            sh("ip", "link", "set", d, "up")
        n(inet, "ip", "link", "set", "wan", "up")
        n(guest, "ip", "link", "set", "eth0", "up")
        sh("ip", "-6", "addr", "add", f"{WAN_NODE}/64", "dev", "enp5s0", "nodad")
        sh("ip", "-6", "addr", "add", f"{N64}::1/64", "dev", "vmbr0", "nodad")
        sh("ip", "-6", "route", "add", "default", "via", WAN_GW)
        n(inet, "ip", "-6", "addr", "add", f"{WAN_GW}/64", "dev", "wan", "nodad")
        for a in (STRANGER, BASE, OTHER_HOST, OTHER_GUEST):
            n(inet, "ip", "-6", "addr", "add", f"{a}/128", "dev", "lo")
        n(inet, "ip", "-6", "route", "add", f"{N64}::/64", "via", WAN_NODE)
        for a in (LEGACY, EXCL):
            n(guest, "ip", "-6", "addr", "add", f"{a}/64", "dev", "eth0", "nodad")
        n(guest, "ip", "-6", "route", "add", "default", "via", f"{N64}::1")
        sh("sysctl", "-qw", "net.ipv6.conf.all.forwarding=1")
        time.sleep(1)

        srv = r'''import socket,sys,threading
def l(a,p):
 s=socket.socket(socket.AF_INET6);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((a,p));s.listen()
 while True:
  c,_=s.accept();c.sendall(b"hola");c.close()
for a in sys.argv[1:]:threading.Thread(target=l,args=(a,3389),daemon=True).start()
threading.Event().wait()
'''
        procs.append(subprocess.Popen(["nsenter", "-t", guest, "-n", "python3", "-c", srv, LEGACY, EXCL]))
        procs.append(subprocess.Popen(["nsenter", "-t", inet, "-n", "python3", "-c", srv, STRANGER]))
        procs.append(subprocess.Popen(["python3", "-c", srv, WAN_NODE]))  # SSH-like al host
        time.sleep(.4)

        cli = r'''import socket,sys
s=socket.socket(socket.AF_INET6);s.settimeout(1.5)
try:
 s.bind((sys.argv[1],0));s.connect((sys.argv[2],3389));print(s.recv(10).decode())
except Exception as e:print("ERR",type(e).__name__)
'''

        def tcp(pid, src, dst):
            out = n(pid, "python3", "-c", cli, src, dst, check=False).stdout.strip()
            return out == "hola"

        def matrix(expect_closed):
            check("desconocido -> heredada", tcp(inet, STRANGER, LEGACY) != expect_closed)
            check("invitado de otro nodo -> heredada", tcp(inet, OTHER_GUEST, LEGACY) != expect_closed)
            check("base -> heredada", tcp(inet, BASE, LEGACY))
            check("host de otro nodo (::2) -> heredada", tcp(inet, OTHER_HOST, LEGACY))
            check("desconocido -> excluida (vmid 520)", tcp(inet, STRANGER, EXCL))
            check("invitado sale desde la heredada (respuesta)", tcp(guest, LEGACY, STRANGER))
            check("desconocido -> host del nodo (input)", tcp(inet, STRANGER, WAN_NODE))

        for _ in range(20):  # calentar ND/rutas antes de medir
            if tcp(inet, STRANGER, LEGACY) and tcp(inet, STRANGER, EXCL):
                break
            time.sleep(.25)
        print("== sin guarda");  matrix(False)
        r = sh("python3", SCRIPT, env=dict(env, MODE="count"))
        print("== MODE=count:", r.stdout.strip());  matrix(False)
        m = sh("python3", SCRIPT, "--mirar", env=env).stdout
        print("  ", m.strip())
        check("count registra los dos intentos de fuera", "FINAL=2" in m)
        r = sh("python3", SCRIPT, env=dict(env, MODE="drop"))
        print("== MODE=drop:", r.stdout.strip());  matrix(True)
        m = sh("python3", SCRIPT, "--mirar", env=env).stdout
        print("  ", m.strip())
        check("drop cuenta lo descartado", "MODO=drop" in m and "FINAL=0" not in m)
        check("salida desde node64 contada", "SALIDA=0" not in m)
        r2 = sh("python3", SCRIPT, env=dict(env, MODE="drop"))
        m2 = sh("python3", SCRIPT, "--mirar", env=env).stdout
        check("segunda pasada sin cambios no toca la tabla (contadores intactos)",
              r2.stdout == "" and m2.split("TIMER")[0] == m.split("TIMER")[0])
        sh("nft", "flush", "ruleset")
        sh("python3", SCRIPT, env=dict(env, MODE="drop"))
        check("tras flush ruleset la pasada la repone", not tcp(inet, STRANGER, LEGACY))
        sh("python3", SCRIPT, "--quitar", env=env)
        check("--quitar reabre (marcha atras)", tcp(inet, STRANGER, LEGACY))
        sh("ip", "-6", "addr", "del", f"{N64}::1/64", "dev", "vmbr0")
        r = sh("python3", SCRIPT, env=dict(env, MODE="drop"))
        check("sin /64 en vmbr0 (Kademar) no pone nada",
              "nada que guardar" in r.stdout and sh("nft", "list", "tables").stdout.find("nvxnode64") < 0)
    finally:
        for p in procs:
            p.kill()
    print("\nRESULTADO:", "OK" if not fails else f"{len(fails)} FALLOS: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
