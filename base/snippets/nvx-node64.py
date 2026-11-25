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
# Tabla `inet nvxnode64`, hook FORWARD (nunca input: la ::2 y la ::1 del host,
# y el SSH bases->nodo, no pasan por aqui). Lo que se reenvia hacia vmbr0 con
# destino en el /64 del nodo solo pasa si:
#   - nv-excl: el destino es la direccion derivada (<node64>::<vmid hex>) de una
#     VM excluida (ipv6Enabled=true, /etc/neuravps/node64-guard-allow-vmids) QUE
#     RESIDE EN ESTE NODO; y la tabla `bridge nvxnode64b` la entrega SOLO al
#     puerto de esa VM (tap<vmid>iN / fwpr<vmid>pN): una vecina que se ponga esa
#     direccion no se beneficia.
#   - nv-infra: el origen es una direccion de HOST de la infraestructura: ::1 y
#     ::2 de cada prefijo <=/64 de [IPSET base] y de hosts-ipv6 en cluster.fw.
#     Nunca el /64 entero: el de las bases es tambien el SNAT de salida de los
#     clientes (base/64 + sufijo de la VM, vmid >= 100 => sufijo >= ::64) y el de
#     los nodos incluye invitados.
#   - nv-infra-tun: prefijos mas largos que /64 de [IPSET base] (el transito
#     /112 de los tuneles), y solo si entra por un tun-*.
#   - nv-est / nv-rel: respuesta a una conexion que inicio la VM (ct direction
#     reply + established) o ICMPv6 relacionado (PMTU, errores). Una conexion
#     ENTRANTE ya abierta antes de armar no se conserva.
# Lo demas: nv-final, descarta (MODE=drop) o solo cuenta + log limitado
# (MODE=count).
#
# SALIDA (desde el 25/11/2026): lo que un invitado envia desde vmbr0 hacia fuera
# con ORIGEN en el /64 del nodo sale por la uplink del nodo, saltandose las
# bases (pools de salida, «Internet apagado»): la regla de politica 100 solo
# lleva a la base el origen IDENT. Con SALIDA_MODE=drop solo pasan las
# respuestas a lo que la entrada ya permite (nv-salida-resp/-rel); lo demas se
# rechaza: TCP con RST (nv-salida) y el resto con ICMPv6 policy-fail
# (nv-salida-otro), para que el invitado no espere un timeout y use su IDENT o
# IPv4, que si van por las bases. Con SALIDA_MODE=count solo se cuentan los
# paquetes en estado new (incluye retransmisiones; no son conexiones distintas)
# con log limitado "nvx-node64 out:". El trafico del propio
# host (::2, ::1) no es forward y no pasa por aqui.
#
# nv-spoof: descarta lo que un invitado envia por vmbr0 con ORIGEN en las
# direcciones reservadas de la IDENT (::1, ::2 y el transito /112). La base hace
# `snat prefix` de la IDENT a su /64 conservando el sufijo, asi que un invitado
# con IDENT ::2 saldria a Internet como la propia base (::2) y pasaria por
# nv-infra en cualquier nodo. Ninguna VM usa esas direcciones (vmid >= 100).
#
# Reconciliable: lo lanza un timer cada 5 min, con cerrojo. Solo reconstruye las
# tablas (una transaccion nft atomica) si el conjunto calculado cambia o si la
# estructura viva no es la esperada (un `flush ruleset`, un reinicio), asi que
# los contadores se conservan. Ante un ERROR leyendo el estado (ip, cluster.fw,
# lista de excluidas) NO toca lo que haya puesto y sale con error. Si vmbr0
# existe y no tiene /64 propio (Kademar, con el marcador SIN64), pone la tabla
# con node64 vacio: solo actua nv-spoof.
#
#   nvx-node64.py            aplica (MODE y SALIDA_MODE del entorno; defecto drop;
#                            SALIDA_MODE sin definir = MODE)
#   nvx-node64.py --mirar    estado en una linea KEY=VALOR
#   nvx-node64.py --quitar   borra las tablas (marcha atras inmediata)
#
# ⚠️ `policy accept` a proposito y sin tocar otras tablas: un `accept` aqui no
#    anula nvxaisla3 ni nada mas; solo los `drop` son efectivos.
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys

