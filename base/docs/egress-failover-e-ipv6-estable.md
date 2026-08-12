# Salida por failover IPs + IPv6 estable por VM

**Estado: DISEÑADO, NO EJECUTADO.** Documento de trabajo para la sesión de
prueba (operador + asistente). Todas las cifras de este documento están
**medidas** el 2026-08-10/11 contra producción, no estimadas — para no volver a
derivarlas mañana. Nada de lo aquí descrito se ha aplicado.

**Revisión 2026-08-13** (sesión operador + asistente). El diseño se ha
**simplificado** respecto a la versión del 08-11. Lo que cambió:

- **La salida IPv4 tenía un hueco sin resolver** y ahora está cerrado: los 235
  nodos comparten el mismo `10.0.0.0/16`, así que "SNAT en la base" no era
  implementable tal cual. Solución: **una IPv4 privada por VM, única en toda la
  flota, que viaja con la VM** (§4.4). Espejo exacto del direccionamiento IPv6.
- **Descartado NAT46 en el nodo (464XLAT)**: el kernel de PVE no admite Jool
  (§4.4bis). Al caerse, se cae también el Jool con estado en la base — y §3.2
  vuelve a ser válida tal como está escrita.
- **Túnel: dos por nodo, a la IP principal de cada base**, no a la VIP; la base
  deduce la IPv4 de salida de por qué túnel entra el paquete (§4.5).
- **`preferred_lft 0` de las VIPs se queda como está** — es una protección, no
  una limitación (§4.5).
- Corregido: `snat/dnat prefix to` **no es sin estado** (§4.2).
- Nodos de prueba elegidos y plan de fase 0 reescrito (§7).

---

## 1. Qué resuelve

| Problema hoy | Efecto |
|---|---|
| La IPv4 de salida del cliente es la del **nodo** | Cambia en cada migración. Caso real (marcolaralba, vm215): **7 IPs distintas en 9 meses**, una de ellas en otro país. Inaceptable para prop firms. |
| La IPv6 del invitado se deriva del /64 del **nodo** (`expected = <prefijo_destino><vmid>`) | Hay que reconfigurarla **dentro de Windows** en cada migración, vía guest agent. Cuando el agente está ocupado, la migración se completa pero deja al cliente inalcanzable → flag `MIGRATION_DEGRADED` en `migrate_vm.sh`. Es la causa principal de migraciones fallidas. |
| No hay forma de dar IP fija ni dedicada | No se puede vender el add-on de fase 1. |

**El objetivo NO es ahorrar dinero.** Sale aproximadamente a coste cero o algo
barato; se hace por producto (IP estable), por fiabilidad (matar el
`MIGRATION_DEGRADED`) y para habilitar el add-on.

---

## 2. Lo que ya existe (verificado, no hay que construirlo)

- **4 failover VIPs de Hetzner**, ya en producción:
  - HEL: `77.42.49.79` + `2a01:4f9:fff1:5f::`
  - FSN: `94.130.3.118` + `2a01:4f8:fff2:95::`
- **Las dos bases tienen las cuatro enlazadas** (`preferred_lft 0`), así que
  una conmutación es puramente del lado de Hetzner.
- **Conmutación automatizada**: cliente Robot API (`GET/POST /failover/{vip}`)
  en `functions/failover_watchdog.py`, y conmutación manual rutinaria en los
  drenajes de mantenimiento.
- **Jool NAT46/NAT66** corriendo en las bases (netns `jool`), más nftables con
  flowtable offload y uplinks de **10 Gbit**.
- **Ambas bases tienen el mapa NAT completo e idéntico**: 1865 entradas cada
  una (**1343 HEL + 522 FSN**) — comprobado el 2026-08-11.
- **Alcance cruzado verificado**: b0 (Falkenstein) entra por SSH en un nodo de
  Helsinki y b1 en uno de Falkenstein.
- **Entrada cruzada verificada**: el puerto `20959` (VM de Helsinki) responde
  por las **dos** VIPs — `sqx-hel` 25,8 ms y `sqx-fsn` 26,6 ms. Es decir, la
  URL del cliente **nunca se rompe** al migrar; sólo deja de ser la óptima.
- **NPTv6 disponible en los nodos**: `nftables v1.1.3`, kernel `7.0.14-4-pve`,
  y `ip6 saddr <p>::/64 snat prefix to <q>::/64` **acepta** (probado y
  revertido en 0000142). Es traducción de prefijo 1:1 **sin estado**: sin
  conntrack, sin estado que reconstruir tras una migración.

---

## 3. Capacidad y límites (medido)

### 3.1 Tráfico y tablas

| | medido | máximo | uso |
|---|---|---|---|
| Tráfico a NATear (b1, 1336 VMs) | **0,42 Gbit/s** | 10 Gbit/s | **4,2 %** |
| Tráfico a NATear (b0, 507 VMs) | 0,16 Gbit/s | 10 Gbit/s | 1,6 % |
| conntrack b1 | **3.199** | 2.097.152 | **0,15 %** (flota entera ≈ 28k = 1,3 %) |
| Conexiones nuevas por VM | **0,06–0,13 /s** | — | — |

Consumo por VM: **3,2 GB/día** de media (177 VMs, 6 nodos). Por tipo:
**SQX ≈ 7,6 GB/VM/día vs MT ≈ 2,0** (4×).

### 3.2 El único techo duro: puertos de origen POR DESTINO

Con SNAT a una IPv4, la tupla única es (IP orig, puerto orig, IP dest, puerto
dest). Como la IP de origen es fija, sólo varía el puerto:

```
ip_local_port_range = 10240-65535        -> 55.296 puertos
nf_conntrack_tcp_timeout_time_wait = 120 s
techo = 55.296 / 120 = ~460 conexiones nuevas/s CONTRA UN MISMO IP:puerto
```

**Es por destino, no global.** Contra 50 destinos distintos son 50 × 460.

- **MT no es problema**: conexiones largas (~2 establecidas por terminal), no
  reciclan. El techo teórico son ~4.600 VMs todas contra el mismo destino.
- **El único patrón de riesgo es SQX descargando** (una conexión HTTPS por
  fichero horario, contra sólo 2 backends de Dukascopy). Con el pico real del
  operador (~100 descargando a la vez; el 90 % del tiempo sólo *usan* los
  datos ya descargados), la rotura llegaría a **4,6 conn/s por VM
  descargando** — dentro del rango plausible.
