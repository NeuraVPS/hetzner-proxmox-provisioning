# Salida por failover IPs + IPv6 estable por VM

**Estado: DISEÑADO, NO EJECUTADO.** Documento de trabajo para la sesión de
prueba (operador + asistente). Todas las cifras de este documento están
**medidas** el 2026-08-10/11 contra producción, no estimadas — para no volver a
derivarlas mañana. Nada de lo aquí descrito se ha aplicado.

**Revisión 2026-08-13** (sesión operador + asistente, dos rondas). El diseño se
ha **simplificado mucho** respecto a la versión del 08-11.

**Ronda 1 — la salida IPv4 y el invitado:**

- **La salida IPv4 tenía un hueco sin resolver** y ahora está cerrado: los 235
  nodos comparten el mismo `10.0.0.0/16`, así que "SNAT en la base" no era
  implementable tal cual. Solución: **una IPv4 privada por VM, única en toda la
  flota, que viaja con la VM** (§4.4). Espejo exacto del direccionamiento IPv6.
- **Fuera el DHCP**: IPv4 e IPv6 **estáticas in-guest**, puestas en la provisión
  y en el `reset_vm` y nunca más. Es el modelo que el IPv6 ya usa hoy, y
  **dnsmasq desaparece del nodo** (§4.4).
- **Descartado NAT46 en el nodo (464XLAT)**: el kernel de PVE no admite Jool
  (§4.4bis). Al caerse, se cae también el Jool con estado en la base — y §3.2
  vuelve a ser válida tal como está escrita.

**Ronda 2 — la salida a Internet y el reparto de responsabilidades:**

- **La salida va por la IP PRINCIPAL de cada base, no por la failover** (§4.4).
  Decisión del operador: el requisito no es "una IP fija por cliente" sino **un
  conjunto finito, enumerable y nuestro**, que se le enseña al cliente desde el
  primer día. Con eso desaparece toda la dependencia del estado de propiedad de
  las VIPs, y con ella la ventana de propagación de Hetzner en la salida.
- **La IPv6 pública sale del /64 propio de cada base**, misma lógica (§4.1).
- **La traducción se hace en la BASE, no en el nodo** (§4.2). El nodo queda como
  puro transporte: dos túneles, rutas y MSS clamping. Sin nft, sin conntrack.
  Eso mata los riesgos #2 y #9 enteros.
- **Túnel: `ip6gre`, dos por nodo, a la IP principal de cada base** (§4.5).
- **`preferred_lft 0` de las VIPs se queda como está** — es una protección, no
  una limitación (§4.5). Las failover quedan reducidas a **entrada**.
- **Regresión de aislamiento detectada y tapada** (§4.3bis): con todo pasando
  por la base, un cliente podría abrir RDP contra la VM de otro. Regla en la
  base desde v0.
- **Criterio de diseño explícito** (§4.6) y hoja de ruta v0 / v1 / v1.1.

**Ronda 3 — anclaje del túnel, y ejecución arrancada:**

- **Los túneles se anclan a las DOS VIPs, no a las IPs principales** (§4.5). Con
  eso la salida sigue a la VIP sola cuando se mueve, apagar una base es sólo
  mover las VIPs, y **el sondeo en el nodo pasa de obligatorio a opcional**.
- **`ip6gre` es sin estado y Linux no implementa sus keepalives** (§4.2bis): con
  anclaje a IP principal el túnel apuntaría a una caja muerta para siempre.
- ⚠️ **Métricas distintas en las dos rutas por defecto** — si quedan iguales,
  Linux hace ECMP y la IP de salida del cliente baila por conexión (§4.2bis).
- **Los dos nodos de prueba están CONGELADOS** (§7). Cambia el de Falkenstein:
  0000227 se llenó de clientes por no haber congelado a tiempo.
- **Criterio de aborto fijado** (§7, 0.H) y **`<IDENT>` elegido** (§7, 0.F).
- **Dos cambios independientes** que se aplican en el mismo viaje (§7, fase
  0bis): el `cluster.fw` desfasado y cerrar el DNAT de la IP principal.
- Corregido: `snat/dnat prefix to` **no es sin estado** (§4.2, ya sin uso).
- **Riesgo #6 CERRADO**: el operador confirma que Hetzner permite renunciar a la
  IPv4 de un dedicado ya contratado (€1,70/mes cada una, el /64 se queda).
- Nodos de prueba elegidos y plan de fase 0 reescrito (§7). **b0 va primero.**

---

## 1. Qué resuelve

| Problema hoy | Efecto |
|---|---|
| La IPv4 de salida del cliente es la del **nodo** | Cambia en cada migración. Caso real (marcolaralba, vm215): **7 IPs distintas en 9 meses**, una de ellas en otro país. Inaceptable para prop firms. |
| La IPv6 del invitado se deriva del /64 del **nodo** (`expected = <prefijo_destino><vmid>`) | Hay que reconfigurarla **dentro de Windows** en cada migración, vía guest agent. Cuando el agente está ocupado, la migración se completa pero deja al cliente inalcanzable → flag `MIGRATION_DEGRADED` en `migrate_vm.sh`. Es la causa principal de migraciones fallidas. |
| No hay forma de dar IP fija ni dedicada | No se puede vender el add-on. |

**El objetivo NO es ahorrar dinero.** Se hace por producto, por fiabilidad
(matar el `MIGRATION_DEGRADED`) y para habilitar el add-on. El ahorro llega
igualmente: €399,50/mes de IPv4 de nodo, ahora confirmado como recuperable.

### Qué se le promete al cliente (decisión 2026-08-13)

**No es "tu IP no cambia nunca".** Es **"tus IPs son estas cuatro, y te las
decimos desde el primer día"**:

| | |
|---|---|
| `<ipv4_b0>` | salida cuando estás servido desde Falkenstein — compartida |
| `<ipv6_b0>::<vmid>` | entrada y salida por Falkenstein — **sólo tuya** |
| `<ipv4_b1>` | salida cuando estás servido desde Helsinki — compartida |
| `<ipv6_b1>::<vmid>` | entrada y salida por Helsinki — **sólo tuya** |

Eso es enumerable, se puede poner en una lista blanca, y **convierte el
mantenimiento en un no-evento**: el día que se conmuta una base no ha pasado
nada raro, estaba dicho de antemano. Con la promesa de IP fija, ese mismo día
es una incidencia.

Sigue siendo una mejora enorme sobre lo de hoy — de 235 IPs de nodo
impredecibles a 2 nuestras y estables — pero **no hay que venderlo como IP
fija**. La IP fija de verdad es el add-on (§7, fase 5): dos IPv4 dedicadas,
una por base, exclusivas de ese cliente. Y la mitad IPv6 de ese add-on **ya
viene entregada desde v0**, porque cada VM tiene sus dos IPv6 exclusivas por
construcción.

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

Dos direcciones **internas** que el invitado lleva puestas y no cambian nunca,
y dos **públicas** por familia que dependen de qué base te esté sirviendo:

| rol | dirección | quién lo ve |
|---|---|---|
| **Identidad v6** (dentro de Windows) | `<IDENT>::<vmid>` — un /64 **global nuestro que no anunciamos** | el invitado, su nodo y las bases |
| **Identidad v4** (dentro de Windows) | una IPv4 de `10.0.0.0/8` **única en la flota**, fija por VM (§4.4) | el invitado, su nodo y las bases |
| **Pública v6 por b0** | `<b0_/64>::<vmid>` — el /64 **propio de b0** | Internet |
| **Pública v6 por b1** | `<b1_/64>::<vmid>` — el /64 **propio de b1** | Internet |
| **Pública v4** | la **IP principal** de la base que sirve (compartida) | Internet |

El sufijo es **siempre el `vmid`** en todas las IPv6. Eso ya es la convención
actual (`expected = f"{short_prefix64_str(dst_ipv6)}{vmid:x}"`), sólo cambia de
dónde sale el prefijo. La identidad IPv4 sigue el mismo principio — fija,
puesta una vez, viaja con la VM — aunque no comparta el esquema de sufijo.

⚠️ **Las públicas NO salen de los /64 failover** (decisión de la ronda 2). Si
salieran de ahí, el nodo tendría que traducir hacia el bloque que la base
**posea en ese momento**, y eso reintroduce la dependencia del estado de
propiedad de las VIPs que precisamente queremos quitar. Saliendo del /64 propio
de cada base, la base traduce hacia el suyo y no hay nada que consultar.

**Consecuencia**: cada VM tiene **dos** direcciones IPv6 públicas, no una, y
cuál se usa depende de la base. Entra por las dos (§4.3), sale por la de su
base local (§4.4). Las dos se le enseñan al cliente (§1).

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

### 4.2 Nodo — transporte (sin traducción, sin conntrack)

**El nodo no traduce nada.** Su configuración de red para este diseño:

1. **Dos túneles `ip6gre`** con nombre predecible (`tun-b0`, `tun-b1`), a la IP
   **principal** de cada base (§4.5).
2. **Rutas**: `<dirección de b0> → tun-b0`, `<dirección de b1> → tun-b1`, y
   **por defecto → el túnel de su base local**.
3. **MSS clamping** en los dos túneles.
4. **Un watchdog de enlace** (§4.2bis). Esto sí es estado, y es obligatorio.

Ni tabla `nat`, ni NPTv6, ni marcas, ni conntrack IPv6.

#### 4.2bis ⚠️ GRE es SIN ESTADO: el túnel no se cae cuando la base muere

Detectado el 2026-08-13. `ip6gre` es pura encapsulación: no hay sesión, no hay
negociación, y **Linux no implementa los keepalives de GRE** (los de estilo
Cisco). Cuando la base peer se reinicia o cae:

- La interfaz del túnel **sigue UP**.
- La ruta por defecto **sigue apuntando ahí**.
- El nodo sigue metiendo paquetes en un agujero negro.

O sea que sin añadir nada, **no hay "delay": no hay recuperación**. El nodo se
queda sin salida hasta que la base vuelve, con el otro túnel vivo al lado.

**Esto lo resuelve el anclaje a las VIPs** (§4.5, decisión de la ronda 3): al
moverse la VIP, el túnel la sigue y el tráfico aterriza en la superviviente sin
que el nodo haga nada. Suelo de recuperación: la cadena de failover completa,
~5-6 min (detección 6×30 s + árbitro + 2-3 min de propagación de Hetzner).

**Mejora opcional, para después**: un temporizador que sondea las dos bases por
sus túneles y ajusta la métrica de la ruta por defecto. Baja los 5-6 min a
~15 s, porque en operación normal cada túnel termina en una base distinta y el
nodo puede irse al de la otra región sin esperar a la VIP. Es **por túnel, no
por VM**: el mismo script en los 235 nodos, sin nada que crezca con la flota, y
**aditivo** — se puede montar más adelante sin rediseñar nada.

Descartado un demonio de enrutado (BGP/BFD con FRR): para dos vecinos fijos es
desproporcionado, y añade una superficie operativa grande en 235 hipervisores.

⚠️ **Caso que el anclaje a VIP NO cubre: base viva pero degradada.** Es lo del
2026-07-16 — la base con el conntrack lleno tirando paquetes mientras seguía
contestando. El watchdog no mueve la VIP porque la base está viva, así que la
salida no se recupera. El sondeo del nodo *podría* pillarlo, pero tampoco está
garantizado: si la base descarta tráfico de cliente pero contesta al sondeo, se
le escapa igual.

#### ⚠️ Métricas DISTINTAS en las dos rutas por defecto — nunca iguales

Con una sola ruta activa no hay duplicación de tráfico: el kernel elige un
camino. **Pero si las dos quedan con la misma métrica, Linux hace ECMP y reparte
por flujo.** Eso no duplica, **divide**: las conexiones salientes del cliente
saldrían unas por la IP de una base y otras por la de la otra, alternando por
conexión. Una prop firm vería la IP bailando dentro de la misma sesión.

Fallo silencioso y facilísimo de diagnosticar mal. **Métricas explícitamente
distintas**, y comprobado en la validación (§8).

⚠️ **Esto sustituye a la tabla `ip6 nvxlat` de la versión anterior**, que hacía
NPTv6 en el nodo con `snat/dnat prefix to`. Se retira por tres motivos:

- **Aquello NO era sin estado.** Las cadenas `type nat` de nftables van sobre
  conntrack por definición. La versión del 08-11 decía "cero conntrack, nada que
  reconstruir tras migrar" y no se sostenía.
- Al vivir la traducción en la base, y **no cambiar de base en una migración
  intra-región**, el conntrack nunca se mueve. Desaparece toda la dependencia
  del rescate de flujos a media conversación (`tcp_loose`, trato de los
  `INVALID`) — **el riesgo #9 se cae entero**.
- No hay que instalar ni mantener reglas nft en 235 hipervisores, ni verificar
  que sobreviven a cada actualización de PVE. Y **el riesgo #2 también se cae**:
  ya da igual si `dnat prefix to` existe.

Que la traducción se pueda hacer en la base es consecuencia directa de que las
públicas salgan del /64 propio de cada base (§4.1): la base traduce hacia el
suyo y no necesita saber nada de las VIPs.

### 4.3 Base — traducción, rutas por VM y entrada por las dos