TABLE = "nvxnode64"
BTABLE = "nvxnode64b"
IDENT = ipaddress.ip_network("2a01:4f9:c01f:e::/64")
# Rutas sobreescribibles solo para la prueba en netns (test-node64-netns.py).
CLUSTER_FW = os.environ.get("NVX_NODE64_CLUSTER_FW", "/etc/pve/firewall/cluster.fw")
EXCL_FILE = os.environ.get("NVX_NODE64_EXCL", "/etc/neuravps/node64-guard-allow-vmids")
QEMU_DIR = os.environ.get("NVX_NODE64_QEMU_DIR", "/etc/pve/local/qemu-server")
STATE = os.environ.get("NVX_NODE64_STATE", "/run/nvx-node64.nft")
# Marcador persistente: este nodo NO tiene /64 propio en vmbr0 (Kademar). Sin el,
# un vmbr0 sin /64 es un error y la guarda vigente se conserva.
SIN64 = os.environ.get("NVX_NODE64_SIN64", "/etc/neuravps/node64-guard-sin64")
IDENT_RESERVADAS = ["2a01:4f9:c01f:e::1", "2a01:4f9:c01f:e::2", "2a01:4f9:c01f:e:ffff::/112"]
LOCK = STATE + ".lock"
# Solo si NO hay tabla puesta y cluster.fw no se puede leer (primer arranque sin
# pmxcfs): las bases vigentes (25/11/2026). Con tabla puesta, un fallo de lectura
# conserva la tabla.
BASE_FALLBACK = [
    "2a01:4f8:2b01:124::/64",   # BASE 0 (FSN)
    "2a01:4f9:2a:2d56::/64",    # BASE 1 (HEL)
    "2a01:4f8:fff2:95::/64",    # VIP FSN
    "2a01:4f9:fff1:5f::/64",    # VIP HEL
    "2a01:4f9:c01f:e:ffff::/112",  # transito de los tuneles base<->nodo
]


class Fallo(Exception):
    pass


def sh(*cmd, inp=None):
    return subprocess.run(cmd, input=inp, capture_output=True, text=True)


def node64_prefixes():
    """Lista de /64 propios de vmbr0. [] = vmbr0 existe sin /64. Fallo = no se sabe."""
    r = sh("ip", "-6", "-j", "addr", "show", "dev", "vmbr0")
    if r.returncode != 0:
        raise Fallo(f"ip addr vmbr0: {r.stderr.strip()[:120] or r.returncode}")
    try:
        links = json.loads(r.stdout)
    except ValueError as e:
        raise Fallo(f"ip -j ilegible: {e}")
    if not links:
        raise Fallo("vmbr0 no existe")
    out = []
    for link in links:
        for a in link.get("addr_info", []):
            if a.get("family") != "inet6" or a.get("scope") != "global" or a.get("prefixlen") != 64:
                continue
            net = ipaddress.ip_network(f"{a['local']}/64", strict=False)
            if net.overlaps(IDENT) or net in out:
                continue
            out.append(net)
    return out


def ipsets():
    """{nombre: [red IPv6]} de cluster.fw. Fallo si no se puede leer o no hay base."""
    sets, cur = {}, None
    try:
        text = open(CLUSTER_FW, encoding="utf-8", errors="replace").read()
    except OSError as e:
        raise Fallo(f"cluster.fw: {e}")
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
        tok = line.split()[0]
        if tok.startswith("!"):
            continue  # exclusion dentro del ipset: nunca se convierte en permiso
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            continue
        if net.version == 6:
            sets[cur].append(net)
    if not sets.get("base"):
        raise Fallo("cluster.fw sin [IPSET base] IPv6")
    return sets