- **Arreglo**: `nf_conntrack_tcp_timeout_time_wait` **120 → 30 s** eleva el
  techo a **1.840 conn/s** (rotura a 18,4 conn/s/VM). Cierra el tema.
  ⚠️ **Este parámetro NO mata conexiones vivas**: sólo afecta al estado
  TIME_WAIT, o sea a conexiones **ya cerradas** cuya tupla se retiene. Las
  establecidas tienen su propio temporizador (`432000 s`).

Medición 2026-08-10 23:20 CEST: de **211 VMs SQX sólo 1** tenía conexión a
Dukascopy, con **0 nuevas en 20 s**. Es el momento más muerto de la semana, así
que **no mide el pico** — pendiente repetir en hora punta.

### 3.3 Modo de fallo si se agotan los puertos

**El cliente NO pierde Internet.** Netfilter descarta el paquete → el invitado
ve un *timeout*. Y:

- Sólo fallan **conexiones nuevas al destino saturado**. Navegación, Windows
  Update, brókers, MQL5 → intactos, otro espacio de puertos.
- **Las conexiones establecidas no se tocan.** Los MetaTrader conectados
  siguen operando sin enterarse.

⚠️ **El peligro es de diagnóstico, no de servicio**: el síntoma sería *"a
todos los SQX les ha dejado de funcionar la descarga de Dukascopy a la vez"*,
**indistinguible del throttling de Dukascopy** que ya explicamos a los
clientes. Nos lo comeríamos como "cosas de Dukascopy".

**Discriminador**: `conntrack -S` expone **`insert_failed`**. Si sube, somos
nosotros; si está plano y las descargas van lentas, es Dukascopy.
**Hay que cablear ese contador a métricas ANTES de consolidar.**

**Nota**: Dukascopy **no tiene IPv6 real** (`datafeed.dukascopy.com` →
`::ffff:194.8.15.180`), así que el único tráfico capaz de provocar el problema
está obligado a ir por IPv4. La vía de escape IPv6 no cubre este caso.

---

## 4. Diseño

### 4.1 Direccionamiento

Tres prefijos IPv6, más una identidad IPv4:

| rol | dirección | quién lo ve |
|---|---|---|
| **Identidad v6** (dentro de Windows) | `<IDENT>::<vmid>` — un /64 **global nuestro que no anunciamos** | sólo el invitado y su nodo |
| **Identidad v4** (dentro de Windows) | una IPv4 de `10.0.0.0/8` **única en la flota**, fija por VM (§4.4) | sólo el invitado, su nodo y la base |
| **Público HEL** | `2a01:4f9:fff1:5f::<vmid>` | Internet |
| **Público FSN** | `2a01:4f8:fff2:95::<vmid>` | Internet |

El sufijo es **siempre el `vmid`**, en las tres IPv6. Eso ya es la convención
actual (`expected = f"{short_prefix64_str(dst_ipv6)}{vmid:x}"`), sólo cambia de
dónde sale el prefijo. La identidad IPv4 sigue el mismo principio — fija,
puesta una vez, viaja con la VM — aunque no comparta el esquema de sufijo.

⚠️ **NO usar `fd00::`/ULA para la identidad.** Windows ordena por la tabla de
políticas de la RFC 6724 y ahí las ULA (`fc00::/7`) van **por debajo de
IPv4**: un invitado cuya única IPv6 fuera una ULA preferiría IPv4 para todo
destino dual-stack, y habría que meter `netsh interface ipv6 set prefixpolicy`
en cada invitado — justo la configuración por-VM que queremos eliminar. Con un
/64 **global** Windows lo trata como dirección normal y no hay que tocar nada.

🔲 **DECISIÓN PENDIENTE (mañana)**: qué /64 concreto se usa como `<IDENT>`.
Requisitos: global unicast, nuestro, y que **no se anuncie**. Nunca sale a la
red pública (se traduce en el nodo), pero debe ser nuestro para no ocupar
espacio ajeno.

⚠️ Reservar las direcciones bajas de los bloques públicos: `::2` es la VIP
actual de cada par. Los vmid empiezan muy por encima, pero conviene dejar
`::1–::ff` reservadas por convención.

### 4.2 Nodo — NPTv6 (un par de reglas, NO una por VM)

```nft
table ip6 nvxlat {
  chain post {
    type nat hook postrouting priority srcnat;
    ip6 saddr <IDENT>::/64 snat prefix to <BLOQUE_DE_SU_REGION>::/64
  }
  chain pre {
    type nat hook prerouting priority dstnat;
    ip6 daddr <BLOQUE_HEL>::/64 dnat prefix to <IDENT>::/64
    ip6 daddr <BLOQUE_FSN>::/64 dnat prefix to <IDENT>::/64
  }
}
```

- **Salida**: se traduce al bloque de la región **del nodo** → la salida es
  siempre por la base local, sin excepción. Un nodo nunca cambia de región,
  así que esta regla es fija por nodo y se pone una vez.
- **Entrada por las DOS**: las dos reglas de `pre` hacen que el cliente sea
  alcanzable por sus dos direcciones públicas si activa la IPv6 pública. La de
  su región llega directa; la de la otra entra por la base peer y cruza (+25 ms),
  lo cual es aceptable para una dirección secundaria.
- Como el sufijo se conserva y sólo se intercambia el /64, la traducción es
  **1:1 y determinista**.

⚠️ **CORRECCIÓN 2026-08-13: esto NO es sin estado.** Las cadenas `type nat` de
nftables van sobre conntrack por definición — sólo ven el primer paquete de
cada conexión. La versión anterior de este documento decía "cero conntrack,
nada que reconstruir tras migrar" y no se sostiene.

La **conclusión** sí aguanta, pero por otro motivo: como la traducción es
determinista, un flujo que aparece a media conversación en el nodo destino crea
su entrada de conntrack y recibe exactamente la misma traducción. Ahora bien,
eso depende de dos cosas que **hay que verificar explícitamente** en el nodo de
pruebas:

- `nf_conntrack_tcp_loose=1` (recogida de flujos a media conversación),
- que la cadena forward del nodo no descarte los `INVALID`.

Si alguna de las dos falla, las migraciones cortan las conexiones establecidas
— justo lo contrario de lo que persigue el diseño. Efecto colateral menor: el
nodo pasa a conntrackear también todo el IPv6 del invitado (hoy sólo el IPv4
del MASQUERADE). A 10–55 VMs por nodo es irrelevante en capacidad.

🔲 **VERIFICAR**: sólo se ha probado `snat prefix to`. Falta confirmar que
`dnat prefix to` existe en nftables 1.1.3 con la misma sintaxis. Si de verdad
hiciera falta traducción sin estado, la única vía es Jool SIIT/EAMT — que en el
nodo **no es una opción** (§4.4bis).

### 4.3 Base — rutas por VM y mapa NAT46 estático

