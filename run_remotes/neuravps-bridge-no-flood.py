#!/usr/bin/env python3
"""Corta la inundación de unicast desconocido hacia los puertos de VM de vmbr0.

Por qué (laboratorio del 01/10/2026): cuando una VM se va de un nodo, el
tráfico que aún le llega (las BASE tardan unos segundos en mover su ruta) sale
del nodo hacia su MAC, que el puente ya no conoce, y Linux la INUNDA a todos
los `tap` de `vmbr0`: 16-34 VMs ajenas recibieron así el tráfico de otra
durante 7-20 s en cada migración medida. Un invitado en modo promiscuo lo vería.

Cómo: el mecanismo nativo de Proxmox. Con `bridge-disable-mac-learning 1` en la
estrofa de `vmbr0`, `PVE::Network::tap_plug` deja cada `tap` con
`learning 0` y `unicast_flood 0` y le añade una entrada FDB ESTÁTICA con la MAC
de la VM. Así la entrega a cada VM nunca depende de inundar, y una MAC que ya no
está en el nodo no sale por ningún `tap`. Difusión y multidifusión (ARP, ND)
siguen igual: `bcast_flood` y `mcast_flood` no se tocan.

Proxmox lee esa línea del FICHERO en cada `tap_plug` (arranque, migración
entrante, cambio de net0), así que basta escribirla: no se recarga la red.
Los `tap` que ya existen no cambian hasta que se replanten; `--convert-running`
los convierte en caliente sin corte (entrada estática PRIMERO, después los
flags), y `--revert-running` deshace eso.

Uso:
  neuravps-bridge-no-flood.py                    # solo inspecciona
  neuravps-bridge-no-flood.py --config           # escribe la línea (sin recargar)
  neuravps-bridge-no-flood.py --convert-running  # convierte los tap existentes
  neuravps-bridge-no-flood.py --revert-running   # los devuelve a learning/flood on
  neuravps-bridge-no-flood.py --unconfig         # quita la línea
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

INTERFACES = Path('/etc/network/interfaces')
BACKUP = Path('/etc/network/interfaces.before-neuravps-no-flood')
BRIDGE = 'vmbr0'
KEY = 'bridge-disable-mac-learning'
TAP_RE = re.compile(r'^tap(\d+)i(\d+)$')
MAC_RE = re.compile(r'(?:^|,)(?:virtio|e1000|e1000e|rtl8139|vmxnet3)=([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})')


def configured_text(text: str, enable: bool = True) -> str:
    """Pone o quita `bridge-disable-mac-learning 1` en `iface vmbr0 inet static`.

    Idempotente; no toca nada fuera de esa línea."""
    lines = text.splitlines(keepends=True)
    head = next((i for i, l in enumerate(lines) if l.split()[:4] == ['iface', BRIDGE, 'inet', 'static']), None)
    if head is None:
        raise ValueError(f'no encuentro la estrofa "iface {BRIDGE} inet static"')
    end = next((i for i in range(head + 1, len(lines)) if lines[i].strip() and not lines[i][:1].isspace()), len(lines))
    body = [l for l in lines[head + 1:end] if l.split()[:1] != [KEY]]
    if enable:
        body.insert(0, f'    {KEY} 1\n')
    if not lines[head].endswith('\n'):
        lines[head] += '\n'
    return ''.join(lines[:head + 1] + body + lines[end:])


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def brport(tap: str, name: str) -> str:
    return Path(f'/sys/class/net/{tap}/brport/{name}').read_text().strip()


def set_brport(tap: str, name: str, value: str) -> None:
    Path(f'/sys/class/net/{tap}/brport/{name}').write_text(value)


def vm_mac(vmid: str, idx: str) -> str | None:
    conf = Path(f'/etc/pve/qemu-server/{vmid}.conf')
    if not conf.exists():
        return None
    for line in conf.read_text().splitlines():
        if line.startswith('['):
            break  # snapshots / pending: solo cuenta la config viva
        if line.startswith(f'net{idx}:'):
            m = MAC_RE.search(line.split(':', 1)[1].strip())
            return m.group(1).lower() if m else None
    return None


def static_entries(tap: str) -> set[str]:
    out = run(['bridge', 'fdb', 'show', 'dev', tap])
    return {l.split()[0].lower() for l in out.splitlines() if ' static' in l and 'master' in l}


def taps() -> list[str]:
    return sorted(p.name for p in Path(f'/sys/class/net/{BRIDGE}/brif').iterdir() if TAP_RE.match(p.name))


def inspect() -> dict:
    conf = KEY in INTERFACES.read_text()
    rows = []
    for tap in taps():
        vmid, idx = TAP_RE.match(tap).groups()
        mac = vm_mac(vmid, idx)
        rows.append({'tap': tap, 'mac': mac, 'learning': brport(tap, 'learning'),
                     'unicast_flood': brport(tap, 'unicast_flood'),
                     'static': bool(mac and mac in static_entries(tap))})
    return {'configured': conf, 'taps': rows,
            'converted': sum(1 for r in rows if r['static'] and r['unicast_flood'] == '0'),
            'total': len(rows)}


def convert(tap: str) -> str:
    vmid, idx = TAP_RE.match(tap).groups()
    mac = vm_mac(vmid, idx)
    if not mac:
        return f'{tap}: SALTADO (sin MAC en la config)'
    run(['bridge', 'fdb', 'replace', mac, 'dev', tap, 'master', 'static'])
    if mac not in static_entries(tap):
        return f'{tap}: SALTADO (la entrada estática no aparece)'
    set_brport(tap, 'learning', '0')
    set_brport(tap, 'unicast_flood', '0')
    return f'{tap}: {mac} estática, learning 0, unicast_flood 0'


def revert(tap: str) -> str:
    set_brport(tap, 'learning', '1')
    set_brport(tap, 'unicast_flood', '1')
    return f'{tap}: learning 1, unicast_flood 1 (la entrada estática se queda; inocua)'


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--config', action='store_true')
    g.add_argument('--unconfig', action='store_true')
    g.add_argument('--convert-running', action='store_true')
    g.add_argument('--revert-running', action='store_true')
    a = ap.parse_args()
    if a.config or a.unconfig:
        if INTERFACES.is_symlink():
            raise SystemExit('interfaces es un enlace simbólico: no lo toco')
        text = INTERFACES.read_text()
        new = configured_text(text, enable=a.config)
        if new != text:
            if not BACKUP.exists():
                shutil.copy2(INTERFACES, BACKUP)
            tmp = INTERFACES.with_suffix('.neuravps-tmp')
            tmp.write_text(new)
            tmp.replace(INTERFACES)
        print(json.dumps({'changed': new != text, 'configured': a.config}))
        return 0
    if a.convert_running or a.revert_running:
        for tap in taps():
            print(convert(tap) if a.convert_running else revert(tap))
    print(json.dumps(inspect(), indent=None))
    return 0


if __name__ == '__main__':
    sys.exit(main())
