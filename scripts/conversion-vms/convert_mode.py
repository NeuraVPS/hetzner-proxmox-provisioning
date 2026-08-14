#!/usr/bin/env python3
"""Modo `convert` de vmtool: pasa un invitado al direccionamiento del modelo
nuevo, replicando EXACTAMENTE el estado que la vm 1096 tiene validado con
arranques en frio.

Orden dentro del invitado: primero se ANADE lo nuevo, luego se quita lo viejo.
Nunca hay un instante sin direccion. Y aunque la red quedase mal, el agente
sigue respondiendo (va por virtio-serial), asi que siempre hay marcha atras.

NO toca Firestore: eso se hace fuera, despues de medir que el invitado esta bien.
"""
PLANTILLA = r'''
$ErrorActionPreference = "Continue"
$if = "{ALIAS}"

# --- IPv6: anadir lo nuevo ANTES de quitar lo viejo -------------------------
# /128 OBLIGATORIO. Con /64 el invitado cree que las direcciones de
# TRANSITO de las bases estan en su mismo enlace — viven dentro del /64 de
# identidad — y las busca por NDP en vez de mandarlas a fe80::1. Nadie le
# contesta y la respuesta nunca sale: la salida funciona y la ENTRADA no.
netsh interface ipv6 add address $if {NEW6}/128 store=persistent | Out-Null
netsh interface ipv6 add route ::/0 $if {GW6} store=persistent | Out-Null
{DEL6}

# --- IPv4: `set address ... static` desactiva el DHCP el solo ---------------
netsh interface ipv4 set address   name=$if static {NEW4} 255.255.0.0 {GW4} | Out-Null
netsh interface ipv4 set dnsserver name=$if static 185.12.64.1 primary validate=no | Out-Null
netsh interface ipv4 add dnsserver name=$if 185.12.64.2 index=2 validate=no | Out-Null

Start-Sleep -Seconds 3

# --- MEDIR. Un exitcode 0 no prueba nada (regla de oro de la vm 1023) -------
$i  = Get-NetAdapter -InterfaceAlias $if
$a6 = (Get-NetIPAddress -AddressFamily IPv6 -InterfaceIndex $i.ifIndex |
       Where-Object {{$_.IPAddress -notlike "fe80*"}} | ForEach-Object {{$_.IPAddress}}) -join ","
$a4 = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.IPAddress}}) -join ","
$g6 = (Get-NetRoute -DestinationPrefix "::/0" -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.NextHop}}) -join ","
$g4 = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.NextHop}}) -join ","
$dh = (Get-NetIPInterface -InterfaceIndex $i.ifIndex |
       ForEach-Object {{$_.AddressFamily.ToString()+"="+$_.Dhcp.ToString()}}) -join ","
$s4 = (curl.exe -4 -s -m 15 https://api.ipify.org)
$s6 = (curl.exe -6 -s -m 15 https://ifconfig.me)
$dn = try {{ (Resolve-DnsName -Name api.ipify.org -Type A -ErrorAction Stop | Select-Object -First 1).IPAddress }} catch {{ "FALLO" }}
"BOUND:v6=$a6 v4=$a4 gw6=$g6 gw4=$g4 dhcp=$dh salida4=$s4 salida6=$s6 dns=$dn"
'''


def script(alias: str, vmid: int, viejo6: str, viejo_gw6: str,
           ident: str = "2a01:4f9:c01f:e") -> str:
    nuevo6 = f"{ident}::{vmid:x}"
    nuevo4 = f"10.64.{vmid // 256}.{vmid % 256}"
    partes = []
    if viejo_gw6:
        # La ruta vieja primero: si se fuera la direccion antes que su ruta,
        # el invitado se queda sin salida durante el hueco.
        partes.append(f'netsh interface ipv6 delete route ::/0 $if {viejo_gw6} store=persistent | Out-Null')
    if viejo6:
        # Los DOS almacenes: `store=persistent` a secas NO retira la direccion
        # viva, y eso deja un estado partido que se apaga en el proximo arranque.
        partes.append(f'netsh interface ipv6 delete address $if {viejo6} store=persistent | Out-Null')
        partes.append(f'netsh interface ipv6 delete address $if {viejo6} store=active | Out-Null')
    return PLANTILLA.format(ALIAS=alias, NEW6=nuevo6, NEW4=nuevo4,
                            GW6="fe80::1", GW4="10.64.255.1",
                            DEL6="\n".join(partes))


def objetivos(vmid: int, ident: str = "2a01:4f9:c01f:e"):
    return f"{ident}::{vmid:x}", f"10.64.{vmid // 256}.{vmid % 256}"
