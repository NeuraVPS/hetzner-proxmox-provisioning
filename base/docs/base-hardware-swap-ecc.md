# Reemplazo de las BASES por hierro con ECC — runbook

Cambio de las dos BASES (EX44 / i5-13500 / DDR4 **sin ECC**) por servidores con
**64 GB DDR5 ECC + uplink 10G**. Complementa `dual-region-cutover.md`, que documenta
el build de b0 del 2026-07-04 y cuyas trampas siguen vigentes.

**Requisito del operador:** hasta que las nuevas tomen el relevo, **todos los clientes
siguen usando el servicio con normalidad a través de las bases antiguas**. Blue-green
con 4 máquinas en vuelo; las viejas no se cancelan hasta cerrar la ventana de rollback.

---

## Lo que decide el coste para el cliente: dos preguntas a Hetzner

**1. ¿Se pueden reasignar las 4 failover IPs a los servidores nuevos?**

Es la bifurcación principal. Con las VIPs conservadas:

- Los **219 nodos no se tocan**. Sus túneles `tun-fsn`/`tun-hel` tienen `remote` = la **VIP**,
  no la IP de la base. Whoever holds the VIP gets the traffic.
- El **DNS no se toca**: `sqx-hel`, `trading-fsn`, etc. apuntan a las VIPs.
- Los **clientes no cambian nada**: mismos hostnames, mismos puertos, misma IP de salida.

Sin ellas: hay que reanclar 438 túneles en 219 nodos, migrar DNS con TTL bajo, y cambiar la
IP de salida de todos los invitados. Es otro proyecto, no este.

**2. ¿Están los servidores nuevos en la MISMA ubicación que los actuales?**
Las failover IP de Hetzner sólo enrutan dentro de su ubicación. La nueva b0 tiene que estar en
**Falkenstein** y la nueva b1 en **Helsinki**, o las VIPs no las pueden seguir aunque
administrativamente se dejen reasignar. Confirmar ANTES de aceptar la entrega.

---

## Fase 0 — Antes de que lleguen (no necesita hierro nuevo)

### 0.1 Mover la SNAT de salida IPv4 a la VIP

**Esta es la fase que hace que el cambio de hierro no le cueste nada al cliente.**

Hoy la salida a Internet de los invitados lleva SNAT **a la IP principal de la máquina**:

```
ip saddr 10.64.0.0/16 iifname "tun-*" oifname "enp6s0" snat to 188.40.153.120   # b0
ip saddr 10.65.0.0/16 iifname "tun-*" oifname "enp6s0" snat to 37.27.135.250    # b1
```

Es la última regla atada a la identidad del hierro. Mientras siga así:

- cambiar de máquina **cambia la IP pública de todos los invitados** de esa región, y rompe las
  listas blancas que los clientes tienen puestas en sus brókers;
- y cada movimiento de VIP seguirá matando todas las sesiones establecidas
  (ver `neuravps-vip-move-mata-sesiones` en la memoria).

El patrón ya existe en el repo: `snippets/canon-snat-por-vip.py` hizo exactamente esto para la
canónica IPv6 del camino de vuelta — *"la dirección canónica de SNAT pasa a ser DE LA VIP, no de
la máquina"*. Falta aplicarlo a la salida v4: elegir la dirección por el **túnel de salida**
(`oifname "tun-fp*"` → VIP FSN, `oifname "tun-hp*"` → VIP HEL) en vez de por la máquina.

Consecuencias:
- La IP de salida de un invitado pasa a ser **la VIP de su región** y ya **no cambia nunca más**:
  ni al mover una VIP, ni al cambiar el hierro, ni en un failover.
- **Cuesta un cambio de IP de salida, una sola vez**: de `37.27.135.250` / `188.40.153.120` a
  `77.42.49.79` / `94.130.3.118`. Hay que **avisar a los clientes que filtran por IP en su
  bróker** antes de aplicarlo. Es el único aviso de este tipo que habrá que dar.
- Las dos VIPs de una región pasan a moverse **siempre juntas** (la v4 es el origen del SNAT y la
  v6 el ancla del túnel: separadas, la vuelta llega a la máquina equivocada).

Aplicar en vivo sin `flush` y persistir en `/etc/nftables.conf`, igual que hace
`canon-snat-por-vip.py`. Verificar con el canario de la fase 3 antes de darlo por bueno.

### 0.2 Preparar y ensayar

- Actualizar `scripts/migrate_vm.sh` (`PEER_BASES`) y `BASE_HOSTS` para admitir **4 bases**.
- Revisar que `base_setup.sh` y los snippets de `snippets/` están al día con lo que hay
  desplegado en b0/b1 (hay divergencias históricas: ver el log de cambios de `base/README.md`).