- **Ruta**: `<BLOQUE_PROPIO>::<vmid>/128` → nodo donde vive la VM, por el
  túnel. Junto con la `/32` de la IPv4 privada (§4.4), es **el único estado
  por-VM que cambia al migrar**, y son dos actualizaciones de ruta: atómicas,
  instantáneas y **sin tocar el invitado**.
- **Mapa NAT46 del RDP**: pasa a ser `puerto 20000+vmid → <BLOQUE_PROPIO>::<vmid>`,
  con el bloque **de esa base**. Como el sufijo es el vmid y el prefijo es fijo
  por base, **el mapa se vuelve estático para siempre**: se genera una vez y no
  se vuelve a reescribir en ninguna migración. Lo que se mueve es la ruta.
- **Las dos bases deben mantener las rutas de los DOS bloques**, siempre. Si
  el /64 de HEL conmuta a b0 y b0 no sabe a qué nodo mandar
  `...fff1:5f::<vmid>`, la conmutación no sirve de nada.

### 4.3bis ⚠️ Tráfico VM↔VM (Samba) — el hueco del diseño

**Hoy va DIRECTO, nodo a nodo por IPv6, sin tocar la base.** Verificado
2026-08-11: el MT de un cliente (vm215, nodo 0000142) alcanza su propio SQX
(vm214, nodo 0000018) en el **puerto 445** usando su IPv6 como origen y
saliendo por `Ethernet`. Funciona porque la dirección de cada VM vive en el
/64 **del nodo**, y los /64 de los nodos se enrutan entre sí de forma nativa:
**coste cero, sin estado**. La VIP de la base ni siquiera responde en 445 (el
mapa de la base es `10000+vmid`, que es para llegar desde el PC del cliente,
otro caso de uso). Lo permite `GROUP-vm-default-IN`, que acepta 135/139/445
desde el ipset `hosts-ipv6` (todos los /64 de nodo) — independientemente del
flag `ipv6Enabled`, que sólo gobierna la entrada **desde Internet**.

**Con este diseño eso deja de ser directo.** Si la VM A se dirige a la
dirección pública de B (`<bloque>::<vmidB>`), el camino pasa a ser:
A → NPTv6 en su nodo → **base** (porque ahí enruta el bloque) → nodo de B →
DNAT → B. Es un rodeo.

**Coste real, estimado con lo medido:**
- **Misma región**: el rodeo son ~0,4 ms por salto contra la base →
  **latencia despreciable**. El ancho de banda sí pasa por la base, que hoy
  va al 4,2 % de un NIC de 10 G, así que hay sitio.
- **Distinta región**: ~25 ms, que es **lo mismo que cuesta hoy** el camino
  directo HEL↔FSN. No empeora.

**Decisión propuesta: aceptarlo en fase 0 y medirlo.** Es una copia de
ficheros ocasional entre las dos máquinas del propio cliente (típicamente
exportar datos del MT al SQX), no tráfico sostenido.

**Si al medir resulta que importa**, el arreglo es que cada nodo tenga rutas
`/128` del prefijo de identidad hacia los demás nodos — misma información que
ya se sincroniza a las bases, abanicada a los ~215 nodos (`sync-base-nat.py`
ya tiene un modo `sync nodes` que escribe 210 backends de nginx). Se descarta
de entrada porque una ruta rancia en un nodo = VMs que no se ven entre sí, con
un fallo parcial y confuso; sólo merece la pena si la medición lo justifica.

🔲 **Añadir a la validación de la fase 1**: copiar un fichero grande por SMB
entre las dos VMs de un cliente y comparar throughput antes/después.

### 4.4 Salida IPv4 — direccionamiento privado único por VM

**El hueco que había que cerrar primero.** `install.sh:620` da a **todos** los
nodos el mismo `10.0.0.1/16` y el bloque de dnsmasq de `first_boot.sh:103-104`
reparte `10.0.0.100–10.0.255.254` en cada uno: los 235 nodos usan el mismo
espacio privado. Mientras la salida sea el MASQUERADE del nodo eso da igual,
pero en cuanto el IPv4 del invitado cruce el túnel, la base **no puede
distinguir** el `10.0.0.105` del nodo A del del nodo B — ni saber por qué túnel
devolver la respuesta. La versión anterior de este documento decía "SNAT en la
base" sin resolver esto.

⚠️ Y hay una colisión concreta encima de la genérica: **la base usa
`10.0.0.1/24`, `.2` y `.3` en su propio `veth-host`** (netns de Jool — ver
`netns-jool-nat46-nat66-guide.md`). Los invitados que hoy caen en
`10.0.0.100–254` chocarían con la fontanería de la propia base.

**Decisión: una IPv4 privada por VM, única en toda la flota, que viaja con la
VM.** Espejo exacto de `<IDENT>::<vmid>`:

- Dirección sacada de `10.0.0.0/8` (16,7 M de direcciones para ~1.850 VMs),
  **excluyendo `10.0.0.0/24`** por lo de arriba.
- **Por VM, NO por nodo.** Un rango por nodo sería más simple, pero haría que
  la IPv4 del invitado cambiara en cada migración — y el IPv4 es justo la
  familia donde vive el bróker (Dukascopy ni siquiera tiene IPv6 real). Eso
  reintroduciría por debajo el corte que este proyecto existe para quitar: las
  migraciones intra-región volverían a tirar la conexión del MT.
- **Por DHCP con reserva estática por MAC**, no estática dentro de Windows.
  `reset_vm` rehace el invitado entero cada vez que un cliente pulsa
  "reinstalar", así que cualquier configuración in-guest se pierde; la MAC
  sobrevive. 🔲 **Misma pregunta pendiente para `<IDENT>::<vmid>`**: confirmar
  que el instalador la vuelve a poner tras un reinstall.
- **Puerta de enlace `10.0.0.1` idéntica en todos los nodos**, con máscara
  ancha y `proxy_arp` en el bridge, para que el invitado no necesite saber en
  qué nodo está y no haya que tocarlo nunca.
- **La base guarda una ruta `/32` por VM** hacia el túnel del nodo que la
  aloja — mismo objeto y mismo momento que la `/128` de §4.3.
- **SNAT normal de netfilter en la base** hacia la IPv4 failover. Sin
  traducción entre familias, así que el techo de puertos de §3.2 sigue siendo
  **por destino** y el arreglo `time_wait 120 → 30` sigue aplicando tal cual.
- `nf_conntrack_tcp_timeout_time_wait` = **30 s**.
- **`insert_failed` monitorizado** (§3.3).
- El MASQUERADE del nodo **se queda presente pero inactivo**, como ruta de
  rollback por nodo con un solo flip.