La base hace **todo** el trabajo de direccionamiento:

- **Traducción v6**: `<IDENT>::<vmid>` ⇄ `<su_/64>::<vmid>`. Prefijo fijo,
  sufijo conservado.
- **Traducción v4**: IPv4 privada de la VM ⇄ IP principal de la base (§4.4).
- **Rutas por VM**: `<IDENT>::<vmid>/128` y la `/32` de su IPv4 privada, las dos
  hacia el túnel del nodo que la aloja. Son **el único estado por-VM que cambia
  al migrar**: dos actualizaciones de ruta, atómicas, instantáneas y **sin tocar
  el invitado**.
- **Mapa de puertos del RDP/SMB/SSH**: `20000+vmid → <IDENT>::<vmid>` etc. Como
  el sufijo es el vmid y el prefijo es fijo, **el mapa se vuelve estático para
  siempre**: se genera una vez y no se reescribe en ninguna migración. Lo que se
  mueve es la ruta.

#### Entrada por las DOS bases — se mantiene, y no es negociable

Hoy las dos bases llevan el mapa completo de la flota (1.865 entradas cada una)
y **todas las VMs responden por las dos VIPs** (§2, puerto 20959 verificado).
Eso se conserva. Cuesta ~1.850 rutas `/128` por base, que para Linux no es nada,
y sostiene dos cosas que sí importan:

1. **Es la red de seguridad del TTL del DNS.** El `trading-<vmid>` tiene TTL 60,
   así que tras una migración cruzada el nombre viejo aún resuelve a la VIP
   anterior durante un rato — más con la caché DNS de Windows, que es pegajosa,
   y con los `.rdp` guardados. Hoy eso **funciona** con +25 ms. Sin ello, cada
   migración cruzada pasa a tener hasta un minuto de conexión rechazada.
2. **conncheck depende de ello.** El prober de cada base sondea **toda la flota
   a través de la IPv4 principal de la base peer** — sondear la propia IP se
   salta el hook de prerouting donde vive el DNAT y no prueba nada. Si cada base
   llevara sólo su región, todas las VMs fuera de región darían inalcanzable y
   habría que rediseñar el reconciliador.

**El retorno asimétrico** (cliente entra por la base que no es la local del
nodo) lo resuelve la regla que **ya está en producción**:
`ct status dnat snat to <ipv6 principal de la base>`. Al poner la base su propia
dirección como origen, la respuesta del invitado va dirigida a esa base
concreta, y al nodo le basta con enrutar por destino. Sin marcas, sin estado en
el nodo.

⚠️ El peaje es que el invitado ve la dirección de la base, no la IP real del
cliente. Para RDP/SMB/SSH **eso ya pasa hoy**, así que no cambia nada; sólo
cambiaría para quien tenga `ipv6Enabled` y reciba conexiones directas. Ver §4.6:
preservar la IP real es v1 y va atado al traslado del firewall.

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
A → nodo → túnel → **base** → túnel → nodo de B → B. Es un rodeo.

#### ⚠️ REGRESIÓN DE AISLAMIENTO — regla obligatoria en v0

Detectada el 2026-08-13 leyendo `first_boot.sh:617`. Las reglas del firewall
por-VM van **por `/64`, no por `/128`**:

```
[IPSET base]                         ← contiene los /64 de LAS DOS bases
[group vm-default]
IN RDP(ACCEPT)  -source +dc/base     ← RDP abierto a todo el /64 de la base
IN SMB(ACCEPT)  -source +dc/hosts-ipv6   ← SMB abierto a los /64 de nodo
```

**Hoy**: el tráfico VM↔VM va directo nodo a nodo, así que la VM destino ve el
/64 del **nodo** origen, que está en `hosts-ipv6` — y ese ipset concede **sólo
SMB**. RDP entre clientes está cerrado.

**Con el diseño nuevo**: el tráfico VM↔VM pasa por la base y llega con la
dirección de la base como origen, que está en `+dc/base` — y `base` **sí
concede RDP**. Resultado: **un cliente podría abrir RDP contra la VM de otro**,
con sólo la autenticación de Windows por medio.

**Arreglo, en la base y desde v0**: el tráfico procedente de otra VM llega por
un **túnel**, no por `enp6s0`. Es una distinción de primera clase en la base y
trivial de expresar; en el nodo es indistinguible, porque las dos cosas llegan
con la dirección de la base. Regla que impida el tráfico entre túneles hacia
puertos de servicio, **puesta en la base, no en el firewall por-VM del nodo**
(§4.6).

Esto es además el argumento más fuerte para el traslado del firewall a la base
(§4.6, v1): no es sólo una simplificación, es donde la política se puede
expresar correctamente.

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
- **Estática dentro de Windows, y se ELIMINA el DHCP.** Es el modelo que el
  IPv6 ya usa hoy: `first_boot.sh:110-114` — *"IPv6: NOTHING. No RA, no DHCPv6.
  Each VM is configured in-guest with a static `<prefix>::<vmid_hex>` + manual
  default route + manual DNS via the legacy netsh `store=persistent` path.
  Both DHCPv6 client and RouterDiscovery are persistently DISABLED"*. Se pone
  en la provisión y en el `reset_vm` (que vuelve a correr el instalador), y no
  se toca nunca más. Como **dnsmasq en el nodo no hace nada más que DHCP
  IPv4**, desaparece entero: un servicio menos por nodo. El `dhcp-option=3`
  (gateway) y el `dhcp-option=6` (resolvers `185.12.64.1/.2`) pasan al
  invitado, donde el IPv6 ya los tiene.
  ⚠️ **Descartada la reserva DHCP por MAC** (decisión del 08-13 revisada el
  mismo día): con direccionamiento por VM, una reserva es **estado por-VM en el
  nodo** — al migrar habría que escribirla en el dnsmasq del destino y recargar
  el servicio, que es justo el estado por-nodo que el diseño elimina, y más
  frágil que actualizar una ruta. La configuración estática pone la identidad
  donde corresponde: con la VM.
- **Puerta de enlace `10.0.0.1` idéntica en todos los nodos**, con máscara
  ancha y `proxy_arp` en el bridge, para que el invitado no necesite saber en
  qué nodo está y no haya que tocarlo nunca.

- **La base guarda una ruta `/32` por VM** hacia el túnel del nodo que la
  aloja — mismo objeto y mismo momento que la `/128` de §4.3.
- **SNAT normal de netfilter en la base hacia su IP PRINCIPAL**, no hacia la
  failover (decisión de la ronda 2, §1). Sin traducción entre familias, así que
  el techo de puertos de §3.2 sigue siendo **por destino** y el arreglo
  `time_wait 120 → 30` sigue aplicando tal cual.
  - La IP principal **no se mueve nunca**: está atada a la máquina. Así que la
    salida funciona en cuanto el nodo elige túnel, **sin esperar a que Hetzner
    propague nada**. Desaparece la ventana de 2-3 min en el tráfico saliente.
  - Y **no hay nada compartido con la entrada**: failover para entrar, principal
    para salir. Cada mecanismo hace una sola cosa. Esto retira la restricción de
    rango de puertos del SNAT que se propuso durante la ronda 2 — al ser
    direcciones distintas no hay solape que evitar, y se conserva el rango
    completo de 55.296 puertos.
  - 🔲 La regla va **en su propia cadena**, no como línea suelta, para que en v1
    "la base local no hace SNAT" sea insertar una excepción y no reescribirla
    (§4.6).
- `nf_conntrack_tcp_timeout_time_wait` = **30 s**.
- **`insert_failed` monitorizado** (§3.3).
- El MASQUERADE del nodo **se queda presente pero inactivo**, como ruta de
  rollback por nodo con un solo flip.

**Lo que hay que atender al quitar el DHCP:**

1. **La provisión pasa a ser sensible al orden.** Hoy el DHCP da IPv4 en el
   arranque, así que el instalador puede descargar (`install_openssh.ps1` tira
   de `files-fsn`/`files-hel`) antes o después de tocar la red. Con todo
   estático, **lo primero** que hace el instalador es configurar las
   direcciones — y con la regla de oro: **el éxito se MIDE, no se supone**
   (`BOUND:` devuelto Y el puerto contestando). Ver el caso vm 1023 del
   2026-08-02: un exec devolvió exit 0 sin aplicar nada.
2. **Una inyección fallida deja la caja sin red ninguna**, no a medias. No nos
   deja fuera: el guest agent habla por virtio-serial, no por red, y queda la
   consola VNC. Pero conviene tenerlo escrito antes de que pase.
3. **Se pierde el autocurado del DHCP** (cliente que hace `netsh int ip reset`,
   reinstala el adaptador o restaura una copia). 🔲 **Arreglo: extender a IPv4
   la detección de deriva y el auto-fix de conncheck**, que ya existe y está
   validado E2E para IPv6 (vm 1985, 21 s de detección a reparación). Queda
   mejor que el DHCP porque conncheck **verifica** que ha funcionado.

**Superficie de cambio (verificada 2026-08-13):** sólo **tres sitios** tocan
esto en los dos repos — `install.sh:620` (dirección de vmbr),
`install.sh:625/627` (MASQUERADE `-s 10.0.0.0/16`) y el bloque entero de
dnsmasq de `first_boot.sh:89-117`, duplicado en
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

**Dos túneles por nodo, anclados a las DOS VIPs de failover** (no a las IPs
principales). `tun-hel` → VIP de Helsinki, `tun-fsn` → VIP de Falkenstein.
Ambos siempre arriba.

**El emparejamiento nodo↔base es por VIP, no por máquina**, y eso es lo que hace
que se resuelva solo: cada base lleva **un único juego** de túneles, sourced de
la VIP que posee (b1 con `local = VIP de Helsinki`, b0 con `local = VIP de
Falkenstein`). Cuando una VIP se mueve, los túneles de los nodos la siguen sin
tocar nada, y **la base superviviente no reconfigura nada**: sigue usando su
propio juego, que alcanza todos los nodos igual.

⚠️ **UN SOLO túnel a la VIP de su región NO vale** (se evaluó y se descartó):
entonces sólo la base que posee esa VIP alcanza ese nodo, y eso rompe la entrada
cruzada que §2 tiene verificada — el puerto `20959` de una VM de Helsinki
responde por las **dos** VIPs — además de dejar sin camino al sondeo cruzado de
conncheck (§4.3). Con **dos** túneles, uno por VIP, cada base alcanza todos los
nodos por el túnel de la VIP que posee y las dos propiedades se conservan.

**Por qué a las VIPs y no a las IPs principales** (decisión de la ronda 3, tras
comparar las dos):

| | anclado a IP principal | anclado a failover ✅ |
|---|---|---|
| **sin** sondeo en el nodo | **nunca se recupera** — el túnel apunta a una caja muerta para siempre | se recupera al moverse la VIP (~5-6 min) |
| **con** sondeo en el nodo | 15 s | 15 s |
| apagar una base | drenaje que flipea métricas + mover VIPs | **sólo mover las VIPs** |

El anclaje a la failover es **estrictamente mejor**: funciona sin sondeo, y el
sondeo pasa a ser una **mejora opcional** en vez de un requisito. Con las IPs
principales el sondeo es obligatorio porque sin él no hay recuperación de
ninguna clase.

**El sondeo es ORTOGONAL al anclaje.** Incluso anclando a las VIPs, en operación
normal cada túnel termina en una base distinta, así que un nodo que vea su túnel
muerto puede irse al de la otra región —que llega a la superviviente— y
recuperar en ~15 s sin esperar a que la VIP se mueva. **Se arranca sin sondeo**,
con los 5-6 min como suelo, y se añade después si la primera caída real duele.
Es aditivo y no cambia el diseño.

⚠️ **Asimetría a saber**: tras una conmutación las dos VIPs viven en la misma
caja, así que los dos túneles del nodo terminan en el mismo sitio y el sondeo se
queda sin alternativa independiente. Sólo importa si la superviviente también
cae —escenario sin salida de todas formas— pero no hay que creerse que queda
redundancia donde ya no la hay.

**Tecnología: `ip6gre`.**

| | overhead | familias | coste operativo |
|---|---|---|---|
| **ip6gre** ✅ | 44 B → MTU 1456 | **las dos en un túnel** | nulo: kernel, sin claves, sin demonio |
| ip6tnl `mode any` | 40 B → 1460 | las dos | nulo, pero menos común y peor documentado |
| WireGuard | ~80 B → 1420 | las dos | **470 pares de claves** + CPU de cifrado |
| VXLAN | 50 B | L2 | innecesario, queremos L3 |

GRE lleva las dos familias en un solo túnel por el campo de tipo de protocolo,
es de kernel y sin estado. A 235 nodos × 2 bases son **470 túneles**, y ahí la
diferencia entre dos comandos `ip link` y gestionar 470 pares de claves con su
rotación lo decide todo. El cifrado compra poco: es tráfico entre dos máquinas
nuestras dentro de la red de Hetzner, y el del cliente ya va cifrado por encima.

✅ **Prueba del día 0 — HECHA 2026-08-13: Hetzner SÍ pasa GRE.** Túnel
`ip6gre` entre el nodo `0000228` y b0, **0 % de pérdida y 0,51 ms de RTT**.

