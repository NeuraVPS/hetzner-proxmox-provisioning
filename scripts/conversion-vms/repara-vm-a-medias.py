#!/usr/bin/env python3
"""Repara una VM que quedo A MEDIAS entre los dos modelos. Corre EN UNA BASE.

Sintoma: tiene la IPv6 NUEVA pero con /64, y la IPv4 y las puertas VIEJAS.
Firestore y las rutas de las bases ya apuntan al modelo nuevo, asi que la VM
queda incomunicada por fuera aunque por dentro parezca viva.

Lee el estado real del invitado y aplica lo que falte, sin suponer nada.

Uso: repara.py <vmid> [...]
"""
import sys

sys.path.insert(0, "/root")
import vmtool  # noqa: E402

PLANTILLA = r'''
$ErrorActionPreference = "Continue"
$if = "{ALIAS}"

# IPv6 con el prefijo CORRECTO: /128. Se retira primero la copia con /64 --de
# los DOS almacenes, porque `store=persistent` a secas no quita la viva.
netsh interface ipv6 delete address $if {V6} store=persistent | Out-Null
netsh interface ipv6 delete address $if {V6} store=active | Out-Null
netsh interface ipv6 add address $if {V6}/128 store=persistent | Out-Null
netsh interface ipv6 add route ::/0 $if fe80::1 store=persistent | Out-Null
{DELGW}

# IPv4 estatica del modelo nuevo (`set address ... static` desactiva el DHCP).
netsh interface ipv4 set address   name=$if static {V4} 255.255.0.0 10.64.255.1 | Out-Null
netsh interface ipv4 set dnsserver name=$if static 185.12.64.1 primary validate=no | Out-Null
netsh interface ipv4 add dnsserver name=$if 185.12.64.2 index=2 validate=no | Out-Null

Start-Sleep -Seconds 3
$i  = Get-NetAdapter -InterfaceAlias $if
$a6 = (Get-NetIPAddress -AddressFamily IPv6 -InterfaceIndex $i.ifIndex |
       Where-Object {{$_.IPAddress -notlike "fe80*"}} |
       ForEach-Object {{$_.IPAddress+"/"+$_.PrefixLength}}) -join ","
$a4 = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.IPAddress}}) -join ","
$g6 = (Get-NetRoute -DestinationPrefix "::/0" -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.NextHop}}) -join ","
$g4 = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.NextHop}}) -join ","
"FIX:v6=$a6 v4=$a4 gw6=$g6 gw4=$g4"
'''

for v in [int(x) for x in sys.argv[1:]]:
    _i, s = vmtool.doc_de(v)
    nip = vmtool.nodo_ip((s or {}).get("nodeId") or "")
    ok, est = vmtool.por_agente(nip, v, vmtool.PS_ESTADO)
    if not ok:
        print(f"  vm {v}: el agente no responde ({est[:60]}) -> MANUAL")
        continue
    c = dict(kv.split("=", 1) for kv in est.split() if "=" in kv)
    alias = c.get("alias") or "Ethernet"
    gw6_viejo = [g for g in (c.get("gw6") or "").split(",") if g and g != "fe80::1"]
    v6 = f"2a01:4f9:c01f:e::{v:x}"
    v4 = f"10.64.{v // 256}.{v % 256}"
    delgw = "\n".join(
        f'netsh interface ipv6 delete route ::/0 $if {g} store=persistent | Out-Null'
        for g in gw6_viejo)
    ps = PLANTILLA.format(ALIAS=alias, V6=v6, V4=v4, DELGW=delgw)
    ok2, out = vmtool.por_agente(nip, v, ps, t=90)
    print(f"  vm {v}: [{'ok' if ok2 else 'FALLO'}] {out}")
