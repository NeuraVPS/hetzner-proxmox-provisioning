#!/usr/bin/env python3
# E) Guarda del /64 propio del NODO: corta la entrada directa desde Internet a
#    las VMs que conservan (o se ponen) una IPv6 del /64 del nodo.
#
# QUE RESUELVE
# Cada nodo tiene su /64 publico on-link en vmbr0 (::1/64 en el puente, ::2/128
# en la uplink). La IPv6 publica de las VMs ya NO sale de ahi: es el /64 de la
# BASE + vmid, que la base traduce a la IDENT (2a01:4f9:c01f:e::/64) y filtra
# (ipv6Enabled, rdp/ssh/samba, rdpguard, IPs de confianza, modo cerrado). Pero si
# un invitado conserva `<node64>::<vmid hex>` (modelo anterior a IDENT) o se pone
# a mano cualquier direccion del /64 del nodo (es Administrador), Internet llega
# DIRECTO por la uplink del nodo -> vmbr0 y se salta todo lo anterior. Caso real
# el 25/11/2026: VM1409 (nodo 0000061, ipv6Enabled=false) con 3389/445/22
# abiertos desde fuera por 2a01:4f9:3081:3cdd::581.
#
# QUE HACE
# Tabla propia `inet nvxnode64`, hook FORWARD (nunca input: la ::2 y la ::1 del
# host, y el SSH bases->nodo, no pasan por aqui). Lo que se reenvia hacia vmbr0
# con destino en el /64 del nodo solo pasa si:
#   - el destino es la direccion derivada de una VM excluida (ipv6Enabled=true,
#     lista en /etc/neuravps/node64-guard-allow-vmids), o
#   - el origen es infraestructura: [IPSET base] de cluster.fw (+ copia fija por
#     si el fichero no se puede leer) y la ::1/::2 de cada /64 de hosts-ipv6
#     (los hosts de los nodos; NO el /64 entero, que incluye invitados), o
#   - es respuesta de una conexion ya establecida (ct established/related),
#     igual que el forward de las bases.
# Lo demas se descarta (MODE=drop) o solo se cuenta y se registra (MODE=count).
# Ademas cuenta (sin cortar nunca) la SALIDA vmbr0->fuera con origen en el /64
# del nodo, para ver si algun invitado sigue usando esa via.
#
# MODE=count: solo cuenta + log limitado (prefijo "nvx-node64"). Para medir.
# MODE=drop:  descarta (y cuenta). Defecto si no hay drop-in.
#
# Reconciliable: lo lanza un timer cada 5 min. Solo reconstruye la tabla si el
# conjunto calculado cambia o si la tabla falta (un `flush ruleset` de nftables,
# un reinicio), asi que los contadores se conservan entre pasadas. El cambio es
# atomico (una sola transaccion nft). Si el nodo no tiene /64 en vmbr0
# (Kademar), no pone nada.
#
#   nvx-node64.py            aplica (MODE del entorno)
#   nvx-node64.py --mirar    estado en una linea KEY=VALOR
#   nvx-node64.py --quitar   borra la tabla (marcha atras inmediata)
#
# ⚠️ `policy accept` a proposito y sin tocar otras tablas: un `accept` aqui no
#    anula nvxaisla3 ni nada mas; solo el `drop` final es efectivo.
import ipaddress
import json
import os
import subprocess
import sys

TABLE = "nvxnode64"
IDENT = ipaddress.ip_network("2a01:4f9:c01f:e::/64")
# Rutas sobreescribibles solo para la prueba en netns (test-node64-netns.py).
CLUSTER_FW = os.environ.get("NVX_NODE64_CLUSTER_FW", "/etc/pve/firewall/cluster.fw")
EXCL_FILE = os.environ.get("NVX_NODE64_EXCL", "/etc/neuravps/node64-guard-allow-vmids")
STATE = os.environ.get("NVX_NODE64_STATE", "/run/nvx-node64.nft")
# Copia de [IPSET base] (25/11/2026). Se une a lo que diga cluster.fw: si el
# fichero falta o viene roto, las bases siguen dentro.
BASE_FALLBACK = [
    "2a01:4f8:2b01:124::/64",   # BASE 0 (FSN)
    "2a01:4f9:2a:2d56::/64",    # BASE 1 (HEL)
    "2a01:4f8:2b03:18a9::/64",  # BASE 0 legacy
    "2a01:4f9:3070:3984::/64",  # BASE 1 legacy
    "2a01:4f8:fff2:95::/64",    # VIP FSN
    "2a01:4f9:fff1:5f::/64",    # VIP HEL
    "2a01:4f9:c01f:e:ffff::/112",  # transito de los tuneles base<->nodo
]


def sh(*cmd, inp=None):
    return subprocess.run(cmd, input=inp, capture_output=True, text=True)


def node64_prefixes():
    r = sh("ip", "-6", "-j", "addr", "show", "dev", "vmbr0", "scope", "global")
    if r.returncode != 0 or not r.stdout.strip():
        return []
    out = []
    for link in json.loads(r.stdout):
        for a in link.get("addr_info", []):
            if a.get("family") != "inet6" or a.get("prefixlen") != 64:
                continue
            net = ipaddress.ip_network(f"{a['local']}/64", strict=False)
            if net.overlaps(IDENT) or net.is_link_local or net in out:
                continue
            out.append(net)
    return out


def ipsets():
    """{nombre: [red IPv6]} de cluster.fw. Ignora IPv4, comentarios y basura."""
    sets, cur = {}, None
    try:
        text = open(CLUSTER_FW, encoding="utf-8", errors="replace").read()
    except OSError:
        return sets
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            low = line.lower()
            cur = low[len("[ipset "):-1].strip() if low.startswith("[ipset ") else None
            if cur is not None:
                sets.setdefault(cur, [])
            continue
        if cur is None:
            continue
        tok = line.split()[0].lstrip("!")
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            continue
        if net.version == 6:
            sets[cur].append(net)
    return sets