def _hosts(net):
    if net.prefixlen == 128:
        return [net]
    return [ipaddress.ip_network(net.network_address + 1),
            ipaddress.ip_network(net.network_address + 2)]


def infra_nets(sets):
    """(direcciones de host /128 de la infraestructura, prefijos solo-por-tunel)."""
    hosts, tun = [], []
    for n in sets.get("base", []) + sets.get("hosts-ipv6", []):
        if n.prefixlen <= 64 or n.prefixlen == 128:
            hosts += _hosts(n)
        elif n in sets.get("base", []):
            tun.append(n)
    dedup = lambda xs: list(dict.fromkeys(xs))
    return dedup(hosts), dedup(tun)


def excl_vms(hay_tabla=False):
    """{vmid: [puertos del puente]} de las excluidas QUE RESIDEN aqui."""
    try:
        lines = open(EXCL_FILE).read().split()
    except FileNotFoundError:
        if hay_tabla:  # la lista la escriben install.sh y el despliegue: si falta, algo va mal
            raise Fallo(f"falta {EXCL_FILE}")
        return {}
    except OSError as e:
        raise Fallo(f"lista de excluidas: {e}")
    out = {}
    for x in lines:
        if not (x.isdigit() and 100 <= int(x) <= 999999):
            continue
        v = int(x)
        try:
            conf = open(os.path.join(QEMU_DIR, f"{v}.conf")).read()
        except FileNotFoundError:
            continue  # no reside en este nodo
        except OSError as e:
            raise Fallo(f"config de {v}: {e}")
        conf = conf.split("\n[", 1)[0]  # sin snapshots
        idx = sorted({int(m) for m in re.findall(r"^net(\d+):", conf, re.M)})
        ports = [p for i in idx for p in (f"tap{v}i{i}", f"fwpr{v}p{i}")]
        if ports:
            out[v] = ports
    return out


def ruleset(mode, p64, infra, infra_tun, excl, smode=None):
    smode = smode or mode
    final = "counter drop" if mode == "drop" else \
        'counter limit rate 6/minute log prefix "nvx-node64 in: " level info'
    ajena = "counter drop" if mode == "drop" else "counter"
    spoof = ajena
    if smode == "drop":
        sal_tcp = "meta l4proto tcp counter reject with tcp reset"
        sal_otro = "counter reject with icmpv6 type policy-fail"
    else:
        sal_tcp = ('meta l4proto tcp ct state new counter '
                   'limit rate 6/minute log prefix "nvx-node64 out: " level info')
        sal_otro = ('meta l4proto != tcp ct state new counter '
                    'limit rate 6/minute log prefix "nvx-node64 out: " level info')
    el = lambda xs: f"elements = {{ {', '.join(xs)} }}" if xs else ""
    pref = [str(n) for n in p64]
    ex_addr, ex_own = [], []
    for n in p64:
        for v, ports in sorted(excl.items()):
            a = str(n.network_address + v)
            ex_addr.append(a)
            ex_own += [f'{a} . "{p}"' for p in ports]
    return f"""table inet {TABLE}
delete table inet {TABLE}
table bridge {BTABLE}
delete table bridge {BTABLE}
table inet {TABLE} {{
    comment "nvx-node64 modo={mode} salida={smode}"
    set node64 {{ type ipv6_addr; flags interval; {el(pref)} }}
    set infra6 {{ type ipv6_addr; {el([str(n.network_address) for n in infra])} }}
    set infra6tun {{ type ipv6_addr; flags interval; auto-merge; {el([str(n) for n in infra_tun])} }}
    set excl6 {{ type ipv6_addr; {el(ex_addr)} }}
    set ident_resv {{ type ipv6_addr; flags interval; elements = {{ {', '.join(IDENT_RESERVADAS)} }} }}
    chain guarda {{
        type filter hook forward priority filter; policy accept;
        iifname "vmbr0" ip6 saddr @ident_resv {spoof} comment "nv-spoof"
        oifname "vmbr0" ip6 daddr @node64 jump entrada
        iifname "vmbr0" oifname != "vmbr0" ip6 saddr @node64 jump salida64
    }}
    chain salida64 {{
        ct direction reply counter accept comment "nv-salida-resp"
        ct state related counter accept comment "nv-salida-rel"
        {sal_tcp} comment "nv-salida"
        {sal_otro} comment "nv-salida-otro"
    }}
    chain entrada {{
        ip6 daddr @excl6 counter accept comment "nv-excl"
        ip6 saddr @infra6 counter accept comment "nv-infra"
        iifname "tun-*" ip6 saddr @infra6tun counter accept comment "nv-infra-tun"
        ct direction reply ct state established counter accept comment "nv-est"
        ct state related counter accept comment "nv-rel"
        {final} comment "nv-final"
    }}
}}
table bridge {BTABLE} {{
    comment "nvx-node64 modo={mode} salida={smode}"
    set excl6 {{ type ipv6_addr; {el(ex_addr)} }}
    set excl_own {{ type ipv6_addr . ifname; {el(ex_own)} }}
    chain salida {{
        type filter hook output priority filter; policy accept;
        ip6 daddr @excl6 ip6 daddr . oifname != @excl_own {ajena} comment "nv-excl-ajena"
    }}
}}
"""