#### ⚠️ TRAMPA: `ip6gre` mete una cabecera DSTOPT y rompe el firewall

La trampa que casi hace descartar GRE por un motivo falso. Por defecto `ip6gre`
añade una cabecera de extensión **Destination Options** con el límite de
encapsulación, así que el `nexthdr` de la cabecera IPv6 fija es **60, no 47**:

```
2a01:4f8:2240:201f::2 > 2a01:4f8:2b03:18a9::2: DSTOPT GREv0, length 108
```

Una regla `ip6 nexthdr gre accept` **no casa** (contador medido: 0 paquetes) y
todo el túnel muere en el `policy drop` de la base. **Y el cuadro de síntomas es
engañosísimo:**

| se ve | realidad |
|---|---|
| túnel `UP,LOWER_UP` en los dos extremos | ✅ correcto |
| `local`/`remote` bien puestos | ✅ correcto |
| contador TX del nodo **sube** con cada ping | ✅ el nodo transmite |
| `tcpdump` en la base **ve los paquetes en el cable** | ✅ Hetzner los entrega |
| contador **RX del túnel en la base: 0** | ❌ el firewall los tira antes |

Todo verde por los dos lados y ni un paquete atravesando. Sin bajar al `tcpdump`
lo natural habría sido concluir "Hetzner bloquea GRE" y tirar el diseño.

**Arreglo: `encaplimit none`.** ⚠️ Y no se puede cambiar en caliente —
`ip -6 tunnel change ... encaplimit none` **lo acepta sin error y NO lo aplica**.
Hay que **borrar y recrear** la interfaz:

```bash
ip link add name tun-b0 type ip6gre local <nodo> remote <base> encaplimit none
```

De regalo se recuperan 8 bytes de MTU: de 1448 a **1456**.

⚠️ **La base necesita además una regla de entrada para GRE**: su cadena `input`
tiene `policy drop` y no contempla el protocolo 47 (`nft list chain inet filter
input`). Sin ella no entra nada aunque el túnel esté perfecto.

### 🏁 CADENA COMPLETA VALIDADA — vm 1096 sin dependencia del nodo (08-13)

Estado final alcanzado sobre `0000228` + b0/b1:

```
gw=[fe80::1]  v6=[2a01:4f9:c01f:e::448]  v4=[10.64.4.72]
salida6=2a01:4f8:2b03:18a9::448        ← traducida en la base al /64 de b0
RDP 21096 y SSH 31096 ABIERTOS por las DOS VIPs
```

**La IPv6 del invitado ya no contiene el prefijo de su nodo.** Entra por las dos
regiones y sale traducida. Es la prueba de que el modelo funciona.

#### Los CINCO eslabones ocultos, en orden de aparición

Ninguno estaba en el diseño y cada uno rompía la cadena entera en silencio:

**1. GRE hay que permitirlo en LOS DOS extremos.** Se lo puse a las bases y no
al nodo, cuyo `cluster.fw` tiene `policy_in: DROP`. Regla:
`IN ACCEPT -source +dc/base -p gre`.
⚠️ **El síntoma es direccional e intermitente**: un flujo iniciado por el NODO
abre conntrack y la vuelta pasa, así que "a veces funciona". Base→nodo falla
siempre hasta poner la regla.

**2. Las direcciones de tránsito del túnel deben estar en el ipset `base`.**
Tras el cambio la base alcanza al invitado desde su dirección **de túnel**, no
desde su principal, y el firewall por-VM concede RDP/SMB/SSH a `+dc/base`. Sin
esto el invitado descarta todo. Añadido `2a01:4f9:c01f:e:ffff::/112`.

**3. La cadena `forward` de la base sólo deja salir por `enp6s0`.**
`iifname "veth-host" oifname "enp6s0" accept` — el tráfico que sale hacia un
**túnel** cae en el `policy drop`. Hace falta `oifname "tun-*"` (y el simétrico
`tun-*` → `enp6s0` para la salida).

**4. El SNAT de los flujos DNAT'd debe ser la dirección de TRÁNSITO, no la
principal.** La regla global `ct status dnat snat to <principal>` hace que el
invitado responda a la principal de la base; el nodo enruta esa dirección por su
**uplink**, y Hetzner tira el paquete porque lleva un origen que no pertenece a
ese servidor. Se inserta una regla **más específica y delante**, acotada al
prefijo IDENT, y **la global se queda intacta** para las VMs del modelo viejo.

**5. ⚠️ La regla de política del nodo debe acotarse por interfaz de entrada.**
`ip -6 rule add from <IDENT>/64 lookup ident` parece correcto y crea un **bucle**:
las direcciones de tránsito viven **dentro** del IDENT /64, así que el tráfico
que llega **de la base** también casa y el nodo lo devuelve por el túnel en vez
de entregarlo al invitado. El NDP resuelve bien y aun así **cero paquetes llegan
al bridge**. Arreglo: `... iif vmbr0 lookup ident`.
🔲 **Mejor a futuro**: sacar el tránsito FUERA del prefijo IDENT y el problema no
existe. Con un solo /64 flotante se resolvió acotando por interfaz.

#### Orden de operaciones que funciona

1. Túneles a **las dos** bases + reglas GRE en los tres extremos.
2. Rutas `/128` del IDENT en las dos bases → su túnel; y en el nodo → `vmbr0`.
3. **Añadir** la IDENT al invitado (sin quitar la vieja).
4. Cambiar `servers.ipv6` → la GCF empuja el mapa a las dos bases (medido:
   ambas actualizadas en segundos).
5. SNAT específico + reglas de forward en las bases; regla de política en el nodo.
6. **Sólo entonces**, quitar la vieja del invitado — y **de los dos almacenes**.

### 🏁 Salida IPv4 por la base — invitado Y host del nodo (08-13)

**Invitado.** `salida4` de la vm 1096 pasa de `188.40.145.216` (la del nodo) a
**`188.40.153.120`** (la de b0). Piezas:

- Nodo: `ip rule from 10.64.0.0/16 iif vmbr0 lookup ident4` → `default dev tun-b0`.
- Base: ruta `/32` de vuelta + `ip saddr 10.64.0.0/16 iifname "tun-*" oifname
  "enp6s0" snat to <ipv4 de la base>`, **añadida al final** de la cadena.
- El MASQUERADE del nodo **no estorba**: casa por `-o <uplink>`, y este tráfico
  sale por el túnel. Se queda como rollback de un solo comando.

**Aislamiento verificado**: la regla de la base está **doblemente acotada** —
origen `10.64.0.0/16` **y** entrada por `tun-*`. Ningún cliente cumple ninguna
de las dos (los suyos son `10.0.x.x` y entran por `enp6s0`), y va detrás de las
reglas existentes sin tocarlas.

