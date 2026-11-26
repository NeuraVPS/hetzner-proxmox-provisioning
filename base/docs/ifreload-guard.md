# Guarda de red del nodo frente a `ifreload` (nvx-netguard)

Desde el 26/11/2026. Ficheros (fuente única: heredocs de `install.sh`):

| Fichero | Qué es |
|---|---|
| `/usr/local/sbin/nvx-netguard.sh` | La guarda: asegura, comprueba y repara. `--check` solo mira. |
| `/etc/network/if-up.d/nvx-netguard` | Hook de ifupdown2: llama a la guarda (`--hook`) tras cada `up` de cada interfaz. |
| `/etc/systemd/system/neuravps-tunnels.service.d/nvx-netguard.conf` | `ExecStartPost=-` de la guarda: en cada arranque, tras los túneles. |

Despliegue y marcha atrás: `scripts/despliega-netguard.py` (extrae los tres de `install.sh`).
Nodos nuevos: `install.sh`. Nodos externos: `scripts/adoptar-nodo-externo.sh` (fase `tunnels`).

## Qué se perdía (medido el 26/11 en 0000038, vacío)

`ifreload -a`, el «Apply Configuration» de la red en la GUI de PVE (`pvesh set /nodes/<n>/network`) y
`systemctl reload networking` hacen que ifupdown2 **purgue de `lo` toda dirección global que no esté en
`/etc/network/interfaces`**. `neuravps-tunnels.sh` pone ahí la /32 propia del nodo (`10.65.<id/256>.<id%256>`).
Al desaparecer, el kernel borra las rutas cuyo `src` era esa dirección:

- se va la **ruta por defecto IPv4 del HOST** (`default dev tun-* src 10.65.x.y`, tabla main, en los nodos
  con `DEFAULT_V4_VIA_TUNNEL=1`, que son 191 de 200): el nodo se queda **sin salida IPv4** (apt, Firestore,
  GitHub, hooks, Storage Box por v4) hasta relanzar `neuravps-tunnels.sh`. `neuravps-tunnel-probe` no lo
  arregla: `neuravps-tunnel-select.sh` falla al reponer la ruta (`src` inexistente);
- la regla 102 queda apuntando a una dirección que ya no existe;
- el `post-up` de `vmbr0` se vuelve a ejecutar, así que la regla raw `-i fwbr+ -j CT --zone 1` se **duplica**
  en cada recarga (encontrados 5 nodos con 2 copias y Kademar con 9: rastro de recargas anteriores).

No se pierde (comprobado): túneles `tun-*` (ifupdown2 no los conoce), reglas 100/101, rutas de las tablas
100/101, el IDENT6 por `vmbr0`, nftables (`nvx-mss`, `nvx-aisla*`, `nvxnode64`; hash del ruleset idéntico),
sysctls. En los nodos Hetzner `vmbr0` tiene dos estrofas (`inet` + `inet6`) y ifupdown2 **no purga** una
interfaz con varias estrofas, así que `10.64.255.1/16` y `fe80::1/64` (post-up) se quedan. En Kademar
(0000266) `vmbr0` tiene una sola estrofa: la `10.64.255.1/16` se purga y el post-up la vuelve a poner en la
misma recarga (ver medidas abajo).

## El arreglo y por qué así

1. **La /32 propia vive también en `nvx-src`**, una interfaz `dummy` que ifupdown2 no conoce y por tanto
   nunca toca. Con una copia viva, que ifupdown2 quite la de `lo` no arrastra ninguna ruta (el kernel solo
   borra rutas con ese `src` cuando la dirección deja de ser local): **no hay ni un instante sin salida**.
   La de `lo` la sigue poniendo `neuravps-tunnels.sh`; es prescindible y no se toca.
2. **Hook de ifupdown2** (`if-up.d`, `addon_scripts_support=1` en los 200 nodos): tras cada `up`, si
   `neuravps-tunnels.service` ya está activo (en el arranque no hace nada), comprueba túneles, reglas
   100/101/102, rutas por defecto de 100/101/102 y main, IDENT6 por `vmbr0` y pasarelas de invitados en
   `vmbr0`; si falta algo, repone las pasarelas que declara el fichero y **relanza `neuravps-tunnels.sh`**
   (la recuperación de siempre, idempotente). Deja una sola copia de la regla raw. Es la red de seguridad
   para lo que no previene la dummy (p. ej. un `ifdown/ifup vmbr0`, que borra la ruta IDENT6).
3. **Drop-in `ExecStartPost=-`** en `neuravps-tunnels.service`: la dummy se recrea en cada arranque.

Descartado:
- *Declarar la /32 en `/etc/network/interfaces`*: la GUI de PVE reescribe el fichero y no garantiza conservar
  direcciones extra de `lo`; habría que editar el fichero vivo de 200 nodos.
- *`address-purge no`*: solo se lee de la estrofa (no de las políticas de `policy.d`), mismo problema.
- *Solo un hook que relance los túneles*: deja una ventana sin salida en cada recarga y el relanzamiento borra
  y vuelve a crear las reglas 100/101/102 (microcortes de los invitados); con la dummy el hook casi nunca
  tiene que actuar.
- *Sustituir `neuravps-tunnels.sh` en la flota*: hay 4 variantes vivas (174/20/4/2 nodos); no hace falta.

La guarda **nunca** ejecuta `ifreload`/`ifup`/`ifdown`, no toca `/etc/network/interfaces` ni recrea `vmbr0`.
Si relanza los túneles y el sondeo había pasado la salida a la otra región, vuelve a la de casa y el sondeo
la reapunta en ≤15 s × n, igual que tras una recuperación manual.

## Operación

```bash
nvx-netguard.sh --check            # NVXSRC LOCAL4 TUNS RULES DEFS DEFMAIN IDENT6 GW4 GW6 =si|no, CT, HOOK, DROPIN
journalctl -t nvx-netguard         # qué ha reparado y por qué
scripts/despliega-netguard.py --solo-mirar                     # toda la flota, desde el sandbox
scripts/despliega-netguard.py --nodos 0000038-AX162-R          # instalar en uno
scripts/despliega-netguard.py --nodos ... --quitar             # marcha atrás
```

Marcha atrás a mano en un nodo: borrar hook y drop-in, `systemctl daemon-reload`, y `ip link del nvx-src`
**solo si** la /32 sigue en `lo` (si no, primero `neuravps-tunnels.sh`).

## Pruebas reales (26/11/2026)

Ver el informe de la sesión en `memory/neuravps-ifreload-guard-2026-11-26.md` (fuera de git).
