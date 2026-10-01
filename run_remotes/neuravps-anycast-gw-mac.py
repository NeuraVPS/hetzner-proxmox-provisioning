#!/usr/bin/env python3
"""PROPUESTA (01/10/2026), NO desplegada en nodos con clientes: MAC de puerta de
enlace idéntica en el `vmbr0` de todos los nodos.

Por qué: la puerta de enlace de las VMs (10.64.255.1 / fe80::1) ya es la misma IP
en toda la flota, pero su MAC es distinta en cada nodo (fijada por
neuravps-pin-bridge-mac.py). Tras una migración en vivo, Windows sigue mandando a
la MAC del nodo viejo hasta que su caché ARP caduca (~8 s medidos); migrate_vm.sh
lo tapa hoy con un ARP/NA dirigido al reanudar. Con la misma MAC en todos los
nodos el problema desaparece de raíz, también para migraciones hechas a mano.
Es seguro como dominio L2: `vmbr0` tiene `bridge-ports none` en toda la flota y
los túneles hacia las BASE son L3, así que la MAC nunca sale del nodo.

Cambiarla en un nodo vivo deja a cada VM mandando a la MAC vieja hasta que se
entera. Este script la cambia y en el mismo instante anuncia la nueva por
difusión (ARP gratuito de 10.64.255.1 y NA no solicitado de fe80::1 con
Override), tres veces en 1 s: las VMs actualizan su caché en milisegundos.

Uso:
  neuravps-anycast-gw-mac.py                 # inspecciona
  neuravps-anycast-gw-mac.py --apply [MAC]   # cambia en caliente + anuncia (no persiste)
  neuravps-anycast-gw-mac.py --announce      # solo repite el anuncio
Para persistir: sustituir `hwaddress` en /etc/network/interfaces (sin recargar),
igual que hace neuravps-pin-bridge-mac.py.
"""
from __future__ import annotations

import argparse
import re
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ANYCAST_MAC = '02:4e:56:58:00:01'   # localmente administrada, unicast ("NVX")
BRIDGE = 'vmbr0'
GW4, GW6 = '10.64.255.1', 'fe80::1'
MAC_RE = re.compile(r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$')


def ts() -> str:
    return datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]


def mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(':', ''))


def csum(b: bytes) -> int:
    if len(b) % 2:
        b += b'\0'
    s = sum(struct.unpack('!%dH' % (len(b) // 2), b))
    while s >> 16:
        s = (s & 0xffff) + (s >> 16)
    return (~s) & 0xffff


def frames(br: bytes) -> list[bytes]:
    bcast = b'\xff' * 6
    gw4 = socket.inet_aton(GW4)
    arp_req = bcast + br + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1) + br + gw4 + b'\0' * 6 + gw4
    arp_rep = bcast + br + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2) + br + gw4 + bcast + gw4
    src = socket.inet_pton(socket.AF_INET6, GW6)
    dst = socket.inet_pton(socket.AF_INET6, 'ff02::1')
    body = struct.pack('!BBHI', 136, 0, 0, 0xA0000000) + src + struct.pack('!BB', 2, 1) + br
    body = body[:2] + struct.pack('!H', csum(src + dst + struct.pack('!I3xB', len(body), 58) + body)) + body[4:]
    na = b'\x33\x33\x00\x00\x00\x01' + br + b'\x86\xdd' + struct.pack('!IHBB', 0x60000000, len(body), 58, 255) + src + dst + body
    return [arp_req, arp_rep, na]


def announce(rounds: int = 3, gap: float = 0.4) -> None:
    br = mac_bytes(Path(f'/sys/class/net/{BRIDGE}/address').read_text().strip())
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    tx.bind((BRIDGE, 0))
    for i in range(rounds):
        for f in frames(br):
            tx.send(f)
        print(ts(), 'announce', i + 1, br.hex(':'), flush=True)
        if i + 1 < rounds:
            time.sleep(gap)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', nargs='?', const=ANYCAST_MAC)
    ap.add_argument('--announce', action='store_true')
    a = ap.parse_args()
    cur = Path(f'/sys/class/net/{BRIDGE}/address').read_text().strip()
    ports = len(list(Path(f'/sys/class/net/{BRIDGE}/brif').iterdir()))
    if a.apply:
        new = a.apply.lower()
        if not MAC_RE.match(new) or int(new[:2], 16) & 1:
            raise SystemExit(f'MAC no válida o multicast: {new}')
        if new != cur:
            subprocess.run(['ip', 'link', 'set', 'dev', BRIDGE, 'address', new], check=True)
            print(ts(), 'vmbr0', cur, '->', new, f'({ports} puertos)', flush=True)
        announce()
    elif a.announce:
        announce()
    else:
        print(f'vmbr0={cur} anycast={ANYCAST_MAC} igual={cur == ANYCAST_MAC} puertos={ports}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
