#!/usr/bin/env python3
"""Estructura nft de los pools de IPv4 de salida por VM, en vivo y persistida.

Añade a `table ip nat`, SIN `flush` (hay clientes encima):

    map egress_hel4 { type ipv4_addr : ipv4_addr; }       # privada -> publica
    map egress_fsn4 { type ipv4_addr : ipv4_addr; }
    chain egress_pools {
        iifname "tun-hp*" ip saddr @egress_hel4 snat to ip saddr map @egress_hel4
        iifname "tun-fp*" ip saddr @egress_fsn4 snat to ip saddr map @egress_fsn4
        iifname "tun-hp*" ip saddr @egress_fsn4 snat to ip saddr map @egress_fsn4
        iifname "tun-fp*" ip saddr @egress_hel4 snat to ip saddr map @egress_hel4
    }
    # en postrouting, JUSTO ANTES de la regla general de 10.64.0.0/16:
    ip saddr 10.64.0.0/16 iifname "tun-*" oifname "<UP>" jump egress_pools

y el include de los elementos (`/etc/nftables.d/base-nat-egress-pools*.nft`,
con comodin: un include con comodin sin fichero NO rompe la carga; uno literal
sin fichero deja la base sin firewall ni DNAT).

Con los mapas VACIOS esto no cambia ni un paquete: el salto no casa nada,
vuelve, y manda la regla general de siempre. Los mapas los puebla
`sync-base-nat.py` y solo con `config/egressPools.enabled`.

Uso (en la base, como root):
    persist-egress-pools-nft.py            # SIMULACRO: dice que haria y valida el fichero
    persist-egress-pools-nft.py --apply    # en vivo (una transaccion) + fichero
    persist-egress-pools-nft.py --rollback # quita SOLO el salto (en vivo + fichero)

Reglas de la casa que respeta: nunca `flush ruleset` ni recargar
/etc/nftables.conf en caliente; el fichero se valida con `nft -c` y se guarda
copia en /etc/nftables.conf.pre-egress-pools antes de sobrescribir.
Ver NeuraVPS docs/EGRESS_IPV4_POOLS_ROLLOUT.md.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

CONF = os.environ.get("NFT_CONF", "/etc/nftables.conf")
DEFAULTS = os.environ.get("BASE_NAT_DEFAULTS", "/etc/default/base-nat")
INCLUDE_DIR = os.environ.get("NFT_INCLUDE_DIR", "/etc/nftables.d")
ELEMENTS_INCLUDE = f'include "{INCLUDE_DIR}/base-nat-elements.nft"'
EGRESS_INCLUDE = f'include "{INCLUDE_DIR}/base-nat-egress-pools*.nft"'
EGRESS_FILE = f"{INCLUDE_DIR}/base-nat-egress-pools.nft"
MARK = "# --- egress-pools (IPv4 de salida por VM)"

MAPS = ("egress_hel4", "egress_fsn4")
CHAIN_RULES = (
    'iifname "tun-hp*" ip saddr @egress_hel4 snat to ip saddr map @egress_hel4',
    'iifname "tun-fp*" ip saddr @egress_fsn4 snat to ip saddr map @egress_fsn4',
    'iifname "tun-hp*" ip saddr @egress_fsn4 snat to ip saddr map @egress_fsn4',
    'iifname "tun-fp*" ip saddr @egress_hel4 snat to ip saddr map @egress_hel4',
)


def sh(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True)


def main_ipv4() -> str:
    for line in open(DEFAULTS):
        m = re.match(r"\s*MAIN_IPV4\s*=\s*['\"]?([0-9.]+)", line)
        if m:
            return m.group(1)
    sys.exit(f"ABORTO: MAIN_IPV4 no esta en {DEFAULTS}")


def general_rule_re(main: str) -> re.Pattern:
    return re.compile(
        r'^[ \t]*ip saddr 10\.64\.0\.0/16 iifname "tun-\*" oifname "([^"]+)" snat to '
        + re.escape(main) + r"\b.*$", re.M)


def jump_rule(up: str) -> str:
    return f'ip saddr 10.64.0.0/16 iifname "tun-*" oifname "{up}" jump egress_pools'


# --- en vivo -----------------------------------------------------------------

def live_plan(main: str) -> tuple[list[str], str]:
    """(transaccion nft, interfaz de subida). Vacia si ya esta aplicado."""
    listing = sh("nft", "-a", "list", "chain", "ip", "nat", "postrouting")
    if listing.returncode != 0:
        sys.exit(f"ABORTO: no puedo leer ip nat postrouting: {listing.stderr.strip()}")
    general = [l for l in listing.stdout.splitlines() if general_rule_re(main).match(l)]
    if len(general) != 1:
        sys.exit(f"ABORTO: esperaba UNA regla general de 10.64.0.0/16 hacia {main}, hay {len(general)}")
    up = general_rule_re(main).match(general[0]).group(1)
    handle = re.search(r"# handle (\d+)", general[0]).group(1)
    if "jump egress_pools" in listing.stdout:
        return [], up
    tx = [f"add map ip nat {m} {{ type ipv4_addr : ipv4_addr; }}" for m in MAPS]
    tx.append("add chain ip nat egress_pools")
    existing = sh("nft", "list", "chain", "ip", "nat", "egress_pools")
    if existing.returncode != 0 or "snat to" not in existing.stdout:
        tx += [f"add rule ip nat egress_pools {r}" for r in CHAIN_RULES]
    tx.append(f"insert rule ip nat postrouting position {handle} {jump_rule(up)}")
    return tx, up


def apply_live(tx: list[str]) -> None:
    if not tx:
        print("  vivo: ya aplicado")
        return
    r = sh("nft", "-f", "-", stdin="\n".join(tx) + "\n")
    if r.returncode != 0:
        sys.exit(f"ABORTO en vivo (transaccion atomica, no quedo nada a medias): {r.stderr.strip()}")
    print(f"  vivo: {len(tx)} ordenes en una transaccion")


def rollback_live() -> None:
    listing = sh("nft", "-a", "list", "chain", "ip", "nat", "postrouting").stdout
    handles = re.findall(r"jump egress_pools # handle (\d+)", listing)
    for h in handles:
        r = sh("nft", "delete", "rule", "ip", "nat", "postrouting", "handle", h)
        if r.returncode != 0:
            sys.exit(f"ABORTO borrando handle {h}: {r.stderr.strip()}")
    print(f"  vivo: {len(handles)} salto(s) retirado(s); mapas y cadena quedan (inertes)")


# --- fichero -----------------------------------------------------------------

def file_edit(src: str, main: str, up: str) -> str:
    if MARK in src:
        return src
    edits = [
        ("table ip nat {\n",
         "table ip nat {\n"
         f"{MARK}\n"
         "    # IPv4 privada de la VM -> su IPv4 publica del pool de cada region. Los\n"
         "    # puebla sync-base-nat.py (y el include de abajo) SOLO en la base a la que\n"
         "    # Hetzner enruta el bloque. Vacios = comportamiento de siempre.\n"
         + "".join(f"    map {m} {{\n        type ipv4_addr : ipv4_addr\n    }}\n" for m in MAPS)
         + "    # Region = tunel de entrada (tun-hp* VIP Helsinki, tun-fp* VIP Falkenstein).\n"
           "    # Las dos ultimas: la base tiene la VIP de la region pero no su bloque ->\n"
           "    # la OTRA IP del par de la VM. Si nada casa, vuelve y manda la general.\n"
           "    chain egress_pools {\n"
         + "".join(f"        {r}\n" for r in CHAIN_RULES)
         + "    }\n\n"),
    ]
    general = general_rule_re(main)
    hits = list(general.finditer(src))
    if len(hits) != 1:
        sys.exit(f"ABORTO: la regla general 10.64/16 -> {main} aparece {len(hits)} veces en {CONF}")
    if src.count("table ip nat {\n") != 1:
        sys.exit(f"ABORTO: 'table ip nat {{' aparece {src.count('table ip nat {' + chr(10))} veces")
    if src.count(ELEMENTS_INCLUDE + "\n") != 1:
        sys.exit(f"ABORTO: no encuentro una vez `{ELEMENTS_INCLUDE}`")
    out = src.replace(edits[0][0], edits[0][1], 1)
    line = general.search(out).group(0)
    indent = re.match(r"^([ \t]*)", line).group(1)
    out = out.replace(line + "\n",
                      f"{indent}{MARK}\n"
                      f"{indent}# Pools de salida por VM ANTES de la general, que queda de red de seguridad.\n"
                      f"{indent}{jump_rule(up)}\n" + line + "\n", 1)
    out = out.replace(ELEMENTS_INCLUDE + "\n",
                      ELEMENTS_INCLUDE + "\n"
                      f"{MARK}\n"
                      "# Elementos de los pools de salida (sync-base-nat.py). Con comodin a\n"
                      "# proposito: si el fichero falta, la carga NO falla.\n"
                      f"{EGRESS_INCLUDE}\n", 1)
    return out


def file_rollback(src: str) -> str:
    pat = re.compile(r"^[ \t]*" + re.escape(MARK) + r"\n[ \t]*# Pools de salida por VM ANTES.*\n[ \t]*ip saddr 10\.64\.0\.0/16 iifname \"tun-\*\" oifname \"[^\"]+\" jump egress_pools\n", re.M)
    return pat.sub("", src)


def validate(new: str) -> str:
    """Escribe el fichero candidato a un temporal y lo pasa por `nft -c`."""
    fd, path = tempfile.mkstemp(prefix="nftables.", suffix=".conf")
    with os.fdopen(fd, "w") as fh:
        fh.write(new)
    r = sh("nft", "-c", "-f", path)
    if r.returncode != 0:
        os.unlink(path)
        sys.exit(f"ABORTO: nft -c rechaza el fichero nuevo (no se ha tocado nada):\n{r.stderr}")
    return path


def install(path: str, backup_suffix: str) -> None:
    shutil.copy2(CONF, CONF + backup_suffix)
    shutil.copyfile(path, CONF)
    os.unlink(path)
    print(f"  fichero: actualizado y validado (copia en {CONF}{backup_suffix})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    main = main_ipv4()
    src = open(CONF).read()

    if args.rollback:
        new = file_rollback(src)
        path = validate(new) if new != src else None
        if args.apply:
            rollback_live()
            if path:
                install(path, ".pre-egress-pools-rollback")
        print("  fichero: " + ("sin salto que quitar" if path is None else
                               "salto retirado" if args.apply else f"valida (simulacro, {path})"))
        return

    tx, up = live_plan(main)
    print(f"base MAIN_IPV4={main} subida={up}")
    for line in tx:
        print(f"  nft> {line}")
    new = file_edit(src, main, up)
    # Orden: 1) validar el fichero SIN escribirlo, 2) en vivo (atomico),
    # 3) escribir el fichero. Si algo falla, lo que queda es inerte.
    path = validate(new) if new != src else None
    if not args.apply:
        print("  fichero: " + ("ya persistido (marca presente)" if path is None
                               else f"valida con nft -c (simulacro, {path} para revisar)"))
        print("SIMULACRO: nada aplicado (--apply)")
        return
    os.makedirs(INCLUDE_DIR, exist_ok=True)
    if not os.path.exists(EGRESS_FILE):
        open(EGRESS_FILE, "w").write("# generado por sync-base-nat.py - no editar a mano\n")
    apply_live(tx)
    if path:
        install(path, ".pre-egress-pools")
    else:
        print("  fichero: ya persistido (marca presente)")
    for m in MAPS:
        n = len(re.findall(r"\d+\.\d+\.\d+\.\d+ : \d+\.\d+\.\d+\.\d+",
                           sh("nft", "list", "map", "ip", "nat", m).stdout))
        print(f"  {m}: {n} elementos (vacio hasta config/egressPools.enabled)")


if __name__ == "__main__":
    main()