ESPERADAS = {"nv-spoof", "nv-salida", "nv-salida-resp", "nv-salida-rel", "nv-salida-otro", "nv-excl", "nv-infra", "nv-infra-tun", "nv-est", "nv-rel", "nv-final"}


def estado():
    """(modo, {comentario: paquetes}, ok_estructura) o None si no hay tabla inet."""
    r = sh("nft", "-j", "list", "table", "inet", TABLE)
    if r.returncode != 0:
        return None
    c, mode, jumps = {}, "?", set()
    for o in json.loads(r.stdout).get("nftables", []):
        t = o.get("table")
        if t and "comment" in t:
            mode = t["comment"].split("modo=")[-1]
        rule = o.get("rule")
        if not rule:
            continue
        for e in rule.get("expr", []):
            if "jump" in e:
                jumps.add(e["jump"].get("target"))
            if "counter" in e and "comment" in rule:
                c[rule["comment"]] = e["counter"]["packets"]
    rb = sh("nft", "-j", "list", "table", "bridge", BTABLE)
    if rb.returncode == 0:
        for o in json.loads(rb.stdout).get("nftables", []):
            rule = o.get("rule")
            if rule and "comment" in rule:
                for e in rule.get("expr", []):
                    if "counter" in e:
                        c[rule["comment"]] = e["counter"]["packets"]
    ok = {"entrada", "salida64"} <= jumps and ESPERADAS <= set(c) and "nv-excl-ajena" in c
    return mode, c, ok, set64()


def set64():
    """Prefijos que la tabla viva guarda de verdad (conjunto node64)."""
    r = sh("nft", "-j", "list", "set", "inet", TABLE, "node64")
    out = set()
    if r.returncode != 0:
        return out
    for o in json.loads(r.stdout).get("nftables", []):
        for e in (o.get("set") or {}).get("elem", []) or []:
            e = e.get("elem", {}).get("val", e) if isinstance(e, dict) and "elem" in e else e
            if isinstance(e, dict) and "prefix" in e:
                out.add(f"{e['prefix']['addr']}/{e['prefix']['len']}")
            elif isinstance(e, str):
                out.add(e)
    return out


def unit(prop, name):
    r = sh("systemctl", prop, name)
    return (r.stdout.strip() or "?").split()[0]


