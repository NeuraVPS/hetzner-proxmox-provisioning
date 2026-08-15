# Propuesta: clamping de MSS en el egress IPv4 de los invitados

**Estado:** PROPUESTA — nada aplicado. Medido en producción 2026-08-15.
**Origen:** caso Diego Fonts (`cryptopunkofficial@gmail.com`, vm541, nodo 0000206-AX162-2-LTD): StrategyQuant no podía verificar la licencia — *"Error - Program cannot connect to internet"* — con el servidor perfectamente conectado.

## El defecto, en una línea

**El invitado anuncia MTU 1500 sobre un túnel que sólo transporta 1456**, y no hay clamping de MSS en ninguna parte. Los paquetes grandes se pierden en silencio.

## Cómo está montado el egress IPv4 hoy

El IPv4 del invitado NO sale por la tarjeta del nodo. Va por un túnel **ip6gre** hasta la base:

```
ip rule:      from 10.64.0.0/16 iif vmbr0  lookup 101
table 101:    default dev tun-fsn
tun-fsn:      ip6gre  local <nodo>::2  remote <base VIP>::2   mtu 1456
```

Cada nodo tiene los dos túneles, `tun-fsn` y `tun-hel`, **ambos a MTU 1456**. Verificado idéntico en 0000206-AX162-2-LTD (FSN) y 0000034-AX162-R (HEL) → **es uniforme en la flota, no una anomalía de un nodo**.

La aritmética: `1500 − 40 (cabecera IPv6) − 4 (GRE) = 1456`. El túnel está bien dimensionado; lo que falta es que el invitado se entere.

**El `MASQUERADE` del nodo no interviene**: las reglas son `-o enp193s0f0np0`, y el tráfico del invitado sale por `tun-fsn`, otra interfaz.

## Qué se midió

Dentro de vm541, barrido de PMTU con DF:

| payload | paquete | IPv4 |
|---|---|---|
| 1412 | 1440 | PASA |
| 1432 | 1460 | necesita fragmentar |

Y el efecto en un handshake TLS real contra `strategyquant.com`:

```
                 time_connect   time_appconnect
IPv4  #1           0.015 s          0.073 s
IPv4  #2           0.013 s          3.208 s      <-- retransmision TCP (1+2)
IPv4  #3-#5        0.050 s          6.57 s       <-- retransmision TCP (1+2+4)
IPv6  (control)    0.013 s          0.033 s
```

El TCP conecta rápido; **lo que se cuelga es el handshake TLS**, cuando el servidor manda la cadena de certificados en paquetes grandes. Los 3,2 y 6,57 s son exactamente el backoff de retransmisión de TCP.

**Comprobación del diagnóstico** (aplicada en vm541, `netsh ... mtu=1440 store=persistent`):

```
ANTES  (MTU 1500):  6,55 s · 3,22 s · 3,20 s
DESPUES (MTU 1440): 0,043 · 0,058 · 0,071 · 0,070 · 0,033 s   http=200
```

De 3-6 s a 40 ms. Diagnóstico confirmado por experimento, no por inferencia.

## Por qué NO vale con bajar el MTU de cada invitado

Es lo que se hizo en vm541 como parche inmediato, y funciona, pero:

1. **Hay que tocar cada VM**, una a una, y las nuevas nacen mal.
2. **Sólo arregla la dirección de salida.** El invitado deja de emitir paquetes grandes, pero el servidor remoto sigue mandándoselos según el MSS que anunció el invitado en el SYN. El clamping en el nodo reescribe el MSS **en los dos sentidos** (SYN y SYN-ACK pasan ambos por el nodo), que es lo que de verdad cierra el problema.
3. Un cliente puede reinstalar Windows desde el panel y perderlo.

## Arreglo propuesto

Clamping de MSS al MTU de la ruta, en la cadena forward del **nodo** (es la puerta de enlace del invitado y el extremo del túnel):

```
tcp flags syn / syn,rst  tcp option maxseg size set rt mtu
```

`rt mtu` se resuelve a **1456** para el tráfico que enruta por `tun-fsn`/`tun-hel` → MSS **1416**. Se autoajusta si algún día cambia el MTU del túnel, y no afecta al tráfico que no pasa por el túnel.

⚠️ **Ojo con la variante ingenua:** un clamp genérico *contra la ruta principal* daría 1500 → MSS 1460 y **sería un no-op**. Tiene que evaluarse sobre la ruta real del invitado (tabla 101), que es la que apunta al túnel.

**Por qué es de bajo riesgo:** sólo reescribe una opción TCP en el paquete SYN. No toca datos, no cambia enrutado, no añade estado. Es la práctica estándar en cualquier despliegue con túneles GRE/VPN.

## Despliegue sugerido

1. Un nodo canario (el 0000206, que es donde está reproducido), medir `time_appconnect` antes/después desde un invitado con MTU 1500 **sin tocar el invitado**.
2. Revertir el `mtu=1440` de vm541 y confirmar que el clamp solo ya lo sostiene.
3. Flota, por lotes, con la misma medición como criterio de aceptación.
4. Añadirlo a `install.sh` para que los nodos nuevos nazcan con ello.

## Pregunta abierta que NO está resuelta

**El desajuste de MTU es uniforme en la flota, pero el síntoma no.** La VM de Sergio (vm678, nodo 0000034, misma configuración y mismo MTU de túnel 1456) hace el mismo handshake en 0,03 s sin problema. Es decir: el desajuste es condición necesaria pero no suficiente — depende de si el ICMP «Packet Too Big» del camino de vuelta llega o no al servidor remoto, y eso puede variar por base o por destino.

Descartado por medición: saturación de NAT en las bases (conntrack 4.591 y 15.576 de 1.048.576, **0** `insert_failed`, **0** drops) y proxy en el invitado.

**Consecuencia:** el clamping arregla la clase entera de problemas sin necesidad de responder a esa pregunta, porque elimina los paquetes grandes de raíz. Pero conviene no vender que "ya sabemos por qué le pasaba a unos sí y a otros no", porque no lo sabemos.

## Implicación que merece una segunda mirada

Cualquier aplicación forzada a IPv4 sufre esto, no sólo la verificación de licencia de SQX. **Tenemos anotado que las descargas lentas de datos de SQX eran cosa de Dukascopy y no de infraestructura** ([[neuravps-sqx-dukascopy-datafeed-throttle]]). Con esta medición delante, esa conclusión merece revisarse: una descarga larga por IPv4 con paquetes grandes cayéndose y retransmitiéndose se ve exactamente igual que un servidor remoto lento.