**Superficie de cambio (verificada 2026-08-13):** sólo **tres sitios** fijan
ese rango en los dos repos — `install.sh:620` (dirección de vmbr),
`install.sh:625/627` (MASQUERADE `-s 10.0.0.0/16`) y el bloque de dnsmasq de
`first_boot.sh:103-104`, duplicado en
`run_remotes/migrate_to_deterministic_ipv6.sh:212-213`. Nada más en ninguno de
los dos repos.

**Y hoy es un no-op.** El nodo hace MASQUERADE de lo que sea que tenga en
privado contra su propia IPv4 pública, así que renumerar no cambia nada
observable — ni para el cliente ni para nosotros. Por eso es el **primer** paso
de la fase 0: se hace nodo a nodo desde ya, sin comprometer ninguna otra
decisión, y deja el prerrequisito hecho mientras el resto se discute.

**Impacto en el cliente del renumerado**: un parpadeo único de sus conexiones
IPv4 salientes. **RDP y SMB van sobre IPv6 de punta a punta** desde la base
hasta el invitado, así que su sesión no se entera; el MT reconecta en los ~2 s
medidos en la vm215 (§5).

#### 4.4bis Descartado: NAT46 en el nodo (464XLAT)

La alternativa evaluada era CLAT sin estado en el nodo + NAT64 con estado en la
base, para que **ningún** paquete IPv4 cruzara el túnel. Descartada por tres
motivos, en orden de peso:

1. **El kernel de PVE no admite Jool.** Probado por el operador — es
   precisamente la razón de que las bases lleven Debian puro. Sin CLAT
   in-kernel en el nodo no hay 464XLAT, y la alternativa en espacio de usuario
   (Tayga) es monohilo en el camino crítico de cada cliente.
2. **DKMS en 235 hipervisores.** Un fallo de build tras un upgrade de kernel
   dejaría a los invitados de ese nodo sin bróker.
3. **El techo de puertos empeora.** La BIB de un NAT64 (RFC 6146) mapea
   (IPv6 origen, puerto) → (IPv4, puerto): el puerto queda ligado al **origen**,
   no al destino. El techo dejaría de ser por destino y pasaría a ser
   **global** — ~64.512 puertos por IPv4 contra los ~28k flujos concurrentes
   medidos en la flota. Habría obligado a un `pool4` de varias IPv4 desde el
   día 0 y a reescribir §3.2 entera.

Al caerse el CLAT del nodo se cae también el **Jool con estado en la base**: la
salida es SNAT de netfilter normal. El `jool_siit` actual (entrada IPv4→VM) se
queda exactamente como está.

También descartado: **conntrack zones + fwmark + policy routing** en la base
para desambiguar el `10.0.0.0/16` duplicado. Funciona, pero es estado que hay
que mantener; la dirección única por VM lo hace innecesario con enrutado
normal.

🔲 El spike de Jool en el nodo de pruebas se mantiene, pero baja de "gatea el
proyecto" a "conviene saberlo": haría falta el día que se quieran invitados
IPv6-only. Ya no bloquea nada.

### 4.5 Túnel base↔nodo y elección de la IPv4 de salida

**Dos túneles por nodo, terminados en la IP PRINCIPAL de cada base.** Ambos
siempre arriba, así que las dos bases alcanzan siempre todos los nodos.

⚠️ **Por qué NO terminarlos en la VIP de failover.** Es tentador: los paquetes
llegarían solos a la base que en ese momento posee la IP, y una conmutación
arrastraría el túnel gratis sin tocar el nodo. Pero entonces **sólo la base que
posee la VIP alcanza ese nodo**, y eso rompe la entrada cruzada que §2 tiene
verificada — el puerto `20959` de una VM de Helsinki responde por las **dos**
VIPs. Es justo lo que hace que la URL guardada del cliente no se rompa al
migrar de región, y lo que piden las dos reglas de `pre` de §4.2.

**La base deduce la IPv4 de salida de POR QUÉ TÚNEL entró el paquete**, no de
una línea fija. De la interfaz de entrada sale el nodo, del nodo la región, y
de la región la VIP. Esa regla es correcta en los dos estados **sin que la base
consulte a Robot**:

| estado | camino | SNAT |
|---|---|---|
| normal | nodo HEL → túnel a b1 | b1 → VIP de HEL, que posee ✔ |
| b1 caída | nodo HEL → túnel a b0; Hetzner ya movió la VIP | b0 → VIP de HEL, que **ahora** posee ✔ |

La regla "de dónde viene → su VIP" no cambia nunca; lo único que cambia es qué
base la ejecuta, y eso lo decide el nodo eligiendo túnel. Esto sustituye a la
línea estática de hoy (`ip saddr 10.0.0.0/24 oifname enp6s0 snat to
37.27.135.250`), que apunta a la IP principal y no distingue origen.

**Ventana residual**: entre que una base cae y que Hetzner acaba de propagar la
VIP pasan **2–3 min** (`base/docs/failover-watchdog.md`). Durante ese hueco los
paquetes que salgan con origen esa VIP tienen el retorno apuntando a una
máquina muerta. No lo arregla ningún diseño de túnel — es el riesgo #1.

**MSS clamping desde el día 0** (§9.3). El túnel lleva **las dos familias**: el
IPv4 del invitado va 4in6, así que el nodo no necesita IPv4 propia para dar
salida IPv4 a sus invitados. Eso resuelve de paso el problema del **host**: el
`apt` y los `curl` del propio PVE pueden usar la misma tubería el día que se le
quite la IPv4 pública.

#### `preferred_lft 0` en las bases: se queda como está

Las 4 VIPs están enlazadas en las dos bases como *deprecated*. Ese flag saca la
dirección de la **selección automática de origen** del kernel; **no afecta a un
SNAT explícito ni a un `local <ip>` explícito en un túnel**. La prueba está
corriendo hoy en producción: `ct status dnat snat to ct original daddr` ya hace
que las respuestas de cada sesión entrante salgan con la VIP como origen.

No hay que tocarlo, y de hecho **no se debe**:

1. Impide que la base que **no** posee una VIP origine tráfico desde ella
   (saldría, y las respuestas irían a la otra base, que no tiene estado para
   ellas → fallo silencioso, y del tipo peor: *timeouts* hacia el bróker con la
   infraestructura aparentemente sana).
