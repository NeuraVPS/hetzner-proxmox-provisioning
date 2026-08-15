#!/usr/bin/env python3
"""Revive el agente qemu de una VM ENTRANDO POR SSH. Corre EN UNA BASE.

Estas VMs estan sanas: encendidas, en el NAT y respondiendo por sus puertos
publicos. Lo unico mudo es `guest-exec` del agente, que es justo el canal que la
conversion necesita (por SSH no se puede convertir: al cambiar la IP se cortaria
la propia sesion).

Asi que se usa SSH SOLO para reiniciar el servicio del agente. Despues, la
conversion vuelve a poder ir por su canal de siempre.

Uso: revive_agente.py <vmid> [...]
"""
import subprocess
import sys

sys.path.insert(0, "/root")
import vmtool  # noqa: E402

PS = ('$s = Get-Service -Name "QEMU-GA","qemu-ga","QEMU Guest Agent" '
      '-ErrorAction SilentlyContinue | Select-Object -First 1; '
      'if ($s) { Restart-Service -InputObject $s -Force -ErrorAction SilentlyContinue; '
      'Start-Sleep -Seconds 3; "AGENTE:" + $s.Name + "=" + '
      '(Get-Service -Name $s.Name).Status } else { "AGENTE:no-encontrado" }')

for v in [int(x) for x in sys.argv[1:]]:
    _i, s = vmtool.doc_de(v)
    if not s:
        print(f"  vm {v}: no esta en Firestore"); continue
    ok, out = vmtool.por_ssh(s.get("ipv6"), s.get("serverUser"), s.get("serverPassword"), PS)
    if not ok:
        print(f"  vm {v}: ❌ tampoco entra por SSH ({out[:70]})"); continue
    # ¿Vuelve a ejecutar el agente?
    ok2, out2 = vmtool.por_agente(
        vmtool.nodo_ip(s.get("nodeId") or ""), v, '"VIVO"')
    print(f"  vm {v}: {out.strip()[:40]}  ->  agente {'✅ VIVO' if ok2 else '❌ sigue mudo'}")