def mirar():
    try:
        vivos = node64_prefixes()
        p64 = ",".join(map(str, vivos)) or "-"
    except Fallo:
        vivos, p64 = None, "error"
    st = estado()
    tail = (f"TIMER={unit('is-enabled', 'nvx-node64.timer')} "
            f"TIMERACT={unit('is-active', 'nvx-node64.timer')} "
            f"SERVICIO={unit('is-enabled', 'nvx-node64.service')} "
            f"ULTIMA={sh('systemctl', 'show', '-p', 'Result', '--value', 'nvx-node64.service').stdout.strip() or '?'}")
    if st is None:
        br = "si" if sh("nft", "list", "table", "bridge", BTABLE).returncode == 0 else "no"
        print(f"TABLA=no BR={br} NODE64={p64} {tail}")
        return
    mode, c, ok, s64 = st
    mode, _, smode = mode.partition(" salida=")
    n64ok = "si" if vivos is not None and s64 == {str(n) for n in vivos} else "no"
    print(f"TABLA={'si' if ok else 'incompleta'} MODO={mode} MODOSAL={smode or '?'} NODE64={p64} N64OK={n64ok} SPOOF={c.get('nv-spoof', 0)} "
          f"EXCL={c.get('nv-excl', 0)} AJENA={c.get('nv-excl-ajena', 0)} "
          f"INFRA={c.get('nv-infra', 0) + c.get('nv-infra-tun', 0)} "
          f"EST={c.get('nv-est', 0) + c.get('nv-rel', 0)} FINAL={c.get('nv-final', 0)} "
          f"SALIDA={c.get('nv-salida', 0) + c.get('nv-salida-otro', 0)} "
          f"SALRESP={c.get('nv-salida-resp', 0) + c.get('nv-salida-rel', 0)} {tail}")


def quitar():
    sh("nft", "delete", "table", "inet", TABLE)
    sh("nft", "delete", "table", "bridge", BTABLE)
    try:
        os.unlink(STATE)
    except OSError:
        pass
    print("nvx-node64: tablas quitadas")


def aplicar(mode, smode):
    tag = f"{mode} salida={smode}"
    st = estado()
    p64 = node64_prefixes()          # Fallo => no se toca nada
    if not p64 and not os.path.exists(SIN64):
        raise Fallo(f"vmbr0 sin /64 propio y sin el marcador {SIN64}")
    # Sin /64 (Kademar, marcado): la tabla va igual, con node64 vacio. La entrada
    # no tiene nada que guardar, pero nv-spoof si (el nodo lleva IDENT a la base).
    try:
        sets = ipsets()
    except Fallo:
        if st is not None:
            raise                    # conserva la tabla puesta
        sets = {"base": [ipaddress.ip_network(x) for x in BASE_FALLBACK]}
    infra, infra_tun = infra_nets(sets)
    text = ruleset(mode, p64, infra, infra_tun, excl_vms(st is not None), smode)
    try:
        prev = open(STATE).read()
    except OSError:
        prev = None
    if st is not None and st[2] and st[0] == tag and prev == text \
            and st[3] == {str(n) for n in p64}:
        return 0
    r = sh("nft", "-f", "-", inp=text)
    if r.returncode != 0:
        raise Fallo(f"nft: {r.stderr.strip()[:300]}")
    st = estado()
    if st is None or not st[2] or st[0] != tag or st[3] != {str(n) for n in p64}:
        raise Fallo("la tabla no quedo como se esperaba")
    with open(STATE + ".tmp", "w") as f:
        f.write(text)
    os.replace(STATE + ".tmp", STATE)
    print(f"nvx-node64: guarda puesta (modo {mode}, salida {smode}) en {', '.join(map(str, p64)) or 'ningun /64 (solo nv-spoof)'}")
    return 0


def main():
    if "--mirar" in sys.argv:
        mirar()
        return 0
    mode = os.environ.get("MODE", "drop")
    smode = os.environ.get("SALIDA_MODE") or mode
    if "--quitar" not in sys.argv and (mode not in ("count", "drop") or smode not in ("count", "drop")):
        print("nvx-node64: MODE y SALIDA_MODE deben ser count o drop", file=sys.stderr)
        return 2
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if "--quitar" in sys.argv:
            quitar()
            return 0
        try:
            return aplicar(mode, smode)
        except Fallo as e:
            print(f"nvx-node64: ERROR, no se cambia nada: {e}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