2. Mantiene el tráfico propio de la base (nginx, Firestore, apt) saliendo por
   la IP principal, no por la de cara al cliente (riesgo #7).
3. Es lo que permite que una conmutación sea puramente del lado de Hetzner —
   la propiedad de §2 sobre la que se apoya todo el diseño.

### 4.6 DNS — una URL por VM

- Registro por VM: `trading-<vmid>.neuravps.com` (o `sqx-<vmid>`), **CNAME al
  nombre de la VIP regional que toque**, actualizado al cambiar de región.
- El cliente lo guarda **una vez y para siempre**.
- **TTL 60 s** en el registro.
- ⚠️ **Trampa del caché negativo**: el SOA de `neuravps.com` tiene
  `minimum = 3600`, o sea **1 hora de NXDOMAIN cacheado**. Si alguien pregunta
  por el nombre **antes** de que exista, ese cliente se queda fuera hasta 60
  min con el registro ya creado. Dos arreglos, los dos necesarios:
  1. **Crear el CNAME al principio del aprovisionamiento**, antes del email de
     activación y antes de que el panel enseñe la URL.
  2. **Bajar el `minimum` del SOA a 60 s** (ajuste de zona en Hetzner DNS).
- Hoy **todos los registros están a TTL 3600**; hay que bajarlos igualmente.
- Token de la API de DNS de Hetzner ya disponible en `/opt/letsencrypt/hetzner.env`
  (el que usa certbot para DNS-01).

**Descartado y por qué**: *GeoDNS / DNS por latencia* resuelve según dónde está
el **cliente**, no su VPS — responde a otra pregunta. *Anycast* necesitaría AS
propio, espacio PI y BGP, y otra vez enruta por ubicación del cliente. *Una
sola VIP que reenvíe entre regiones* funciona pero fija +25 ms a media flota.

---

## 5. Decisiones tomadas (para no re-discutirlas mañana)

| Decisión | Motivo |
|---|---|
| **Una IPv4 de salida por base**, no un pool | Con el perfil real de uso aguanta (§3.2). Simplicidad. |
| **Prefijo público atado al NODO**, no a la VM | La salida es siempre local → latencia mínima siempre. Se acepta el corte de conexiones al cruzar región. |
| **Se acepta el corte en migración cruzada** | Decisión explícita del operador: *"prefiero pagar un posible minuto de desconexión a tener latencia superior todo el tiempo"*. |
| **La región es DESEMPATE en el relief** | No es criterio de colocación: si el mejor destino está en la otra región, va igual. Sólo desempata entre destinos equivalentes, y convierte parte de los cortes diurnos en transparentes. Coste cero. |
| **Identidad en /64 global, NO ULA** | RFC 6724 en Windows (§4.1). |
| **IPv4 privada por VM, no por nodo** (08-13) | Un rango por nodo cambiaría la IPv4 del invitado en cada migración, y el IPv4 es donde vive el bróker → reintroduce el corte que el proyecto quita (§4.4). |
| **IPv4 del invitado por DHCP con reserva por MAC** (08-13) | `reset_vm` rehace el invitado a petición del cliente y se lleva cualquier configuración in-guest; la MAC sobrevive (§4.4). |
| **Sin NAT46 en el nodo** (08-13) | El kernel de PVE no admite Jool, y el techo de puertos de un NAT64 sería global en vez de por destino (§4.4bis). |
| **Túnel a la IP principal de cada base, no a la VIP** (08-13) | Terminarlo en la VIP dejaría a la base peer sin camino al nodo y rompería la entrada por las dos VIPs (§4.5). |
| **La IPv4 de salida se deduce del túnel de entrada** (08-13) | Una línea estática puede apuntar a una VIP que esa base ya no posee → fallo silencioso. Por túnel, la regla es correcta antes y después de una conmutación, sin consultar a Robot (§4.5). |
| **No se tocan los flags de las VIPs** (08-13) | `preferred_lft 0` es una protección contra originar tráfico desde una VIP ajena, no un obstáculo: el SNAT explícito lo ignora (§4.5). |
| **SQX también sale por la base** | Su throttle de Dukascopy **no es por IP de origen** (reproducido desde 12+ IPs, 7+ subredes, ambos DC y una red no-Hetzner), así que consolidar no lo empeora. |
| **Add-on de IPv4 dedicada = fase 1**, no fase 0 | Fase 0 empeora el reparto (de ~31 VMs por IP a toda la región). El add-on lo compensa para quien lo pague. |

### Efecto sobre las migraciones

Hoy la IPv6 del invitado sale del prefijo del nodo, así que **se corta en TODAS
las migraciones**, crucen región o no.

| | hoy | con este diseño |
|---|---|---|
| migración misma región | corta | **transparente** |
| migración cruzando región | corta | corta (igual) |
| fallo por guest agent ocupado | **sí** | **eliminado** |

Nunca empeora nada; quita el corte en una parte y elimina el modo de fallo del
guest agent en todas.

**Volumen real**: **~12 migraciones/día** (531 en 30 días). Reparto horario:
**72 % entre 22:00 y 07:00 UTC** (el defrag de las 06:00 se lleva él solo el
57 % del total), **28 % en horario de mercado** (~5/día, de los *relief*
horarios) — que es a lo que apunta el desempate por región.

**Coste de un corte**: reconexión medida en producción (vm215, 2026-08-10):
`authorized` → `synchronized` → 4 órdenes recolocadas en **~2 segundos**. Lo
variable es la **detección** (segundos a un minuto). Durante el hueco no hay
ticks ni se pueden abrir/modificar posiciones; **las órdenes ya enviadas siguen
vivas en el bróker**.

---

## 6. Qué cambia en el código

### `migrate_vm.sh` — adelgaza mucho

Desaparece toda la parte de reconfigurar la IP dentro del invitado:

- `_wait_agent_dst()` — ya no hace falta esperar al guest agent
- el `agent/exec` de PowerShell que re-bindea la IPv6
- `_verify_dest_ipv6()` — ya no hay nada que verificar dentro
- el flag `MIGRATION_DEGRADED` por esta causa

Se sustituye por: **actualizar la ruta `/128` y la `/32` en las dos bases**. Se
mantiene la sonda RDP post-migración como comprobación de salud.

### `sync-base-nat.py`

- El mapa `puerto → IPv6` pasa a **generarse determinísticamente** del vmid
  (`20000+vmid → <bloque_propio>::<vmid>`) y deja de reescribirse al migrar.
- **Nuevo**: sincronizar la tabla de rutas `/128` por VM (de los **dos**
  bloques) hacia el nodo actual de cada VM, y la `/32` de su IPv4 privada.

⚠️ **Gotcha**: `sync_full()` lee Firestore y **reemplaza** el estado entero de
la base. Una entrada metida a mano (`sync <vmid> <ipv6>`) sobrevive hasta el
siguiente arranque o sync completo y luego desaparece. La VM de pruebas
necesita doc real en `servers` — y con `maintenance: true`, o conncheck
presentará su `connectivity_distress` y mandará correo a soporte cada vez que
la toquemos. Un doc de `servers` sin `orderId` es inofensivo para facturación.

### Nodo — renumerado del espacio privado (§4.4)

- `install.sh:620` — dirección de vmbr, hoy `10.0.0.1/16`.
- `install.sh:625/627` — MASQUERADE `-s 10.0.0.0/16` (se queda como rollback).
- `first_boot.sh:103-104` — rango de dnsmasq + `dhcp-option=3`; duplicado en
  `run_remotes/migrate_to_deterministic_ipv6.sh:212-213`. **Los dos a la vez.**
- `proxy_arp` en el bridge.
- Reserva estática por MAC para cada VM del nodo.

### Aprovisionamiento

- Configurar en el invitado `<IDENT>::<vmid>` (una vez, nunca más).
- Asignar la IPv4 privada única de la VM (reserva DHCP por MAC).
- Crear el CNAME DNS **al principio**, antes del email de activación.
- Instalar el par de reglas nft del nodo (§4.2) — una vez por nodo.

### Panel

- Mostrar la URL `trading-<vmid>` como dato de conexión.
- Mostrar las **dos** IPv6 públicas, con la primaria destacada y la secundaria
  como detalle avanzado (si no, se convierte en tickets de *"¿cuál es la mía?"*).
- Llamar a la cosa por su nombre: no es "desactivar el firewall", es **activar
  la IPv6 pública** (`firewall.ipv6Enabled`, hoy `false` en casi toda la flota).
- 🔲 **Bug aparte, ya detectado**: `PanelContent.tsx:1517` hace
  `nodeMaintenance.location || DEFAULT_NODE_LOCATION` y las reglas de Firestore
  sólo dejan leer `proxmox_nodes` cuando `max_vm == 1` → **1.212 de 1.843 VMs
  muestran "Falkenstein" estando en Helsinki**. Arreglo: usar
  `server.location`, que el cliente sí puede leer y coincide con el nodo en
  **1845/1845** casos.

---

## 7. Plan de despliegue por fases

Orden acordado con el operador. **Ninguna fase avanza sin validar la anterior.**

**La invariante que protege a los clientes: en fase 0 y fase 1 todo es
aditivo.** Tabla nueva `ip6 nvxlat` en el nodo de pruebas, interfaces de túnel
nuevas, rutas nuevas. Cero ediciones a reglas existentes, cero `flush ruleset`,
cero cambios en las 4 VIPs de producción. Si algo sale mal, lo que se borra es
lo que hemos añadido.

### Nodos de prueba (elegidos 2026-08-13)

Hay **12 AX162-2-LTD vacíos** (0 VMs), instalados el 2026-07-29, 96 cores /
251 GB, `nodeType: SQX`, sincronizando a diario y térmicamente sanos:
Falkenstein 0000227/228/229; Helsinki 0000230/231/233/234/235/236/237/238/239.

- **Fase 1 → `0000238-AX162-2-LTD`** (Helsinki, `2a01:4f9:3100:4b08::2`) + b1.
- **Fase 2 → `0000227-AX162-2-LTD`** (Falkenstein, `2a01:4f8:2240:201e::2`) + b0.

Se reservan **los dos desde el principio** para no tener que negociar el nodo
de la otra región a mitad de experimento. Quedan 10 vacíos de reserva.

### Fase 0 — preparación (sin impacto)

**0.A — Vallar los dos nodos, antes que nada.** `frozen: true` en
`proxmox_nodes/0000238` y `/0000227` desde admin/nodos. Verificado en código:
`auto_provision.py:816` los excluye de la colocación automática y
`neuravps-defrag.py:285` como destino — con doble red, porque el defrag además
nunca elige un nodo vacío ("empties are the operator's drain/maintenance
reserve"). Imposible que aparezca una VM de cliente encima del experimento.
⚠️ **Contrapartida**: `node_liveness.py:805` también salta los nodos frozen, así
que esos dos dejan de tener alerta de liveness mientras dure.

**0.B — Renumerar el espacio privado (§4.4).** Es un **no-op hoy** y
prerrequisito de todo lo demás, así que va lo primero y puede avanzar en
paralelo al resto. Nodo a nodo, empezando por los dos de prueba. 🔲 Validar
antes en la VM de pruebas: que Windows encaja limpiamente el NAK-y-rebind de
dnsmasq cuando el rango cambia bajo sus pies, y que `proxy_arp` se comporta con
el bridge de PVE.

**0.C — DNS: TTL y `minimum` del SOA a 60 s (§4.6).** Va pronto porque es lo
que más tarda en surtir efecto: un TTL nuevo no se nota hasta que expira el
**viejo**, y hoy está todo a 3600. Riesgo cero e independiente del resto. 🔲
Confirmar que la siguiente renovación de certbot (DNS-01 con
`/opt/letsencrypt/hetzner.env`) pasa sin problema.

**0.D — Métricas de saturación.** `insert_failed` de `conntrack -S` en las dos
bases, muestreado y avisando sólo cuando el delta sea distinto de cero. **Hay
que tomar la línea base ahora**, antes de consolidar nada, o el número no sirve
para comparar. Se haría igual aunque el proyecto se cancelara: el incidente del
2026-07-16 fue exactamente agotamiento de conntrack y hoy no tenemos ese
contador en ningún sitio. No hay pipeline de métricas de base a Firestore; lo
más barato es un timer con el patrón de conncheck/sweepguard, que ya tienen
credenciales. Cumple "sólo accionable por nosotros": si sube, la acción es
nuestra.

**0.E — Cronometrar la conmutación, SIN tocar las 4 VIPs de producción.** Es la
única parte de la fase 0 con riesgo real para clientes, y se puede llevar a
riesgo cero: **pedir una failover IPv4 adicional dedicada a la prueba**
(≈€1,73–1,96/mes) y enlazarla en las dos bases igual que las otras. Con una VIP
sin clientes encima se puede conmutar adelante y atrás muchas veces y quedarse
con una **distribución**, no con una muestra. Y no es dinero tirado: la fase 5
necesita failover IPs de todas formas. Qué medir, con sondeo continuo desde un
tercer punto de vista: (1) del POST a Robot hasta que el tráfico sigue de
verdad, (2) si una TCP establecida sobrevive — no debería, el estado se queda
en la base vieja — y cuánto tarda en reconectar, (3) cuánto tarda una conexión
**nueva** en tener éxito, (4) **cuánto tarda el conjunto**, incluida nuestra
parte de reconciliación de la regla de SNAT (§4.5).
⚠️ `failover-watchdog-drill.sh` **no sirve** para esto: con `dryRun:true` no
mueve nada, y con `dryRun:false` mueve las VIPs **de producción**. Como
cross-check gratis, instrumentar el próximo drenaje de mantenimiento.
🔲 **Fijar el criterio de aborto por escrito ANTES de conocer el resultado**:
qué número de segundos convierte esto en un "no seguimos". Es mucho más fácil
ahora que con la cifra delante.

**0.F — Elegir el `<IDENT>` /64.** Requisito: global unicast, nuestro, no
anunciado. No tenemos espacio PI, así que lo práctico es **pedir a Hetzner un
/64 adicional asignado a una de las bases**. Vuelta de tuerca: dejarlo enrutado
a esa base con una regla que lo **cuente y lo tire**. Si alguna vez falla una
traducción NPTv6 y un paquete se escapa con origen IDENT, el retorno muere en
nuestra propia base, en un contador que podemos mirar, en vez de en el vacío.
Detector de fugas gratis y fallo cerrado.

**0.G — Túnel base↔nodo en el nodo de pruebas (§4.5), con MSS clamping.** Va
después de 0.A y 0.B. ⚠️ El nodo usa `iptables` (`install.sh:625`) y la base
carga `/etc/nftables.conf` con `flush ruleset` — cualquier `systemctl reload
nftables` se lleva por delante lo que no esté en fichero, así que la tabla
nueva va en su propio include desde el principio, no como `nft add table` a
mano. Verificar antes de que exista ninguna VM: PMTUD en los dos sentidos,
ICMPv6 incrustado (§9.4), y que `pve-firewall` no pisa la tabla nueva.

### Fase 1 — un nodo, una VM de prueba, una base
- Nodo de Helsinki (**0000238**) + b1. VM de prueba **nuestra**, no de cliente.
- Aplicar: identidad en el invitado, par de reglas nft en el nodo, ruta `/128`
  en las **dos** bases, entrada NAT46 estática.
- **Validar** (§8) y **medir** antes/después.

### Fase 2 — un nodo de la otra región
- Nodo de Falkenstein (**0000227**) + b0, misma VM de prueba migrada allí.
- **Lo que se valida aquí es lo que no se puede validar en la fase 1**: que al
  cruzar de región la pública cambia sola, que la salida sigue siendo local, y
  que Windows **no se ha tocado**.

### Fase 3 — un nodo entero de clientes reales
- Un solo nodo, preferiblemente de SQX (menos sensible a cortes que MT).
- Ventana nocturna. Rollback preparado (flip del MASQUERADE del nodo).

### Fase 4 — flota
- Nodo a nodo, empezando por los solo-SQX (194 nodos) y dejando los solo-MT
  (21 nodos, 1003 VMs) para el final.
- La flota está **limpiamente segregada**: 21 nodos solo-MT, 194 solo-SQX,
  **cero mixtos** → se pueden mover clases enteras sin mezclar.

### Fase 5 (posterior) — add-on de IPv4 dedicada
Una vez la salida es SNAT en la base, dar IP propia a un cliente es **una
regla**, no una arquitectura:

```nft
ip6 saddr <IDENT>::<vmid> snat to <su_IPv4_dedicada>
```

delante de la regla del pool. Se aprovisiona y se revoca al instante.
⚠️ **Esa IPv4 dedicada tiene que ser también una failover**, no una IP
adicional atada a un servidor: si no, una conmutación de base deja al cliente
con la IP colgada en la máquina caída — justo el escenario por el que paga.

---

## 8. Checklist de validación (cada fase)

Desde fuera:
- [ ] RDP entra por `trading-<vmid>.neuravps.com:<20000+vmid>`
- [ ] RDP entra también por la VIP de la **otra** base (debe seguir funcionando)
- [ ] `https://api.ipify.org` desde el invitado devuelve la IPv4 de salida
      **esperada de su región**
- [ ] `https://api64.ipify.org` devuelve `<bloque_de_su_región>::<vmid>`
- [ ] Alcanzable por **las dos** IPv6 públicas con la IPv6 pública activada

Dentro del invitado:
- [ ] Tiene **una sola** dirección IPv6 y es `<IDENT>::<vmid>`
- [ ] Tiene su IPv4 privada única, y **es la misma tras migrar** (§4.4)
- [ ] **No se ha tocado nada** tras la migración (misma config que antes)
- [ ] Sobrevive a un `reset_vm`: tras reinstalar, IPv6 e IPv4 vuelven a ser las
      mismas (la reserva DHCP por MAC debe bastar)
- [ ] MTU/PMTUD: descarga de un fichero grande por HTTPS sin cortes

En el nodo y la base:
- [ ] `nft list table ip6 nvxlat` con las reglas esperadas
- [ ] Ruta `/128` **y** `/32` presentes en **las dos** bases
- [ ] `conntrack -S` → `insert_failed` **no sube**
- [ ] ICMPv6: `ping6` y PMTUD funcionan en los dos sentidos
- [ ] `nf_conntrack_tcp_loose=1` y la cadena forward no descarta `INVALID`
      (§4.2 — sin esto las migraciones cortan las conexiones establecidas)
- [ ] El SNAT sale de la VIP **correcta según el túnel de entrada** (§4.5)
- [ ] Con la base peer, el mismo puerto sigue respondiendo (entrada por las dos)

Tráfico VM↔VM (§4.3bis):
- [ ] SMB 445 entre las dos VMs del mismo cliente sigue funcionando
- [ ] Throughput de una copia grande por SMB **comparado con antes** (hoy es
      directo nodo-a-nodo; con el diseño pasa por la base)
- [ ] `insert_failed` en la base no sube durante esa copia

Migración:
- [ ] Migración **misma región** → la sesión RDP y las conexiones MT
      **sobreviven**
- [ ] Migración **cruzando región** → cortan y **reconectan solas**; la
      pública cambia de bloque; Windows sigue sin tocarse
- [ ] `migrate_vm.sh` ya **no invoca el guest agent** para la IP

---

## 9. Riesgos y pendientes

| # | Riesgo / pendiente | Estado |
|---|---|---|
| 1 | **Cuánto tarda una conmutación de failover.** Hoy ese hueco cuesta acceso RDP; después costaría **trading en vivo**, porque **todo** el tráfico del cliente pasa a cruzar la base, no sólo el RDP. Es el único número que falta y **gatea todo el proyecto**. Medirlo sobre una **VIP de prueba dedicada**, no sobre las 4 de producción (§7, 0.E). | 🔲 **MEDIR ANTES DE EMPEZAR** |
| 2 | `dnat prefix to` en nftables 1.1.3 sin verificar (sólo se probó `snat prefix to`) | 🔲 verificar en fase 1 |
| 3 | **MTU/MSS clamping** en el túnel base↔nodo. Sin él aparecen los "carga a medias" imposibles de diagnosticar. | 🔲 día 0 |
| 4 | **ICMPv6**: NPTv6 debe reescribir también las direcciones incrustadas en los errores ICMPv6, o revienta el PMTUD. `snat prefix` cubre la cabecera externa; el payload incrustado hay que comprobarlo. | 🔲 verificar |
| 5 | **`github.com` no tiene IPv6 real** (devuelve v4 mapeada) y los instaladores de invitado tiran de ahí. **Resuelto por diseño**: el túnel lleva las dos familias (§4.5), así que invitado y host del nodo salen por IPv4 sin necesitar IPv4 propia. | ✅ resuelto 08-13, 🔲 probar antes de quitar la primera IPv4 |
| 6 | **Confirmar con Hetzner** que se puede renunciar a la IPv4 primaria de un dedicado ya contratado. Todo el presupuesto de §10 cuelga de esto. | 🔲 |
| 7 | **Radio de impacto**: con IP única, una lista negra o un aviso de abuso afecta a toda la región (hoy contenido a ~31 VMs por IP de nodo). Ya pasó con la máquina `devel` en julio. No lo arregla ningún parámetro del kernel — sólo repartir en más IPs. | ⚠️ asumido conscientemente en fase 0 |
| 8 | Medir el churn de Dukascopy **en hora punta** (la medición actual es del momento más muerto de la semana). | 🔲 |
| 9 | **`nf_conntrack_tcp_loose` y descarte de `INVALID` en el nodo.** El NPTv6 va sobre conntrack (§4.2); si un flujo a media conversación no se recoge en el nodo destino, la migración corta las conexiones establecidas — lo contrario del objetivo. | 🔲 verificar en fase 1 |
| 10 | **Reconciliar la regla de SNAT tras una conmutación.** Con el reparto por túnel (§4.5) la regla es correcta sola, pero hay que **probarlo** en el simulacro, no darlo por hecho. Un fallo aquí es del tipo peor: *timeouts* al bróker con la infraestructura aparentemente sana. | 🔲 incluir en 0.E |
| 11 | **Windows ante el renumerado IPv4** (NAK-y-rebind de dnsmasq) y `proxy_arp` sobre el bridge de PVE. | 🔲 validar en la VM de pruebas antes de tocar flota |
| 12 | **Que la identidad sobreviva a `reset_vm`.** El cliente puede reinstalar desde el panel y eso rehace el invitado entero. La reserva DHCP por MAC cubre el IPv4; hay que confirmar que el instalador vuelve a poner `<IDENT>::<vmid>`. | 🔲 |
| 13 | ~~El kernel de PVE no admite Jool~~ → **deja de ser un riesgo**: el diseño ya no necesita Jool en el nodo (§4.4bis). El spike se mantiene como información, no como bloqueo. | ✅ cerrado 08-13 |

---

## 10. Economía (referencia, precios del operador)

- IPv4 de nodo: **€1,70/mes** × 235 nodos = **€399,50/mes** liberables si toda
  la salida pasa por la base.
- Bloques failover: `/24` €443,60 · `/25` €226 · `/26` €117,20 · `/27` €62,80
  (≈ €1,73–1,96 por IP; descuento por volumen).
- **No hace falta contigüidad**: si el bloque se queda corto se pide otro y los
  clientes existentes **conservan su IP**. Un `/26 → /25` **no es una
  renumeración**, es una adición.
- Dimensionado por ratio comercial, no técnico. Con 1003 MT y **+119 MT/mes**
  (media de 6 meses): `/25` a 10 VMs/IP = 1,7 meses de pista; **`/24` a 10
  VMs/IP = 12 meses**; a 20 VMs/IP = 33 meses.
- ⚠️ Si SQX se quedara en salida por nodo, sólo se liberarían **€69,70** (21
  nodos solo-MT + 20 vacíos), no €399,50.

---

## 11. Contexto de latencia (afecta a dónde comprar el bloque)

Medido contra el servidor de Darwinex al que estaba conectado un cliente real,
25 muestras desde cada base:

| desde | min | p50 | p90 | jitter |
|---|---|---|---|---|
| Helsinki (b1) | 32,5 | **35,9 ms** | **105,1 ms** | 72,6 ms |
| Falkenstein (b0) | 17,8 | **18,1 ms** | 20,1 ms | **2,3 ms** |

**Falkenstein es 2× más rápido y ~30× más estable hacia el bróker**, y hoy
**843 de 1003 VMs MT están en Helsinki**. Coste de cruzar regiones (RTT b0↔b1
= 25,3 ms): nodo DE→base DE ≈18 ms · nodo FI→base FI ≈36 ms · nodo FI→base DE
≈43 ms · nodo DE→base FI ≈61 ms. **Para MT, la salida siempre por la base
local** — que es lo que garantiza §4.2.

🔲 Antes de comprometer hierro: medir contra los 3-5 brókers que de verdad usan
los clientes (se extraen de las líneas `authorized on <servidor>` de los
diarios `logs\*.log` de la flota MT).

---

## 12. Siguiente sesión (actualizado 2026-08-13)

Por orden. Lo de arriba no depende de decisiones pendientes; lo de abajo sí.

1. **Congelar 0000238 y 0000227** (§7, 0.A). Un toggle en admin/nodos.
2. **Bajar SOA `minimum` y TTL a 60 s** (§4.6). Independiente, riesgo cero, y
   es lo que más tarda en surtir efecto — cuanto antes, mejor.
3. **Línea base de `insert_failed`** en las dos bases (§7, 0.D).
4. **Renumerado del espacio privado** (§4.4), empezando por los dos nodos de
   prueba. No-op hoy, prerrequisito de todo.
5. **Decidir**: ¿se pide la failover IPv4 de pruebas para cronometrar la
   conmutación, o se instrumenta el próximo drenaje de mantenimiento? (§7, 0.E)
   — **gatea todo el proyecto**.
6. **Decidir** el `<IDENT>` /64 (§7, 0.F): pedir un /64 adicional a Hetzner.
7. Crear la VM de prueba con doc en `servers` + `maintenance: true` (§6).
8. Verificar `dnat prefix to`, `tcp_loose` y descarte de `INVALID` (§9.2, §9.9).

Pendiente de escribir cuando haya cifra de 0.E: **el criterio de aborto**.