- Reservar la ventana: **fuera de horario de mercado**. El corte del 27-08 cayó a las 17:23 CEST,
  en pleno solape Londres/Nueva York, y se llevó ~530 VPS de MetaTrader dos veces.

---

## Fase 1 — Construir en frío (servidores nuevos, cero tráfico)

Debian 13 puro, igual que el build de julio. Orden importante:

1. **Red** (`/etc/network/interfaces`): IP principal nueva + **las 4 VIPs con
   `preferred_lft 0`**, tal cual el ejemplo comentado en `base_setup.sh` §0. El
   `preferred_lft 0` es load-bearing: hace que la VIP nunca se elija como origen, y así un swing
   no necesita tocar la config.
2. **`mkdir /var/lib/base-nat`** antes del primer `systemctl restart nftables`, o la unidad muere
   con `226/NAMESPACE`.
3. **Topología jool/netns** según `docs/netns-jool-nat46-nat66-guide.md` — la EAMT mapea la IPv6
   **propia de la máquina** contra `10.0.0.3`, así que aquí sí cambia (IP nueva).
4. **nftables**: `/etc/nftables.conf` con el DNAT por la IP principal **nueva** más las 4 VIPs.
5. **`pve-proxy-map.conf` antes del primer `sync nodes`**, o falla la validación de nginx.
6. **`base_setup.sh`** (servicios de sync en runtime). Ojo `pip3 --break-system-packages
   --ignore-installed` en Debian 13.
7. **Certificado**: `neuravps-dual` por **DNS-01** contra la Cloud API de Hetzner. **No depende de
   la IP**, así que se emite en frío y sin tocar producción. Token en `/opt/letsencrypt/hetzner.env`.
8. **⛔ BOOTSTRAP: empujar el /64 de cada base nueva al `[IPSET base]` de `cluster.fw`
   DESDE UNA BASE EXISTENTE.** Una base nueva **no alcanza ningún nodo** —ni puede empujar el
   ipset ella misma— hasta que un peer lo hace. Es el gotcha que más tiempo costó en julio.
9. **`BASE_SSH_HOST_IPS` con las 4 bases** + `firebase deploy --only functions` **COMPLETO**.
   Un deploy dirigido deja a las funciones no incluidas con la lista vieja y timeouts de 30 s;
   ya mordió dos veces.
10. `sync-base-nat.py sync` y comparar contra la base viva: mapas (~1.918), rutas por VM,
    `gre_peers` (219), túneles (438), `nc_fleet_v6`.

Al terminar, la base nueva tiene **todo el estado y cero tráfico**. Sus 438 túneles existen pero
están inertes: su `local` es una VIP que aún no posee.

---

## Fase 2 — Validar sin clientes encima

⚠️ **Una base sin VIP no puede alcanzar a ningún invitado.** Los túneles se anclan a la VIP, así
que no puede originar GRE. La prueba "aislada por su IP principal" del runbook viejo **da 0/30 en
una base sana** — comprobado el 27-08. No usarla como criterio.

Dos formas, en este orden:

**(a) Túnel canario, sin exposición.** Añadir en UN nodo un tercer túnel anclado a la **IPv6
propia** de la base nueva (no a una VIP), con su ruta de transito, y ejercitar el camino completo:
DNAT → jool NAT46 → mapa por dport → túnel → nodo → invitado, y la vuelta con su SNAT. Valida
todo salvo el anclaje a VIP. Se desmonta después.

**(b) Puerta final: darle la VIP v6 de la región, no la v4.** La v6 es el ancla de los túneles; la
v4 es la puerta de entrada de los clientes. Con la v6 en la máquina nueva y la v4 aún en la
vieja, se ejercita el camino entero **por su IP principal** sin que ningún cliente entre por ella.
Criterio de paso: **30/30 en TCP, SMB y handshake X.224** (el X.224 prueba que el cliente de RDP
entra de verdad; `nc -z` sólo prueba el TCP).

Nota: con la SNAT ya movida a la VIP (fase 0.1), tomar la v6 sin la v4 deja la vuelta descolocada
→ en la fase 2(b) **mover las dos juntas** y aceptar que ahí ya hay clientes. Si se quiere validar
sin exposición ninguna, la vía es (a).

---

## Fase 3 — Cutover, una región cada vez

Ventana fuera de mercado. **Una región, pausa, la otra** — un fallo no puede tumbar las dos.

1. `config/failover_watchdog` → **`maintenance:true`** (deja `enabled`/`dryRun` como estén).
   Sin esto compite con nosotros durante la ventana.