def infra_nets():
    s = ipsets()
    nets = [ipaddress.ip_network(x) for x in BASE_FALLBACK] + s.get("base", [])
    for h in s.get("hosts-ipv6", []):
        if h.prefixlen == 128:
            nets.append(h)
        elif h.prefixlen <= 64:
            # Solo las direcciones de HOST del nodo (::1 en vmbr0, ::2 en la
            # uplink). El /64 entero incluye invitados de otros nodos, que
            # tambien pueden ponerse una direccion de su /64.
            nets.append(ipaddress.ip_network(h.network_address + 1))
            nets.append(ipaddress.ip_network(h.network_address + 2))
    seen, out = set(), []
    for n in nets:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def excl_vmids():
    try:
        lines = open(EXCL_FILE).read().split()
    except OSError:
        return []
    return sorted({int(x) for x in lines if x.isdigit() and 100 <= int(x) <= 999999})


def ruleset(mode, p64, infra, excl):
    final = "counter drop" if mode == "drop" else \
        'counter limit rate 6/minute log prefix "nvx-node64 in: " level info'
    salida_log = "" if mode == "drop" else \
        ' limit rate 6/minute log prefix "nvx-node64 out: " level info'
    pref = ", ".join(str(n) for n in p64)
    ex = sorted({str(n.network_address + v) for n in p64 for v in excl})
    excl_el = f"elements = {{ {', '.join(ex)} }}" if ex else ""
    infra_el = ", ".join(str(n) for n in infra)
    return f"""table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
    comment "nvx-node64 modo={mode}"
    set node64 {{ type ipv6_addr; flags interval; elements = {{ {pref} }} }}
    set infra6 {{ type ipv6_addr; flags interval; auto-merge; elements = {{ {infra_el} }} }}
    set excl6 {{ type ipv6_addr; {excl_el} }}
    chain guarda {{
        type filter hook forward priority filter; policy accept;
        oifname "vmbr0" ip6 daddr @node64 jump entrada
        iifname "vmbr0" oifname != "vmbr0" ip6 saddr @node64 counter{salida_log} comment "nv-salida"
    }}
    chain entrada {{
        ip6 daddr @excl6 counter accept comment "nv-excl"
        ip6 saddr @infra6 counter accept comment "nv-infra"
        ct state established,related counter accept comment "nv-est"
        {final} comment "nv-final"
    }}
}}
"""


def counters():
    r = sh("nft", "-j", "list", "table", "inet", TABLE)
    if r.returncode != 0:
        return None
    c, mode = {}, "?"
    for o in json.loads(r.stdout).get("nftables", []):
        t = o.get("table")
        if t and "comment" in t:
            mode = t["comment"].split("modo=")[-1]
        rule = o.get("rule")
        if not rule or "comment" not in rule:
            continue
        for e in rule.get("expr", []):
            if "counter" in e:
                c[rule["comment"]] = e["counter"]["packets"]
    return mode, c


def mirar():
    p64 = node64_prefixes()
    st = counters()
    unit = sh("systemctl", "is-enabled", "nvx-node64.timer").stdout.strip() or "?"
    last = sh("systemctl", "show", "-p", "Result", "--value", "nvx-node64.service").stdout.strip() or "?"
    if st is None:
        print(f"TABLA=no NODE64={','.join(map(str, p64)) or '-'} TIMER={unit} ULTIMA={last}")
        return
    mode, c = st
    print(f"TABLA=si MODO={mode} NODE64={','.join(map(str, p64)) or '-'} "
          f"EXCL={c.get('nv-excl', 0)} INFRA={c.get('nv-infra', 0)} EST={c.get('nv-est', 0)} "
          f"FINAL={c.get('nv-final', 0)} SALIDA={c.get('nv-salida', 0)} TIMER={unit} ULTIMA={last}")


def main():
    if "--mirar" in sys.argv:
        mirar()
        return 0
    if "--quitar" in sys.argv:
        sh("nft", "delete", "table", "inet", TABLE)
        try:
            os.unlink(STATE)
        except OSError:
            pass
        print("nvx-node64: tabla quitada")
        return 0
    mode = os.environ.get("MODE", "drop")
    if mode not in ("count", "drop"):
        print("nvx-node64: MODE debe ser count o drop", file=sys.stderr)
        return 2
    p64 = node64_prefixes()
    if not p64:
        sh("nft", "delete", "table", "inet", TABLE)
        print("nvx-node64: vmbr0 sin /64 propio; nada que guardar")
        return 0
    text = ruleset(mode, p64, infra_nets(), excl_vmids())
    have = sh("nft", "list", "table", "inet", TABLE).returncode == 0
    try:
        prev = open(STATE).read()
    except OSError:
        prev = None
    if have and prev == text and "--forzar" not in sys.argv:
        return 0
    r = sh("nft", "-f", "-", inp=text)
    if r.returncode != 0:
        print(f"nvx-node64: nft fallo: {r.stderr.strip()}", file=sys.stderr)
        return 1
    st = counters()
    if st is None or st[0] != mode or "nv-final" not in st[1]:
        print("nvx-node64: la tabla no quedo como se esperaba", file=sys.stderr)
        return 1
    with open(STATE + ".tmp", "w") as f:
        f.write(text)
    os.replace(STATE + ".tmp", STATE)
    print(f"nvx-node64: guarda puesta (modo {mode}) en {', '.join(map(str, p64))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