**Host del nodo** — necesario antes de dar de baja la IPv4 en Hetzner, o `apt`
y los instaladores dejan de funcionar (riesgo #5).

⚠️ **El host NO puede salir con `10.64.255.1`**: esa es la puerta de enlace de
los invitados, **idéntica en todos los nodos**, así que la base no podría saber
a qué túnel devolver el retorno. Es la misma colisión que el `10.0.0.0/16`.

**Esquema para nodos: `10.65.<node_hi>.<node_lo>`** — un `/16` aparte del de las
VMs, mismo criterio de legibilidad. Nodo 228 → `10.65.0.228`.

```bash
ip addr replace 10.65.0.228/32 dev lo
ip route replace default dev tun-b0 src 10.65.0.228 table host4
ip rule add from 10.65.0.228 lookup host4 priority 102
```

**Simulada la baja de la IPv4** poniendo la ruta por defecto del nodo en el
túnel:

| prueba | resultado |
|---|---|
| `curl https://api.ipify.org` desde el host | **`188.40.153.120`** (la de b0) |
| `raw.githubusercontent.com` | HTTP 200 |
| **`apt-get update`** | **OK** |
| RDP de la VM por las dos VIPs | abiertos |

**La IPv4 pública del nodo ya no la necesita nadie.** Después se devolvió la
ruta por defecto al uplink: el estado validado se alcanza con **un comando**, y
como no está persistido, un reinicio del nodo vuelve solo al camino seguro.

🔲 Para la baja real: llevar la ruta por defecto y el túnel a `install.sh`, y
**no** quitar el MASQUERADE hasta tener recorrido.

### Nodo de Helsinki preparado — 4 túneles en pie (08-13)

`0000238` queda con la misma configuración que `0000228`: túneles a **las dos**
bases, reglas GRE en los tres extremos, tránsito en el ipset `base`, y enrutado
por política para IDENT (v6) y `10.64.0.0/16` (v4).

⚠️ **`/etc/iproute2/rt_tables` no existe en todos los nodos.** En `0000228` sí y
en `0000238` no, así que `ip route ... table ident` falla en silencio (sin
`set -e` el script sigue y parece que funcionó). **Usar identificadores
NUMÉRICOS** (`table 100`) en cualquier script de flota, que no dependen de ese
fichero.

⚠️ **`ping` a un NODO no sirve como prueba de túnel.** El firewall de PVE no
acepta ICMPv6 echo entrante, así que el nodo nunca contesta aunque el túnel esté
perfecto — y da 100 % de pérdida en las dos direcciones tras el primer intento.
Se pierde media hora persiguiendo un túnel sano. **La prueba válida es alcanzar
un servicio real del invitado** (`nc -6 -s <transito> -z <IDENT> 3389`).

### 🔲 SIGUIENTE: la migración en caliente — requiere TOCAR `migrate_vm.sh`

No es "lanzar el script y mirar". `migrate_vm.sh:1235-1240` calcula:

```bash
EXPECTED_VM_IPV6="<prefijo_del_nodo_destino><vmid>"
DST_GATEWAY="${EXPECTED_VM_IPV6%::*}::1"
```

y después **entra en el invitado por el guest agent a reescribir esa dirección y
esa puerta de enlace**. Sobre una VM del modelo nuevo eso le **machacaría** su
IDENT y su `fe80::1`, dejándola con una dirección derivada del nodo — justo lo
contrario de lo que persigue el proyecto, y con el cliente dentro.

Así que la migración exige antes el cambio de código de §6: que `migrate_vm.sh`
detecte que la VM está en el modelo nuevo y **se salte** todo el bloque del
guest agent, sustituyéndolo por **mover las rutas `/128` y `/32` en las dos
bases**. Eso es una modificación del script más crítico de la flota y merece su
propia sesión con pruebas, no una improvisación.

**Lo que ya está listo para esa sesión**: los cuatro túneles, las dos VMs, los
dos nodos preparados y el camino de datos validado de punta a punta.

---

## 13. ESTADO ALCANZADO — los dos nodos de prueba en el modelo FINAL

Cerrado el 2026-08-14 de madrugada. `0000228` (FSN) y `0000238` (HEL) están
**exactamente como quedará la flota**, y ambos **sin IPv4 pública**.

| | 0000228 (FSN) | 0000238 (HEL) |
|---|---|---|
| IPv4 pública del nodo | **ninguna** | **ninguna** |
| Salida IPv4 del host (`apt`, GitHub) | `188.40.153.120` (b0) | `37.27.135.250` (b1) |
| VM | 1096 | 1097 |
| IPv6 del invitado | `2a01:4f9:c01f:e::448` | `2a01:4f9:c01f:e::449` |
| IPv4 del invitado | `10.64.4.72` | `10.64.4.73` |
| Puerta de enlace | `fe80::1` | `fe80::1` |
| Salida IPv6 | `2a01:4f8:2b03:18a9::448` | `2a01:4f9:3070:3984::449` |
| Salida IPv4 | `188.40.153.120` | `37.27.135.250` |
| **RDP por las DOS VIPs** | ✅ | ✅ |

**Ninguna dirección del invitado depende del nodo.** Todo persiste a reinicios
de VM y de nodo vía `neuravps-tunnels.service` + `/etc/default/neuravps-tunnels`.

### Piezas del diseño que sólo aparecieron al montarlo

**Una sola dirección de SNAT por BASE, no una por túnel.** La base pone SIEMPRE
la suya como origen del tráfico DNAT'd, así que **cada nodo debe enrutar las dos
direcciones canónicas** (`::` de b0, `::2` de b1) por su túnel correspondiente.
Con `/127` por túnel, el nodo sólo conocía los suyos y devolvía por el equivocado
→ **la entrada cruzada fallaba**. Síntoma: RDP OK por la VIP local y cerrado por
la otra.

**El `/64` de identidad se enruta ENTERO a `vmbr0`**, no una `/128` por VM. Quita
todo el estado por-VM del nodo, y no interfiere con el tráfico saliente del
invitado porque de eso se encarga la regla de política (`from IDENT iif vmbr0`).

**`snat ip6 prefix to` funciona en la base** — una sola regla traduce
`<IDENT>::<vmid>` → `<base>::<vmid>` conservando el sufijo, en vez de una regla
por VM. Verificado en producción.

**Direccionamiento de los nodos: `10.65.<node_hi>.<node_lo>`.** El host NO puede
salir con `10.64.255.1` (la puerta de enlace de los invitados, idéntica en todos
los nodos): la base no sabría a qué túnel devolver el retorno. Es la misma
colisión que el `10.0.0.0/16`, repetida en otro sitio.

### Trampas operativas que costaron tiempo

- **`fwd` es palabra reservada de nftables** (el statement `fwd to <dev>`). Una
  cadena llamada `fwd` da errores de sintaxis en las líneas SIGUIENTES, no en la
  suya. Renombrada a `clamp`.
- **Un `exit 0` al final de un script deja fuera todo lo que se le añada
  después.** Silencioso.
- **Esperar a que el SSH conteste NO prueba que un nodo haya reiniciado**: sigue
  contestando mientras se apaga. Verificar contra un **uptime pequeño**, o se
  comprueba el arranque viejo y se dan por buenas cosas que no se han aplicado
  (y por rotas otras que están bien).
- **Estos AX162 tardan varios minutos en volver** (import de ZFS). "No responde"
  no es "caído".
- `/etc/iproute2/rt_tables` no existe en todos los nodos → **tablas numéricas**.

## 14. Firewall de flota — DESPLEGADO 2026-08-14

`sync-base-nat.py sync nodes sync-firewall`: **234 nodos, 0 avisos, 0 fallos**,
~8 min secuenciales. Canario de 6 clientes reales (3 por región) **6/6 en todas
las rondas, cero fallos**. Verificado después en 5 nodos al azar: los tres
cambios presentes y aplicados en `ip6tables`.

Los tres cambios, **todos aditivos y compatibles con los dos modelos**:

| cambio | para qué | modelo viejo |
|---|---|---|
| `2a01:4f9:c01f:e:ffff::/112` en `[IPSET base]` | la base alcanza al invitado desde su dirección de túnel, no la principal | intacto |
| `[IPSET vm-ident]` + `IN SMB(ACCEPT) -source +dc/vm-ident` en los dos grupos | VM↔VM del cliente (MT→SQX) con el tráfico pasando por la base | intacto |
| `IN ACCEPT -source +dc/base -p gre` | los túneles; sin ella el nodo tira el GRE por `policy_in: DROP` | intacto |

⚠️ **`vm-ident` concede SOLO SMB**, nunca RDP — igual que `hosts-ipv6` hoy.
Concederle RDP reabriría la regresión de aislamiento (§4.3bis).

Procedimiento seguido, por si hay que repetirlo: canario primero → transformar
la canónica **con asserts** → `diff` → **validar compilando en un nodo real con
vuelta atrás** → subir al Storage Box → verificar idéntica → push → muestra.

**Dos interfaces con nombre predecible, no un túnel multiplexado.** Un solo
túnel con dos direcciones sería igual de funcional y más barato de montar, pero
`iifname` es el discriminador que hace que las marcas de v1 sean seis líneas
(§4.6).

**MSS clamping desde el día 0** (`tcp option maxseg size set rt mtu`, que se
ajusta solo). ⚠️ Eso arregla TCP, **no UDP** — y RDP usa transporte UDP. Hace
falta una prueba explícita de sesión RDP con UDP activo a través del túnel.

**El túnel lleva las dos familias**: el IPv4 del invitado va 4in6, así que el
nodo no necesita IPv4 propia para dar salida IPv4 a sus invitados. Eso resuelve
de paso el problema del **host**: el `apt` y los `curl` del propio PVE usan la
misma tubería el día que se le quite la IPv4 pública.

**El SNAT no depende del túnel de entrada.** En la versión de la ronda 1 la base
deducía la VIP de salida de por qué túnel llegaba el paquete. Con la salida por
la IP principal eso ya no hace falta: la base usa siempre la suya. **Se cae el
riesgo #10 entero** (reconciliar la regla de SNAT tras una conmutación).

**Ventana residual en la salida: ninguna.** Antes había 2-3 min esperando a que
Hetzner propagara la VIP; con la IP principal la salida funciona en cuanto el
nodo cambia de túnel. La ventana sigue existiendo **para la entrada**, que es
donde vive el failover y donde siempre ha existido.

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

### 4.6 Criterio de diseño y hoja de ruta

**LA REGLA: toda la política vive en la BASE. El nodo es transporte y el
invitado es un extremo tonto.**

Ante cualquier bifurcación de v0, la pregunta es *"¿esto deja política o estado
en algún sitio que no sea la base?"* — si la respuesta es sí, la otra opción.
No es purismo: v1 consiste precisamente en mover el firewall a la base y
preservar la IP real del cliente, y cada atajo de v0 que ponga lógica en el nodo
convierte eso en una migración de lógica en vez de en un borrado.

Las cinco bifurcaciones concretas ya identificadas, resueltas:

| bifurcación | opción barata | la que elegimos | por qué |
|---|---|---|---|
| Regla anti-tráfico-entre-túneles (§4.3bis) | en `vm-default` del nodo | **en la base** | v1 = borrar reglas del nodo, no mover lógica |
| Túneles | uno multiplexado | **dos con nombre** (`tun-b0`/`tun-b1`) | `iifname` es el discriminador de las marcas de v1 |
| Regla de SNAT (§4.4) | línea suelta | **cadena propia** | v1 inserta una excepción arriba en vez de reescribir |
| Direcciones del invitado | horneadas en la plantilla | **script re-ejecutable desde Firestore** | lo necesita el auto-fix de conncheck, y hace de v1 un barrido |
| IPv4 privada de la VM | derivada del `vmid` en código | **campo en Firestore**, junto al `ipv6` | el add-on de IPv4 dedicada es otro campo, no una refactorización |

Dos más que se decidieron por otros motivos y apuntan aquí igualmente:
**direcciones por VM y no por nodo** (rutas `/32`, que es lo que permite política
por cliente después) y **traducción en la base y no en el nodo** (el nodo queda
sin estado, así que las marcas de v1 son una suma, no un cambio).

#### v0 — lo que se construye ahora

Camino de datos completo, comportamiento de firewall **igual que hoy**: el
firewall por-VM se queda en el nodo, la base sigue poniendo su dirección como
origen (`ct status dnat snat to …`) y el invitado sigue sin ver la IP real del
cliente. Comportamiento conocido y probado. Lo único nuevo en política es la
regla anti-tráfico-entre-túneles, y va en la base.

#### v1 — un solo paquete, después de que v0 esté estable

Estas tres cosas **son la misma pieza de trabajo** y no se pueden hacer por
separado:

1. **Firewall a la base.** Ya está medio hecho: `sync-base-nat.py` sólo crea la
   entrada del mapa si `rdpEnabled`/`sambaEnabled`/`sshEnabled` están activos, o
   sea que la base ya es el punto de aplicación y lo del nodo es redundancia.
   ⚠️ Excepción: el tráfico VM↔VM **dentro del mismo nodo** va por el bridge
   local y no llega a la base, así que hace falta una regla mínima de
   aislamiento entre invitados. "Abierto del todo" no puede ser literal.
2. **Preservar la IP real del cliente.** Requiere lo anterior, porque hoy el
   permiso de RDP es literalmente `-source +dc/base`: en cuanto el invitado vea
   la IP real, esa regla deja de casar y **el RDP se cae**. El control pasa a ser
   el mapa de puertos de la base.
   - El caso común sale gratis: **que la base local no haga SNAT**. La ruta por
     defecto del nodo ya apunta a ella, así que la respuesta vuelve sola. Cero
     cambios en el nodo.
   - Sólo el caso cruzado necesita algo: o SNAT únicamente en la base lejana, o
     marcas por túnel en el nodo (~6 líneas estáticas, por túnel y no por VM).
   - **Lo que se gana**: la detección de fuerza bruta pasa a ver atacantes
     reales (hoy los clientes v4 llegan colapsados en la VIP y la clave
     `(saddr . dport)` es en la práctica un límite por VM de destino), `bf_static`
     se puede alimentar de datos, y el cliente puede filtrar por origen. Para
     clientes IPv4 la IP real llega igualmente, codificada por Jool como
     `64:ff9b:1::<ipv4>`; hoy la borra el SNAT.
3. **Entrada directa IPv6 sólo por la base local.** El operador lo propuso como
   simplificación de v0; ahí no ahorra nada (la regla que sirve a las dos es una
   sola y ya está en producción), pero **aquí es la llave**: si sólo la base
   local acepta entrada directa, nunca hace falta SNAT para ese caso y la IP real
   se preserva sin tocar el nodo.
   ⚠️ Esto es **sólo** la entrada directa a la IPv6 pública (`ipv6Enabled`, hoy
   `false` en casi toda la flota). La entrada de **RDP/SMB por las dos VIPs** se
   mantiene siempre — ver §4.3, es la red de seguridad del TTL y conncheck
   depende de ella.

#### v1.1 — el add-on de IPv4 dedicada

Ver §7, fase 5. Y su interruptor de "abrir la IP entera", que va después y con
aviso, no como valor por defecto.

### 4.7 DNS — una URL por VM

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
| **Una IPv4 de salida por base**, no un pool | Con el perfil real de uso aguanta (§3.2). Simplicidad. Descartado explícitamente el pool en v0: más caro y más complejo, y el operador prefiere explicar la situación a las props, que lo aceptan porque somos un proveedor de VPS. |
| **La salida va por la IP PRINCIPAL, no por la failover** (08-13) | El requisito es un conjunto finito y enumerable, no una IP fija. La principal no se mueve nunca → la salida no depende de la propiedad de las VIPs y desaparece la ventana de propagación (§4.4). |
| **IPv6 pública desde el /64 propio de cada base** (08-13) | Misma lógica. Si saliera del /64 failover, el traductor tendría que saber quién posee qué (§4.1). |
| **Traducción en la BASE, no en el nodo** (08-13) | El nodo queda sin estado. Como una migración intra-región no cambia de base, el conntrack nunca se mueve: mueren los riesgos #2 y #9 (§4.2). |
| **Se mantiene la entrada por las DOS VIPs** (08-13) | Red de seguridad del TTL del DNS, y **conncheck depende de ello** para su sondeo cruzado. Cuesta ~1.850 rutas por base, que no es nada (§4.3). |
| **Toda la política vive en la base** (08-13) | Criterio para resolver las bifurcaciones de v0 de forma que v1 sea un borrado y no una migración de lógica (§4.6). |
| **Prefijo público atado al NODO**, no a la VM | La salida es siempre local → latencia mínima siempre. Se acepta el corte de conexiones al cruzar región. |
| **Se acepta el corte en migración cruzada** | Decisión explícita del operador: *"prefiero pagar un posible minuto de desconexión a tener latencia superior todo el tiempo"*. |
| **La región es DESEMPATE en el relief** | No es criterio de colocación: si el mejor destino está en la otra región, va igual. Sólo desempata entre destinos equivalentes, y convierte parte de los cortes diurnos en transparentes. Coste cero. |
| **Identidad en /64 global, NO ULA** | RFC 6724 en Windows (§4.1). |
| **IPv4 privada por VM, no por nodo** (08-13) | Un rango por nodo cambiaría la IPv4 del invitado en cada migración, y el IPv4 es donde vive el bróker → reintroduce el corte que el proyecto quita (§4.4). |
| **IPv4 del invitado ESTÁTICA in-guest; se elimina el DHCP** (08-13) | Es el modelo que el IPv6 ya usa. Una reserva DHCP sería estado por-VM en el nodo que habría que mover en cada migración — justo lo que el diseño elimina. Y dnsmasq desaparece del nodo (§4.4). |
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
- **Eliminar dnsmasq del nodo.** Su única función es el DHCP IPv4
  (`first_boot.sh:89-117`, "dnsmasq for IPv4 DHCP only"); el bloque está
  duplicado en `run_remotes/migrate_to_deterministic_ipv6.sh:212-213` —
  **los dos a la vez**.
- `proxy_arp` en el bridge.

### Instalador de invitado

- Configurar IPv4 **estática** (dirección, máscara, gateway `10.0.0.1`) junto a
  la IPv6, por el mismo camino `netsh … store=persistent` que ya se usa.
- Los resolvers que hoy reparte `dhcp-option=6` (`185.12.64.1/.2`) pasan a
  ponerse in-guest, donde el IPv6 ya los pone.
- **Primero la red, después todo lo demás** — hoy el DHCP hacía el orden
  irrelevante (§4.4).
- Deshabilitar el cliente DHCP de IPv4 en el adaptador, igual que ya se hace
  con DHCPv6 y RouterDiscovery, para que no compita con la configuración
  manual.

### `conncheck`

- 🔲 **Extender a IPv4** la detección de deriva in-guest y el auto-fix, que hoy
  sólo cubren IPv6. Es lo que sustituye al autocurado que daba el DHCP (§4.4).
- ⚠️ **No romper el sondeo cruzado**: el prober de cada base sondea toda la
  flota a través de la IPv4 principal de la base **peer**. Depende de que las
  dos bases lleven el mapa completo (§4.3).

### Firestore — esquema

- **Nuevo campo `ipv4` en `servers`**, junto al `ipv6` que ya existe: la IPv4
  privada única de esa VM. **No derivarla del `vmid` en cada sitio que la
  necesite** — las rutas de la base, el instalador, el futuro chequeo de deriva
  de conncheck y el panel tienen que leer todos la misma fuente. Con un campo,
  el add-on de IPv4 dedicada es otro campo y no una refactorización (§4.6).

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

- **Fase 1 → `0000228-AX162-2-LTD`** (Falkenstein, `2a01:4f8:2240:201f::2`,
  v4 `188.40.145.216`) + **b0**. 🧊 **Congelado el 2026-08-13.**
- **Fase 2 → `0000238-AX162-2-LTD`** (Helsinki, `2a01:4f9:3100:4b08::2`,
  v4 `65.109.148.185`) + b1. 🧊 **Congelado el 2026-08-13.**

⚠️ **Por qué NO es 0000227, y la lección.** El plan del 08-13 por la mañana
eligió 0000227, pero **el paso 0.A (congelar) no se ejecutó** — y en menos de 24
horas el nodo recibió **3 VMs de clientes**, dos creadas esa misma mañana
(08:59 y 10:04 UTC). En el mismo periodo la reserva de AX162 vacíos pasó de
**12 a 8** (se llenaron 227, 230, 231 y 233), dejando Falkenstein con sólo dos.

Lección, y por eso 0.A es el paso uno y no el cuatro: **la reserva de vacíos se
consume a ~4 nodos/día**. Congelar no es una precaución, es una carrera.

⚠️ **b0 va PRIMERO** (cambio del 08-13, antes era al revés). b0 sirve las 522
VMs de Falkenstein; b1 sirve las 1.343 de Helsinki. El paso con riesgo real en
una base es añadir la línea de `include` a `/etc/nftables.conf`, cuyo error de
sintaxis deja la base **sin firewall y sin DNAT** (§7.1 de la guía de Jool, ya
os pasó). Si el primer contacto sale mal, que sea sobre 522 y no sobre 1.343 —
y cuando le toque a b1, el procedimiento ya estará probado.
Mitigación en las dos: `nft -c -f /etc/nftables.conf` antes de aplicar, copia
conocida buena a mano, y la base peer sana para poder conmutar.

Se reservan **los dos desde el principio** para no tener que negociar el nodo
de la otra región a mitad de experimento. Quedan 10 vacíos de reserva.

### Qué toca cada fase (restricción del operador)

Hasta el final de la fase 2, **sólo se tocan los dos AX162 y las bases**. Todo
lo demás de la flota sigue con su MASQUERADE de nodo, su IPv6 derivada del nodo
y su DNAT de siempre, sin enterarse. Lo único que las bases hacen de más es
enrutar dos prefijos nuevos hacia dos túneles nuevos y una regla de SNAT que
sólo matchea eso — todo **aditivo**.

⚠️ **El renumerado de flota NO es prerrequisito de la validación.** A los dos
nodos de prueba se les dan rangos propios (`10.227.x`, `10.238.x`) que no
colisionan con el `10.0.0.x` del resto. El renumerado es prerrequisito del
**despliegue** (fase 3 en adelante), no del test — eso saca una acción de flota
entera del camino crítico.

**Secuencia acordada dentro de las fases 1-2:**

1. Congelar los dos nodos y crear 1-2 VMs de prueba **antes de tocar nada** —
   así hay línea base sobre la misma VM y RDP funcionando por el camino viejo
   mientras se trabaja. Con `maintenance: true` en su doc de `servers`.
2. Cambios en las bases, **una cada vez**, empezando por b0.
3. **Hito propio: el corte de salida.** Cambiar la ruta por defecto del rango
   del invitado hacia el túnel es el instante en que su internet pasa a depender
   de toda la cadena. Checkpoint separado, con rollback de un solo comando — que
   es para lo que el MASQUERADE del nodo se queda presente pero inactivo.
4. Documentar y aplicar las IPs estáticas en el invitado, **desinstalar
   dnsmasq**, reiniciar y verificar que persisten.
5. ⚠️ **Antes de pedir a Hetzner que quite la IPv4 del nodo**: el tráfico del
   propio host (`apt`, `curl` a GitHub, instaladores) tiene que ir ya por el
   túnel. No lo cubre el paso 3, que es sólo el del invitado. El SSH nuestro no
   es problema: la flota se direcciona por IPv6.
6. Migración en caliente al otro AX162 de prueba, cruzando región y base, y
   verificar que la VM conserva Internet por las dos familias.

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

**0.C — DNS: TTL y `minimum` del SOA a 60 s (§4.7).** Va pronto porque es lo
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

**0.F — El `<IDENT>` /64: pedir uno adicional en Robot, asignado a b0.**
Elegido el 2026-08-13. Requisito: global unicast, nuestro, no anunciado. No
tenemos espacio PI, así que la vía es **un /64 adicional de Hetzner**. Razones
de la elección, por orden:

1. Es global y sin ambigüedad **nuestro**, así que ninguna VM se queda sin poder
   alcanzar un destino legítimo por colisión de prefijo.
2. Enrutado a b0, un paquete que se escape por un fallo de traducción tiene el
   retorno muriendo en máquina nuestra, donde lo podemos **contar y tirar**:
   fallo cerrado con detector de fugas gratis.
3. No depende de ningún nodo ni de los bloques failover, así que ni una baja de
   hierro ni una conmutación de VIP lo tocan.

❌ **Descartado reusar el /64 de un AX162 vacío**: funcionaría hoy y sería una
bomba de relojería el día que ese nodo entre en servicio o se dé de baja.
🔲 Confirmar que Hetzner lo entrega **enrutado**, no atado a MAC.

**0.H — Criterio de aborto (fijado por el operador, 2026-08-13).**

> Si en cualquier momento una de las bases deja de responder a conexiones
> entrantes de RDP: **mover el failover a la otra base, parar, y
> revertir/evaluar.**

Para que sea operativo hace falta detectarlo **en segundos**, y ninguna de las
sondas actuales sirve: conncheck es **horario**, y el failover watchdog mira
liveness de base (ICMP + TCP 22/443 desde el árbitro), no RDP. Así que durante
cada ventana de trabajo hay que levantar un **sondeo continuo de RDP contra una
VM conocida de cada base**, y dejarlo corriendo mientras dure la sesión.

**0.G — Túnel base↔nodo en el nodo de pruebas (§4.5), con MSS clamping.** Va
después de 0.A y 0.B. ⚠️ El nodo usa `iptables` (`install.sh:625`) y la base
carga `/etc/nftables.conf` con `flush ruleset` — cualquier `systemctl reload
nftables` se lleva por delante lo que no esté en fichero, así que la tabla
nueva va en su propio include desde el principio, no como `nft add table` a
mano. Verificar antes de que exista ninguna VM: PMTUD en los dos sentidos,
ICMPv6 incrustado (§9.4), y que `pve-firewall` no pisa la tabla nueva.

### Fase 0bis — dos cambios INDEPENDIENTES que aprovechan el viaje

Ninguno de los dos necesita nada de este proyecto, pero los dos tocan las
mismas piezas, así que se aplican en la misma ventana.

**1. Arreglar el `cluster.fw` desfasado (riesgo #17). Son TRES pasos, no uno:**

- Corregir la plantilla inline de `first_boot.sh:617` — cubre sólo
  **instalaciones nuevas**, porque `first_boot.sh` únicamente corre al instalar.
- Corregir la **copia canónica del Storage Box** (`/home/firewall/cluster.fw`),
  que es la que se sirve a los 235 nodos existentes.
- Empujarla con `sync-base-nat.py sync nodes sync-firewall` y **verificar**.

**2. Cerrar el DNAT de la IP PRINCIPAL al público.**

Hoy `table ip nat prerouting` acepta el rango `10000-39999` en **las dos**
direcciones — la failover y la principal:

```nft
ip daddr 37.27.135.250 tcp dport 10000-39999 dnat to 10.0.0.3   # ← principal
ip daddr 77.42.49.79   tcp dport 10000-39999 dnat to 10.0.0.3   # ← failover
```

**Ningún cliente usa la principal.** Verificado el 2026-08-13 resolviendo los
nombres que genera `get_connection_url` / `_rdp_host`: `sqx-hel`, `trading-hel`,
`sqx` y `trading` → `77.42.49.79`; `sqx-fsn` y `trading-fsn` → `94.130.3.118`.
Todos VIPs. Así que la IP de gestión de cada base —la que sirve SSH, nginx y las
sondas del árbitro— está publicando el rango de RDP a todo Internet sin que
nadie legítimo entre por ahí.

⚠️ **NO se puede quitar: hay que restringirla.** `neuravps-conncheck.py:53-55`
sondea las ~1.850 VMs **a través de la IP principal de la base peer**, y es
deliberado — las dos bases tienen la VIP enlazada, así que una conexión a la VIP
se entregaría **localmente** y probaría la caja equivocada. Quitar el DNAT de la
principal deja a conncheck reportando la flota entera como inalcanzable.

**Arreglo: añadir `ip saddr <principal de la peer>` a esas dos líneas.** La IP
de gestión deja de ser superficie pública de RDP y conncheck sigue igual. El
patrón ya existe: `bf_allow` de `ip rdpguard` ya lleva las dos principales como
infraestructura de confianza.

⚠️ **Es NO aditivo** — toca una cadena existente. `nft -c -f` antes, una base
cada vez empezando por b0, y con el canario de RDP encendido.
🔲 Antes de aplicarlo: verificar la **IP de origen real** de las sondas (podrían
salir por otra dirección de la que asumimos) y desde dónde se ejecuta
`scripts/check_rdp_smb_connectivity.sh`, que lleva `37.27.135.250` cableado.

**Corrección de premisa**: esto NO libera puertos de salida. El DNAT de entrada
y el SNAT de salida son tuplas de conntrack distintas, y netfilter no reserva
puertos por tener un DNAT apuntando a ellos. El techo real sigue siendo el de
§3.2, por destino, y no cambia ni un puerto. **El motivo es superficie, no
capacidad.**

### Fase 1 — un nodo, una VM de prueba, una base
- Nodo de Falkenstein (**0000228**) + b0. VM de prueba **nuestra**, no de cliente.
- Aplicar: identidad en el invitado, par de reglas nft en el nodo, ruta `/128`
  en las **dos** bases, entrada NAT46 estática.
- **Validar** (§8) y **medir** antes/después.

### Fase 2 — un nodo de la otra región
- Nodo de Helsinki (**0000238**) + b1, misma VM de prueba migrada allí.
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

### Fase 5 (v1.1) — add-on de IPv4 dedicada

Una vez la salida es SNAT en la base, dar IP propia a un cliente son **dos
reglas**, no una arquitectura:

```nft
ip daddr <ded_v4>          dnat to <ipv4_privada_vm>   # sólo si abre la entrada
ip saddr <ipv4_privada_vm> snat to <ded_v4>            # salida, siempre
```

delante de la regla general. Se aprovisiona y se revoca al instante.

**Un bloque por base, SIN failover** (decisión 08-13, mucho más barato). Cada
cliente del add-on recibe **dos** IPv4 dedicadas, una por base, exclusivas
suyas, y se le enseñan las dos — mismo modelo que sus dos IPv6.

⚠️ **Esto CORRIGE el aviso de la versión anterior**, que decía que la IPv4
dedicada tenía que ser failover para que una conmutación no dejara al cliente
con la IP colgada. Con el modelo nuevo **ya no aplica**: no hay ninguna IP
colgada porque nunca se prometió que una dirección concreta sobreviviera — se
prometieron las dos. Cae la base, el nodo se va a la otra, y el cliente sale por
su segunda dirección, que ya conocía.

**La mitad IPv6 del add-on ya está entregada desde v0**: cada VM tiene sus dos
IPv6 exclusivas por construcción. El add-on sólo añade el par de IPv4.

🔲 Confirmar con Hetzner: (1) que el bloque sea **enrutado a la máquina**, no
direcciones atadas a MAC; (2) que un bloque adicional queda **atado a ese
servidor** — si algún día se cambia el hierro de una base, el bloque no viaja
solo. Con dos bases es asumible, pero tiene que estar en el runbook de
sustitución de base antes de necesitarlo.

**Beneficio colateral**: con IP dedicada el radio de impacto de un aviso de
abuso o una lista negra es **ese cliente y nadie más**. El add-on mejora el
riesgo #7 para quien lo compra, y saca de la IP compartida justo a los clientes
que más tráfico raro pueden generar.

#### Fase 5b — el interruptor de "abrir la IP entera"

Con IP dedicada, "abrir todos los puertos" significa algo por primera vez
(sobre una VIP compartida por 1.300 clientes no se puede). Firewall **cerrado
por defecto**; si se abre, las dos IPv4 aceptan entrada reenviada a la VM. El
flag gobierna **sólo la entrada** — la salida por su IP dedicada es
independiente, igual que `ipv6Enabled` hoy sólo gobierna la entrada desde
Internet (§4.3bis). Eso importa: el valor comercial (que la prop firm vea su
IP) llega con el firewall cerrado.

⚠️ **Va DESPUÉS, y no como valor por defecto.** Abrir una IPv4 no es como abrir
una IPv6: el espacio IPv6 es demasiado grande para barrerlo, pero una IPv4
pública se escanea entera y continuamente, y una caja Windows con todo abierto
aparece en los barridos en minutos. Y hay un detalle concreto: las dos capas de
`rdpguard` están montadas sobre los rangos del DNAT (`tcp dport 10000-39999` y
`20000-39999`), así que una IP dedicada reenviando todo llevaría el **3389
directo, fuera de esos rangos, saltándose las dos capas**. El incidente del
2026-07-16 — la base tirando paquetes por conntrack lleno, presentado como un
"flap de red" en toda Helsinki — fue exactamente una inundación de fuerza bruta
contra RDP. **Hay que extender los guards a las IPs dedicadas antes de ofrecer
el interruptor.**

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
      mismas (las pone el instalador; verificar que **las dos** se aplican)
- [ ] Sin cliente DHCP activo en el adaptador, en ninguna de las dos familias
- [ ] MTU/PMTUD: descarga de un fichero grande por HTTPS sin cortes

En el nodo y la base:
- [ ] `nft list table ip6 nvxlat` con las reglas esperadas
- [ ] Ruta `/128` **y** `/32` presentes en **las dos** bases
- [ ] `conntrack -S` → `insert_failed` **no sube**
- [ ] ICMPv6: `ping6` y PMTUD funcionan en los dos sentidos
- [ ] El SNAT sale de la **IP principal** de la base, no de la failover (§4.4)
- [ ] Con la base peer, el mismo puerto sigue respondiendo (entrada por las dos)
- [ ] El nodo **no tiene** tabla `nat` ni conntrack IPv6 (§4.2 — es transporte)
- [ ] **Aislamiento**: desde la VM de prueba A, RDP contra la VM B **falla**
      (§4.3bis — la regla anti-tráfico-entre-túneles está puesta en la base)
- [ ] GRE pasa entre las dos máquinas de Hetzner (§4.5)
- [ ] Sesión RDP con transporte **UDP** activo a través del túnel (el MSS
      clamping no cubre UDP)
- [ ] `insert_failed` **no sube** durante todo el ejercicio

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
| 2 | ~~`dnat prefix to` sin verificar~~ → **CERRADO 08-13**: la traducción se hace en la base, no en el nodo. Ya no se usa (§4.2). | ✅ cerrado |
| 3 | **MTU/MSS clamping** en el túnel base↔nodo. Sin él aparecen los "carga a medias" imposibles de diagnosticar. | 🔲 día 0 |
| 4 | **ICMPv6**: NPTv6 debe reescribir también las direcciones incrustadas en los errores ICMPv6, o revienta el PMTUD. `snat prefix` cubre la cabecera externa; el payload incrustado hay que comprobarlo. | 🔲 verificar |
| 5 | **`github.com` no tiene IPv6 real** (devuelve v4 mapeada) y los instaladores de invitado tiran de ahí. **Resuelto por diseño**: el túnel lleva las dos familias (§4.5), así que invitado y host del nodo salen por IPv4 sin necesitar IPv4 propia. | ✅ resuelto 08-13, 🔲 probar antes de quitar la primera IPv4 |
| 6 | ~~Confirmar con Hetzner que se puede renunciar a la IPv4 primaria~~ → **CERRADO 08-13 por el operador: SÍ se puede.** €1,70/mes cada una, y el /64 de IPv6 se queda igual. Los €399,50/mes de §10 están confirmados. | ✅ cerrado |
| 7 | **Radio de impacto**: con IP única, una lista negra o un aviso de abuso afecta a toda la región (hoy contenido a ~31 VMs por IP de nodo). Ya pasó con la máquina `devel` en julio. No lo arregla ningún parámetro del kernel — sólo repartir en más IPs. | ⚠️ asumido conscientemente en fase 0 |
| 8 | Medir el churn de Dukascopy **en hora punta** (la medición actual es del momento más muerto de la semana). | 🔲 |
| 9 | ~~`tcp_loose` y descarte de `INVALID` en el nodo~~ → **CERRADO 08-13**: la traducción vive en la base y una migración intra-región no cambia de base, así que el conntrack nunca se mueve (§4.2). | ✅ cerrado |
| 10 | ~~Reconciliar la regla de SNAT tras una conmutación~~ → **CERRADO 08-13**: la salida va por la IP principal, que no se mueve. No hay nada que reconciliar (§4.4). | ✅ cerrado |
| 16 | **REGRESIÓN DE AISLAMIENTO**: con el tráfico VM↔VM pasando por la base y llegando con la dirección de la base como origen, `+dc/base` concede RDP → un cliente podría abrir RDP contra la VM de otro. Hoy no pasa porque el tráfico va directo y `hosts-ipv6` sólo concede SMB. | 🔲 **REGLA EN LA BASE, OBLIGATORIA EN v0** (§4.3bis) |
| 17 | **`cluster.fw` desfasado en la flota**: la plantilla inline de `first_boot.sh:617` lista dos prefijos `2a01:4f9` (Helsinki) y **ninguno `2a01:4f8`** (Falkenstein). La copia canónica del Storage Box la pisa (`first_boot.sh:723`), pero la línea 740 avisa de que si esa descarga falla el nodo se queda con la no canónica — y ahí el RDP entrando por b0 estaría **bloqueado** para todas sus VMs. Independiente de este proyecto. | 🔲 barrido de solo lectura: `grep '^2a01:4f8' /etc/pve/firewall/cluster.fw` |
| 18 | **Los guards de `rdpguard` se saltan con IP dedicada**: están montados sobre los rangos del DNAT (10000-39999 / 20000-39999), así que una IPv4 dedicada con reenvío completo llevaría el 3389 directo y sin límite. El incidente del 2026-07-16 fue exactamente una inundación contra RDP. | 🔲 extender antes de ofrecer el interruptor (fase 5b) |
| 19 | **MSS clamping no cubre UDP**, y RDP tiene transporte UDP. | 🔲 prueba explícita en fase 1 |
| 20 | **GRE filtrado por Hetzner** (poco probable, pero algunos proveedores filtran lo que no es TCP/UDP). Plan B: `ip6tnl mode any`; plan C: WireGuard sobre UDP. | 🔲 prueba del día 0 |
| 11 | **Windows ante el renumerado IPv4** (NAK-y-rebind de dnsmasq) y `proxy_arp` sobre el bridge de PVE. | 🔲 validar en la VM de pruebas antes de tocar flota |
| 12 | **Que la identidad sobreviva a `reset_vm`.** El cliente puede reinstalar desde el panel y eso rehace el invitado entero. Sin DHCP, **las dos familias** dependen de que el instalador las vuelva a poner — y de que se verifique que lo hizo, no de que devuelva exit 0 (vm 1023, 08-02). | 🔲 |
| 14 | **Sin DHCP se pierde el autocurado**: un cliente que haga `netsh int ip reset`, reinstale el adaptador o restaure una copia se queda sin red y ya no vuelve solo. Lo sustituye el auto-fix de conncheck, que **hay que extender a IPv4** (hoy sólo mira IPv6). | 🔲 antes de la fase 3 |
| 15 | **Provisión sensible al orden**: el instalador debe configurar la red antes de nada que necesite red. Hoy el DHCP hacía ese orden irrelevante. | 🔲 |
| 13 | ~~El kernel de PVE no admite Jool~~ → **deja de ser un riesgo**: el diseño ya no necesita Jool en el nodo (§4.4bis). El spike se mantiene como información, no como bloqueo. | ✅ cerrado 08-13 |

---

## 10. Economía (referencia, precios del operador)

- IPv4 de nodo: **€1,70/mes** × 235 nodos = **€399,50/mes** liberables si toda
  la salida pasa por la base. ✅ **Confirmado por el operador el 2026-08-13**:
  Hetzner permite renunciar a la IPv4 de un dedicado ya contratado, y el /64 de
  IPv6 se conserva. El riesgo #6 queda cerrado y esta cifra es real.
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

## 11bis. Receta de IPs estáticas — VALIDADA EN PRODUCCIÓN 2026-08-13

Probada de punta a punta en las VMs de prueba **1096** (`0000228`, FSN) y
**1097** (`0000238`, HEL). Todo lo de aquí está **medido, no supuesto**.

### El esquema de direccionamiento IPv4

```
  IPv4 de la VM = 10.64.<vmid_hi>.<vmid_lo>/16      (vmid = hi*256 + lo)
  Puerta enlace = 10.64.255.1                        (igual en TODOS los nodos)
  DNS           = 185.12.64.1, 185.12.64.2           (los de dhcp-option=6)
```

| vmid | dirección |
|---|---|
| 100 (el más bajo en uso) | `10.64.0.100` |
| 1096 / 1097 (las de prueba) | `10.64.4.72` / `10.64.4.73` |
| 2072 (el más alto en uso) | `10.64.8.24` |
| 9999 (`VMID_MAX`) | `10.64.39.15` |

**Por qué así:**

- **Los dos últimos octetos SON el vmid** en big-endian, así que de un paquete
  se lee de quién es sin consultar nada.
- **Vive fuera de `10.0.0.0/16`**, donde está hoy toda la flota → durante un
  despliegue **secuencial nodo a nodo no puede haber colisión** con los que aún
  usen el esquema viejo. Los dos esquemas conviven sin tocarse.
- También fuera de `10.0.0.0/24`, que es el `veth-host` del netns de Jool en las
  bases (§4.4).
- Como `VMID_MAX = 9999`, el tercer octeto **nunca pasa de 39**, así que
  `10.64.40.0`–`10.64.255.255` queda libre para siempre y la puerta de enlace en
  `.255.1` no puede colisionar jamás con una VM.
- Es **determinista desde el vmid**: no hace falta asignador ni registro.
  Aun así se **guarda** en `servers.ipv4` (§6) — la fuente de verdad es el campo,
  no el cálculo.

### Prerrequisito por nodo (ADITIVO — no toca nada existente)

```bash
ip addr add 10.64.255.1/16 dev vmbr0
iptables -t nat -A POSTROUTING -s 10.64.0.0/16 -o <uplink> -j MASQUERADE
sysctl -w net.ipv4.conf.vmbr0.proxy_arp=1
```

Se **añade** una dirección al bridge y una regla de MASQUERADE; el `10.0.0.1/16`
y su regla se quedan intactos sirviendo a los invitados que aún estén en el
esquema viejo. Por eso un nodo se puede convertir sin tocar a sus VMs.

🔲 **Pendiente**: llevar esto a `install.sh` para que sea persistente. En los
nodos de prueba está aplicado **en vivo**, así que un reinicio del NODO lo
pierde (el reinicio de la VM no, ver abajo).

### Receta en el invitado

```powershell
netsh interface ipv4 set address   name="Ethernet" static 10.64.4.72 255.255.0.0 10.64.255.1
netsh interface ipv4 set dnsserver name="Ethernet" static 185.12.64.1 primary validate=no
netsh interface ipv4 add dnsserver name="Ethernet" 185.12.64.2 index=2 validate=no
```

Vía `pvesh create /nodes/<nodo>/qemu/<vmid>/agent/exec` con `powershell.exe
-EncodedCommand` (UTF-16LE + base64), el mismo camino que
`migrate_to_deterministic_ipv6.sh`. ⚠️ `qm agent exec` directo da `400 too many
arguments`; hay que usar `pvesh` con `--command` repetido.

**`set address … static` desactiva el cliente DHCP del adaptador por sí solo** —
no hace falta un paso aparte. Verificado: `dhcp=Disabled`.

**El éxito se MIDE**: la PowerShell termina devolviendo una línea

```
BOUND:10.64.4.72/16 origin=Manual gw=10.64.255.1 dns=185.12.64.1,185.12.64.2 dhcp=Disabled
```

y no se da por buena hasta comprobar además salida real (`api.ipify.org`),
resolución DNS y que la IPv6 sigue igual. Regla de oro de la vm 1023 (08-02):
un `exitcode: 0` no prueba que se haya aplicado nada.

### Qué quedó demostrado

| prueba | resultado |
|---|---|
| IPv4 estática aplicada | `10.64.4.72/16`, `origin=Manual`, `dhcp=Disabled` |
| Salida IPv4 real | `188.40.145.216` (la del nodo, vía el MASQUERADE nuevo) |
| Salida IPv6 | `2a01:4f8:2240:201f::448` — **intacta**, sin tocarla |
| DNS | resuelve con los dos servidores estáticos |
| **Persistencia tras reiniciar la VM** | **idéntico** — sobrevive |
| Una sola dirección v4 | sí, sin restos de la concesión DHCP |
| **Con `dnsmasq` PARADO en el nodo** | **todo sigue funcionando** |

Ese último punto es el que valida la decisión de §4.4: **el nodo puede
prescindir de dnsmasq**, que era su única función.

⚠️ **`ping` a la puerta de enlace falla, y es NORMAL**: el firewall por-VM tiene
`policy_in: DROP` y no acepta ICMP, así que se descarta la respuesta del eco. El
enrutado funciona — no perseguir este falso síntoma.

### Experimento: quitar la IPv6 del invitado (2026-08-13, vm 1096)

Hecho a propósito sobre la VM de prueba, y restaurado después. Dos resultados.

**1 — El camino base→VM es IPv6 PURO.** Con la IPv6 fuera:

| | resultado |
|---|---|
| Internet IPv4 desde dentro | ✅ `188.40.145.216` |
| DNS | ✅ resuelve |
| `api64.ipify.org` | devuelve la **IPv4** — Windows cae a v4 solo |
| **RDP `21096` desde fuera** | ❌ **CERRADO** |
| **SSH `31096` desde fuera** | ❌ **CERRADO** |

Es decir: **la VM sigue viva y con Internet, pero nadie puede entrar.** Es el
`MIGRATION_DEGRADED` reproducido deliberadamente, y explica por qué el síntoma
que ve conncheck es "mapas NAT OK + invitado cerrado" (§4.3 de
`neuravps-conncheck`): la VM no está caída, está sin dirección.

Corolario para el diseño: mientras el `<IDENT>` no esté puesto y enrutado, **no
se puede quitar la IPv6 vieja** — el corte es total e inmediato. La sustitución
tiene que ser add-then-delete, nunca delete-then-add.

**2 — ⚠️ TRAMPA: `netsh … delete address … store=persistent` NO borra la
dirección viva.** Sólo quita la entrada del almacén persistente; la dirección
**sigue enlazada y funcionando** hasta el siguiente reinicio. Medido:

```
persistente: (vacío)
activo     : 2a01:4f8:2240:201f::448      ← sigue ahí, y RDP sigue abierto
```

Eso deja la VM en un **estado partido**: funciona hoy, y se queda a oscuras al
reiniciar — días o semanas después, sin ninguna relación aparente con el cambio
que lo causó. Es la clase de bomba de relojería que luego cuesta una tarde de
diagnóstico.

- Para borrar de verdad hacen falta **los dos almacenes** (`store=active` y
  `store=persistent`).
- `add address … store=persistent` sí aplica a los dos a la vez (el activo lo
  recogió al instante). **La asimetría está en el borrado, no en el alta.**
- Cualquier script que sustituya la IPv6 del invitado —el instalador, el
  auto-fix de conncheck, la futura migración a `<IDENT>`— tiene que contemplarlo
  y **verificar el resultado en el almacén activo**, no fiarse del `exitcode`.

### ⚠️ La puerta de enlace del invitado TAMBIÉN depende del nodo

Descubierto el 2026-08-13 al preguntar el operador si bastaba con actualizar
Firestore. **No basta, y no sólo por el transporte.**

```
migrate_vm.sh:1240:  DST_GATEWAY="${EXPECTED_VM_IPV6%::*}::1"
```

La ruta por defecto del invitado apunta hoy a `<prefijo_del_nodo>::1`
(verificado en la vm 1096: `::/0 via 2a01:4f8:2240:201f::1`, y esa dirección
está puesta en el bridge del nodo). Así que **darle a la VM una IPv6 estable no
basta**: al migrar sobreviviría la dirección pero **la puerta de enlace dejaría
de existir**, y habría que volver a entrar en Windows. Se perdería justo lo que
el proyecto persigue, con un síntoma especialmente traicionero — *"la dirección
es correcta pero no hay salida"*.

**Para que el invitado sea de verdad independiente del nodo hay que uniformar
TRES cosas, no una:**

| | hoy | debe ser |
|---|---|---|
| dirección v6 | `<prefijo_nodo>::<vmid>` | `<IDENT>::<vmid>` **como /128** |
| **puerta de enlace v6** | `<prefijo_nodo>::1` | **`fe80::1`, igual en todos los nodos** |
| DNS v6 | `2a01:4ff:ff00::add:1/2` | igual (ya es uniforme) |

⚠️ La dirección va como **`/128`**, no `/64`: con `/64` el nodo trataría todo el
`<IDENT>` como on-link y haría NDP para VMs remotas que nunca contestarán.

### `fe80::1` como puerta de enlace — PROBADO 2026-08-13 (vm 1096)

**Duda del operador**: *"¿no dijiste que Windows no prioriza IPv6 si usamos
fe80?"*. No — aquello era sobre **ULA (`fc00::/7`) como DIRECCIÓN del invitado**,
que la RFC 6724 pone por debajo de IPv4 (§4.1). El `fe80::1` es el **siguiente
salto de la ruta**, y la selección de origen no mira el next-hop. De hecho en
cualquier red IPv6 normal el next-hop por defecto de Windows **es** una
link-local, porque es lo que anuncian los RA; el caso raro es el nuestro, con un
next-hop global, forzado por tener los RA desactivados.

Probado en vez de asumido. Con `fe80::1` como puerta de enlace:

| | resultado |
|---|---|
| `::/0` next-hop | `fe80::1` |
| `api64.ipify.org` | `2a01:4f8:2240:201f::448` → **sigue prefiriendo IPv6** sobre IPv4 |
| origen elegido hacia un destino v6 | la global, no la link-local |
| RDP `21096` / SSH `31096` | abiertos |
| **tras reiniciar la VM** | **todo idéntico** — gw, v6, v4 y salida |

### Persistencia en el NODO — hecha y validada con reinicio real

`install.sh` genera ahora la configuración nueva para **nodos nuevos**, y los dos
de prueba se han actualizado a mano (sólo afecta a la sección de `vmbr0`, nunca
a la del enlace físico, así que el nodo vuelve accesible pase lo que pase):

```
# en la stanza inet de vmbr0
post-up   ip addr add 10.64.255.1/16 dev vmbr0 || true
post-up   iptables -t nat -A POSTROUTING -s '10.64.0.0/16' -o <uplink> -j MASQUERADE
post-down iptables -t nat -D POSTROUTING -s '10.64.0.0/16' -o <uplink> -j MASQUERADE
# en la stanza inet6 de vmbr0
post-up   ip -6 addr add fe80::1/64 dev vmbr0 || true
# y en sysctl.d
net.ipv4.conf.vmbr0.proxy_arp = 1
```

**Reinicio real del nodo 0000228 (2026-08-13)** — todo volvió solo:

| | tras el reinicio |
|---|---|
| `10.0.0.1/16` + `10.64.255.1/16` | ✅ las dos |
| `<nodo>::1/64` + `fe80::1/64` | ✅ las dos |
| MASQUERADE `10.0.0.0/16` + `10.64.0.0/16` | ✅ las dos |
| `proxy_arp` | ✅ `1` |
| Invitado: gw, v6, v4, salida por ambas | ✅ idéntico |
| RDP `21096` / SSH `31096` | ✅ abiertos |

Repetido en **0000238 (Helsinki)** con idéntico resultado: las dos direcciones
v4, `fe80::1`, la regla de MASQUERADE, `proxy_arp`, y la vm 1097 con
`gw=fe80::1`, `10.64.4.73`, salida por ambas familias y RDP/SSH abiertos.

ℹ️ **La VM no arranca sola tras un reinicio PLANIFICADO, y es correcto**: el
apagado ordenado para las VMs antes de reiniciar, así que quedan paradas a
propósito y `node_boot_reconcile` no debe levantarlas. Tras un reinicio **no
planificado** sí las levanta solo (`node_liveness.py:845`). No es un fallo.

### ⚠️ ¿Pierde conexión alguien del modelo antiguo? — análisis por cambio

La pregunta que hay que responder antes de tocar el primer nodo CON clientes.
Los cuatro cambios del nodo son **aditivos**, pero no todos con el mismo grado
de certeza, y **uno de los pasos de la receta NO es seguro**:

| cambio | ¿afecta a un invitado del modelo viejo? | evidencia |
|---|---|---|
| `ip addr add 10.64.255.1/16` | **No.** Segunda dirección; el `10.0.0.1/16` sigue ahí | medido |
| MASQUERADE `-s 10.64.0.0/16` | **No.** Regla añadida al final; la de `10.0.0.0/16` sigue primera y sigue casando | medido |
| `ip -6 addr add fe80::1/64` | **No.** Los viejos usan `<nodo>::1`, que no se toca | medido |
| `proxy_arp = 1` | **No** — Linux no responde por proxy cuando la ruta al destino sale por la MISMA interfaz por la que llegó la petición, así que el ARP invitado↔invitado del bridge no cambia | ✅ **MEDIDO** (ver abajo) |
| **Parar `dnasmasq`** | **SÍ ROMPE.** Los invitados viejos obtienen su IPv4 por DHCP: al no renovar, acaban sin dirección | medido (en 0000228 no había ninguno, por eso fue inocuo) |

**Restricción de orden para el despliegue, que se deriva de la última fila:**

> En un nodo con clientes, **`dnsmasq` se queda hasta que TODOS sus invitados
> estén convertidos a IP estática**. Sólo entonces se retira. Convertir el nodo
> y convertir sus VMs son dos pasos distintos y en ese orden.

#### ✅ Medido: invitado del modelo VIEJO sobre nodo YA CONVERTIDO

Es la situación exacta en la que estará **cada nodo durante el despliegue** —
convertido, pero con invitados que todavía no lo están — así que valida bastante
más que `proxy_arp`. Hecho el 2026-08-13 devolviendo la vm 1097 al modelo viejo
(IPv4 por DHCP + puerta de enlace v6 derivada del nodo) sobre `0000238`, que ya
tenía las dos direcciones nuevas, la regla de MASQUERADE y `proxy_arp=1`:

```
v4=[10.0.115.210] dhcp=Enabled gw4=10.0.0.1 gw6=[2a01:4f9:3100:4b08::1]
salida4=65.109.148.185  salida6=2a01:4f9:3100:4b08::449  dns=OK
RDP 21097 / SSH 31097: ABIERTOS
```

Cogió su concesión del rango antiguo, usó la puerta de enlace vieja en las dos
familias, salió a Internet por ambas y siguió alcanzable. **Un nodo convertido
sirve a sus invitados del modelo viejo exactamente igual que antes.** Después se
devolvió al modelo nuevo y volvió a `10.64.4.73` / `fe80::1` sin incidencias.

Queda así demostrado el punto que sostiene todo el despliegue secuencial: la
conversión del nodo es **aditiva de verdad**, y convertir el nodo y convertir sus
VMs son dos pasos independientes que pueden separarse en el tiempo.

## 12. Siguiente sesión (actualizado 2026-08-13)

Por orden. Lo de arriba no depende de decisiones pendientes; lo de abajo sí.

1. ✅ **Congelar los dos nodos** (§7, 0.A) — HECHO 2026-08-13: `0000228` (FSN) y
   `0000238` (HEL), los dos a 0 VMs y `frozen: true`.
2. **Levantar el canario de RDP** antes de tocar ninguna base: netcat continuo
   contra el puerto RDP de **2-3 clientes reales** de la base que se toca. Si
   dejan de responder → **avisar de inmediato y mover el failover a la otra
   base** (§7, 0.H). Esto va ANTES que cualquier cambio, no en paralelo.
3. **Bajar SOA `minimum` y TTL a 60 s** (§4.7). Independiente, riesgo cero, y
   es lo que más tarda en surtir efecto — cuanto antes, mejor.
4. **Línea base de `insert_failed`** en las dos bases (§7, 0.D).
5. **Pedir el `/64` adicional para `<IDENT>`** en Robot, asignado a b0 (0.F).
6. Crear la VM de prueba en `0000228` desde `/admin/servers` — el formulario
   pide `nodeId` explícito, y `frozen` sólo gatea la colocación **automática**,
   así que se puede forzar un nodo congelado. Luego ponerle `maintenance: true`
   en su doc de `servers` (§6).
7. Montar el túnel `ip6gre` en `0000228` y **verificar que Hetzner pasa GRE**.
8. **Fase 0bis**: `cluster.fw` (3 pasos) y cerrar el DNAT de la IP principal.

**Lo que ya NO gatea la fase 0**: cronometrar la conmutación de failover. Eso
gatea la **fase 4**, no el arranque — durante las fases 0 a 2 los únicos que
dependen de la base para salir son los dos AX162 vacíos. Y cuando toque, sale
gratis instrumentando el próximo drenaje de mantenimiento, que ya se hace de
forma rutinaria y cuyo impacto ya se está asumiendo; con las VMs de prueba en el
camino nuevo, ese drenaje mide además el hueco de **salida**, que es el número
que de verdad falta. Comprar una failover dedicada sólo si al llegar a la fase 3
se quiere ensayar la conmutación repetidas veces (€1,75/mes, decisión de
entonces).

Pendiente de escribir antes de la fase 3, y **antes de conocer la cifra**: el
**criterio de aborto** — cuántos segundos de hueco convierten esto en un "no
seguimos".

---

## 15. Persistencia en las BASES — HECHO 2026-08-14

Todo lo que se construyó a mano en las bases durante los días 13 y 14 vivía
**sólo en memoria**. Un reinicio de una base lo habría borrado entero, dejando
a los dos nodos convertidos —que ya no tienen IPv4 pública— **sin salida v4
ninguna** y sin camino de vuelta para sus invitados.

Se descubrió al preparar el ciclo de reinicio de las bases, antes de tocarlas.
Es el mismo error que el 🔲 pendiente del §11bis para los nodos, repetido en el
otro extremo del túnel.

### Lo que NO sobrevivía a un reinicio

| pieza | dónde vivía |
|---|---|
| los 2 `ip6gre` + sus `/127` + MTU 1456 | memoria |
| rutas `10.65.<hi>.<lo>` al host de cada nodo | memoria |
| `ip nat`: SNAT de `10.64/16` y `10.65/16` | memoria |
| `ip6 nat`: SNAT canónica + `snat prefix to` | memoria |
| `inet filter input`: aceptar GRE | memoria |
| `inet filter forward`: `veth-host↔tun-*`, `tun-*→uplink` | memoria |
| rutas `/128` y `/32` **por VM** | ✅ ya las reconciliaba `base-nat-boot` |

### Lo que se construyó

`base/snippets/neuravps-base-tunnels.{sh,service}` + `tunnel-nodes.conf`:
una unidad `oneshot` que levanta el lado BASE de los túneles desde una tabla de
nodos. Tres decisiones que importan:

- **`Before=base-nat-boot.service`** — las rutas por VM apuntan a estas
  interfaces. Si no existen todavía, `ip route add ... dev tun-pXXX` falla y la
  base arranca sin camino a los invitados del modelo nuevo.
- **`PartOf=nftables.service`** — el set `gre_peers` se vacía con cada recarga
  del firewall; así se vuelve a poblar solo.
- **Sólo recrea un túnel si sus parámetros han cambiado.** Un `ip link del`
  gratuito tira el tráfico de ese nodo ~1 s en cada arranque del servicio.

El firewall pasa a `/etc/nftables.conf` con `base/snippets/persist-egress-nft.py`,
que inserta seis bloques con anclas exactas, **aborta si un ancla no aparece
exactamente una vez**, valida con `nft -c -f` y deja copia en
`/etc/nftables.conf.pre-egress`.

Las direcciones GRE autorizadas dejan de ser una lista literal y pasan a un set
`gre_peers` que puebla la misma unidad desde el mismo fichero de nodos: **set y
túneles no pueden divergir**, y la regla es una sola línea para toda la flota.

### Validación

El script se ejecutó primero **en caliente** contra el estado hecho a mano:
`diff` vacío en b0 y, en b1, exactamente la única ruta que faltaba
(`10.65.0.228 dev tun-p228` — asimetría que llevaba ahí desde el día 13). Que el
estado calculado coincida byte a byte con el estado a mano es la prueba de que
la tabla de nodos describe bien la realidad.

Después, reinicio real de las dos bases (§16): **10/10 piezas reaplicadas solas**.

### 🔲 Pendientes que esto deja abiertos

- **El slot 0 está sobrecargado.** La dirección canónica de SNAT de cada base
  (`…ffff::` y `…ffff::2`) es *a la vez* el extremo del túnel del nodo 228.
  Funciona por casualidad —la base acepta su propia dirección venga por donde
  venga— pero es una trampa. Al pasar a slots deterministas (`slot = id`, que es
  lo que hace el script por defecto), **reservar el slot 0 para las bases** y
  renumerar el 228.
- **Los túneles siguen anclados a las IPs PRINCIPALES de las bases, no a las
  VIPs**, en contra de lo que decide §4.5. Consecuencia medida en §16: reiniciar
  una base deja sin salida v4 a todos los nodos cuyo `LOCAL_TUN` apunta a ella.
  Con dos VMs de prueba es anecdótico; **a escala de flota es media flota sin
  salida durante cada mantenimiento de base**, cuando hoy un mantenimiento es
  invisible. Hay que cerrarlo **antes de la fase 3**.

---

## 16. Ciclo de reinicio de las dos BASES — 2026-08-14

Kernel **6.12.100 → 6.12.101**, una base cada vez, según
[[neuravps-base-maintenance-reboot-cycle]]. Objetivo doble: cargar el kernel y
**comprobar que el modelo nuevo se reaplica solo**.

| | b1 (HEL) | b0 (FSN) |
|---|---|---|
| vuelta (cambio de `boot_id`) | **70 s** | **71 s** |
| kernel | 6.12.101 ✅ | 6.12.101 ✅ |
| `systemd is-system-running` | `running`, 0 fallidas | `running`, 0 fallidas |
| piezas del modelo nuevo | **10/10** | **10/10** |
| mapas NAT | 1873×4 + 209 | 1873×4 + 209 |
| clientes reales por su IP principal, aislada | **12/12** | **12/12** |

**Canario: 140 rondas × 6 clientes reales, CERO fallos** — ni un cliente notó
nada, ni durante los cuatro traslados de VIP ni durante los dos reinicios.

### Método que hizo esto seguro

- **Recargar `nftables` ANTES de reiniciar, con la base ya drenada** y un hombre
  muerto (`systemd-run --on-active=90` que restaura la copia). Si el fichero
  persistido estuviera mal, se ve en 2 s y se arregla en 5, en vez de
  descubrirlo en un arranque a ciegas. Las dos bases cargaron a la primera.
- **Probar la base recién reiniciada AISLADA por su IP principal**, antes de
  devolverle VIPs: el DNAT v4 casa también la IP principal, así que se cazaría
  una base rota sin clientes encima.
- **Gate diferencial**: lo que falla por la base bajo prueba se reintenta por la
  otra. Responde en la otra = fallo real; muerto en las dos = VM apagada.

### Lo que el ciclo DEMOSTRÓ del modelo nuevo

1. **La persistencia funciona.** Las 10 piezas volvieron solas en las dos bases.
2. **Los invitados no se tocan.** Las dos VMs conservaron su IPv6 de identidad,
   su IPv4 privada y su puerta de enlace, y recuperaron salida v4 **y** v6 con
   la identidad correcta (`…::448` por b0, `…::449` por b1) sin entrar en ellas.
3. **La entrada cruzada aguanta el mantenimiento.** Con las 4 VIPs en una sola
   base, las dos VMs de prueba siguieron respondiendo RDP y SSH por las dos.
4. **`base-nat-boot` reconcilia las rutas por VM al arrancar**, y el orden
   `Before=` hace que encuentre los túneles ya en pie.

### Lo que el ciclo DESTAPÓ

⚠️ **Reiniciar una base deja sin salida IPv4 a los nodos cuyo `LOCAL_TUN` apunta
a ella.** Durante la ventana de b1, el nodo 238 y su VM se quedaron sin v4
(entrada por b0 intacta); durante la de b0, lo mismo con el 228. Es consecuencia
directa de que los túneles estén anclados a las **IPs principales** en vez de a
las **VIPs**, en contra de §4.5.

Hoy es anecdótico —dos VMs de prueba, sin clientes—. **A escala de flota deja a
media flota sin salida v4 en cada mantenimiento de base**, cuando hoy un
mantenimiento de base es completamente invisible para el cliente. Anclar a las
VIPs lo elimina: al moverse la VIP, el túnel del nodo la sigue y termina en la
superviviente sin tocar nada. **Bloqueante de la fase 3.**

### Hallazgos laterales

- **`certbot.service` falla en las dos bases** desde hace tiempo: intenta
  renovar `pve.neuravps.com`, un certificado **DNS-01 manual sin hooks**.
  Caduca el **2026-08-31**. En b0 es un residuo (nginx sólo sirve
  `neuravps-dual` y `file-bridge`), pero **b1 todavía lo referencia en nginx**.
  Es exactamente lo que anticipaba [[neuravps-dual-region-bases]]. Arreglo:
  pasar b1 a `neuravps-dual` (12 SANs, DNS-01 automático) y borrar los
  `renewal` huérfanos.
- **`guest-exec` del agente qemu deja de funcionar en los invitados**
  (`Failed to execute child process (Permission denied)`) mientras el resto del
  agente sigue perfecto (ping, `network-get-interfaces`, `get-osinfo`). Apareció
  tras varias llamadas seguidas. **Alternativa fiable: SSH al invitado por su
  IPv6 de identidad** con las credenciales de Firestore (`serverUser` /
  `serverPassword`) — no depende del agente y prueba de paso el camino de red.

---

## 17. Anclaje a las VIPs + sondeo — HECHO Y VALIDADO 2026-08-14

Cierra el bloqueante del §16. Los túneles dejan de apuntar a una **máquina** y
apuntan a un **servicio**: al moverse una VIP, el túnel del nodo la sigue y
termina en la superviviente sin tocar nada en ningún lado.

### ⚠️ §4.5 estaba incompleto: UN juego por base NO basta

El diseño decía «cada base lleva un único juego de túneles, sourced de la VIP
que posee». **Medido: no sobrevive a un traslado de VIP.** Cuando la VIP de
Helsinki se mueve a b0, los paquetes del nodo llegan a b0 dirigidos a esa VIP, y
b0 no tiene ningún túnel con ese par `(local, remoto)`: se caen.

**Cada base crea DOS túneles por nodo, uno por VIP, los dos siempre en pie.** En
cada momento sólo lleva tráfico el de la VIP que la base posee de verdad; el otro
está de reserva y se activa **solo** cuando la VIP se mueve. A escala de flota
son ~470 interfaces por base: de kernel, sin estado y sin coste apreciable.

### La dirección canónica de SNAT es de la VIP, no de la máquina

Antes había una por base, y el nodo enrutaba esa `/128` por un túnel fijo — así
que al moverse la VIP la base de detrás cambiaba y el retorno se perdía. Ahora
son dos, elegidas por el **túnel de salida**:

```
oifname "tun-fp*" ip6 daddr <IDENT> ct status dnat snat to 2a01:4f9:c01f:e:ffff::
oifname "tun-hp*" ip6 daddr <IDENT> ct status dnat snat to 2a01:4f9:c01f:e:ffff::2
```

Lo que sale por un túnel anclado a la VIP FSN lleva la canónica FSN, y el nodo la
devuelve por su `tun-fsn`, que termina en quien posea esa VIP. **Correcto por
construcción**, esté donde esté cada VIP. Los valores no cambian; cambia lo que
significan.

### 🔴 LA TRAMPA CARA: `rp_filter` — v6 pasa y v4 no

Con anclaje a VIP el camino es **asimétrico a propósito**: el tráfico entra por
el túnel de la VIP que la base posee y sale por el de casa. El filtro de ruta
inversa lo tira como `martian source`.

El síntoma engaña muchísimo: **IPv6 funciona perfectamente y IPv4 no**, porque
**Linux no tiene `rp_filter` para IPv6**. Se ve tráfico en `tcpdump` en los dos
extremos, contadores subiendo, y aun así cero entradas de `conntrack` y cero en
los contadores de `forward`: el paquete muere entre el dispositivo y netfilter.

**El modo laxo (`2`) NO basta — tiene que ser `0`**, y en `all` *y* en la
interfaz (el kernel usa el máximo de los dos). Se fija en los dos extremos, sólo
en las interfaces de túnel: sólo llevan GRE entre máquinas nuestras, ya filtrado
por `gre_peers` y `[IPSET base]`; la uplink conserva el suyo.

Para diagnosticarlo: `sysctl -w net.ipv4.conf.all.log_martians=1` y `dmesg`.

### Sondeo en el nodo — ortogonal al anclaje

GRE es **sin estado**: el túnel no se cae cuando la base muere, se queda UP para
siempre apuntando a una caja muerta. Anclaje y sondeo cubren casos distintos:

| | mantenimiento PLANIFICADO | muerte NO planificada |
|---|---|---|
| **anclaje a VIP** | **0 s** (movemos la VIP antes) | espera a que el watchdog mueva la VIP (~5-6 min) |
| **+ sondeo** | igual | **~30 s** (el nodo salta a la otra región) |

El sondeo abre un TCP a la canónica de su región (`:443` de nginx). **`ping` no
vale: PVE tira el eco ICMPv6 y da falso negativo** — el túnel bueno también da
100 % de pérdida al ping mientras mueve tráfico real. Histéresis asimétrica: 2
fallos para irse, 4 aciertos para volver.

### Validación — traslado real de VIP, 2026-08-14

Con las VIPs de Helsinki movidas a b0 y **sin tocar nada en el nodo 238**:

| | antes | con HEL en b0 | tras devolverla |
|---|---|---|---|
| nodo 238, salida v4 | `37.27.135.250` | **`188.40.153.120`** | `37.27.135.250` |
| vm 1097, salida v6 | `…3070:3984::449` | **`…2b03:18a9::449`** | `…3070:3984::449` |
| sondeo | `hel 0` | `hel 0` | `hel 0` |

El sufijo (`::449` = vmid) se conserva en las tres columnas. RDP y SSH de las dos
VMs respondieron por **las cuatro vías** durante todo el ejercicio, la flota se
mantuvo 12/12 por cada base y el canario acumuló **340 rondas × 6 clientes reales
sin un solo fallo**.

### Renumerado del tránsito

`slot = id` (determinista, sin registro). **El hueco 0 queda RESERVADO** para las
canónicas de las bases, lo que de paso resuelve la sobrecarga que señalaba el
§15: el nodo 228 pasa del slot 0 al 228 (`…ffff::e40`) y el 238 al 238
(`…ffff::ee0`). Dentro de cada hueco de 16: `+0/+1` par FSN, `+2/+3` par HEL.

### 🔲 Pendiente antes de la flota

- **Las VIPs en el `[IPSET base]` de los 234 nodos.** Hoy sólo en los dos de
  prueba (`nodo-ipset-base-vips.py`). Es aditivo y compatible con los dos
  modelos, pero sin él un nodo convertido tira el GRE de la VIP.
- **`install.sh`**: túneles, `rp_filter` de los túneles y el sondeo, para que un
  nodo nuevo nazca ya así.