2. **Arrancar el canario ANTES** (ver más abajo).
3. Mover **las dos VIPs de la región juntas** a la base nueva (Robot API,
   `POST /failover/{ip}` con `active_server_ip` = IP principal nueva).
   - Es **asíncrono**: timeout o 409 LOCKED = en curso. Esperar 35 s y sondear cada 20 s.
   - **Límite de 100 peticiones/hora en toda la cuenta.** Un ciclo completo gasta ~85. Usar
     `GET /failover` (una petición para las 4) en vez de 4 `GET /failover/{ip}`.
   - **Los IDs de servidor del Robot cambian**: los actuales (#2982184 b0, #2843573 b1) ya no valen.
4. **Puerta**: 30/30 TCP + SMB + X.224 por la VIP real.
5. Verificar salud contra la línea base: 0 unidades fallidas, servicios activos, mapas
   1.918/1.919, `gre_peers` 219, rutas `10.65.x` 219, `rp_filter=0` en los túneles, 4 VIPs bindeadas.
6. **Pausa.** Dejar la primera región en el hierro nuevo un rato antes de tocar la segunda.
7. Repetir con la otra región.
8. `maintenance:false` + `GET /failover` final comprobando el geo-split.

### El canario (obligatorio, y midiendo lo que toca)

- **Entrada**: TCP a `VIP:443` cada 1 s y camino RDP completo cada 5 s, desde fuera.
- **Salida**: `curl --interface <10.65.x>` **ejecutado en un nodo** — sale por el mismo túnel que
  los invitados, así que su hueco *es* el hueco de Internet de los clientes.
- ⚠️ **Y una sesión TCP LARGA abierta, vigilando cuándo se rompe.** Un canario que reabre
  conexión en cada tick no ve nada: el 27-08 dio *0 fallos en 6.011 sondeos* mientras ~530 VPS de
  MetaTrader perdían su bróker. Una conexión nueva siempre funciona; lo que muere es lo establecido.

### Qué le cuesta al cliente

Con las VIPs conservadas y la fase 0.1 hecha: **un corte de sesiones por región** en el momento
del swing (el conntrack no se comparte entre máquinas; es inevitable) y **ningún cambio de IP ni
de hostname**. RDP y MetaTrader reconectan solos; los terminales de MT que no reintentan hay que
empujarlos (`File → Login to Trading Account`).

---

## Fase 4 — Repuntar operación y retirar

1. Mover de la b0 vieja: `neuravps-defrag.py`, `defrag-relief.timer`, `migrate_vm.sh` +
   `migrate_vms_batch.sh`, logs de `/var/log/migrate_vm`, y el papel de salto SSH a la flota.
2. `PEER_BASES` en `migrate_vm.sh` (repo + ambas bases) y `BASE_HOSTS` → sólo las dos nuevas.
3. `BASE_SSH_HOST_IPS` → las dos nuevas + **`firebase deploy --only functions` COMPLETO**.
4. Quitar los /64 viejos del `[IPSET base]` y empujar a la flota.
5. DNS: `b0.neuravps.com` / `b1.neuravps.com` → IPs nuevas. Alias `b0`/`b1` del
   `~/.ssh/config` del sandbox y limpiar `known_hosts`.
6. **Ventana de rollback**: dejar las viejas encendidas y con su estado unos días. Vuelta atrás =
   un `POST` de failover por VIP, nada más.
7. Sólo entonces, cancelar en Hetzner.

---

## Rollback

En cualquier punto hasta la fase 4: **la base vieja conserva todo el estado** (mapas, rutas,
túneles) y sigue bindeando las 4 VIPs. Volver atrás es mover las VIPs de vuelta con el Robot API
y verificar la puerta. Coste: otro corte de sesiones de esa región.

## Si una VIP nueva no enruta

Una failover recién **asignada** puede no enrutar hasta que hay un **switch real**: la asignación
inicial no dispara la propagación. Síntoma: 0 paquetes en `tcpdump` pese a estar bindeada y el
Robot decir que está asignada. **Arreglo = kick**: POST a la otra base, esperar a que aplique,
POST de vuelta. Verificado en fsn-v6 el 2026-07-04.

## Dimensionado (medido 2026-08-30)

CPU y RAM sobran en cualquiera de los dos hierros: la base ocupada usa **0,3 hilos de 20** y
**1 GB de 62**, con el disco al 2%. **Lo único a dimensionar es la red**: b1 mueve **196 Mb/s de
media** (72 h, 44.446 paq/s) y en una ventana de mantenimiento **una sola base carga con las dos
regiones**. De ahí el 10G. Detalle en la memoria `neuravps-perfil-de-carga-de-las-bases`.
