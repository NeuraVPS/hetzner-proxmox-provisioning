# Endurecimiento del aislamiento entre inquilinos — 18/09/2026

Cuatro huecos de aislamiento hallados el 18/09 (nota
`memory/neuravps-aislamiento-huecos-base-2026-09-18.md`), aprobados por el
operador. Todo se aplica en vivo de forma atómica (una transacción `nft -f`),
**sin `flush ruleset` ni recarga de `/etc/nftables.conf`**, y se persiste en el
fichero por separado. Cada cambio lleva su script de rollback dirigido.

## A) Regla de vuelta SMB entre túneles (base)

`base/snippets/deploy-sport-return-narrow.sh` · rollback
`rollback-sport-return-narrow.sh`.

La regla `tun-*->tun-*  th sport {135,137,138,139,445} accept` (respuestas SMB
por camino asimétrico entre regiones) solo miraba el puerto de ORIGEN, así que
un invitado que pusiera su puerto local en 137/138 abría CUALQUIER puerto (RDP,
SSH…) de otra VM. Se sustituye por:

    tcp sport {135,139,445} y flags != SYN-a-secas   (respuestas, no inician)
    udp 137->137 y 138->138                          (NetBIOS)

La entrada de SMB nuevo (regla `dport`) no se toca, así que "Mis Servidores"
(UNC a IPv6:445) y el SMB entre VMs del mismo cliente siguen igual.

**Medición previa (1 h, reglas solo-contador):** el único tráfico en las
categorías que la regla nueva descartaría fue de la VM de laboratorio del
operador. `m_attack_syn=0`, `m_udp_smb=0`, `m_udp_nbmis=0`. Lo legítimo
(`m_ret_ok`, respuestas SMB no-SYN) queda aceptado.

## C) DNAT IPv6 acotado por interfaz (base)

`base/snippets/deploy-dnat-iif-scope.sh` · rollback `rollback-dnat-iif-scope.sh`.

Las cuatro reglas de `ip6 nat prerouting` no filtraban por interfaz, así que una
conexión IPv6 de un invitado (iif `tun-*`) a cualquier dirección en un puerto
que fuera clave de un mapa acababa DNATeada a la VM de ese puerto. Se les añade
`iifname { "enp2s0", "veth-host" }`: enp2s0 = entrada pública; veth-host = la
vuelta de jool (el paquete NAT64 traducido reentra por ahí con destino la IP
principal v6 de la base). El invitado (`tun-*`) deja de poder DNATear.

## B1) Límite de ritmo al SMB entre VMs (base)

`base/snippets/deploy-smb-rate-limit.sh` · rollback `rollback-smb-rate-limit.sh`.

Techo por origen a las conexiones SMB **nuevas** (SYN a secas) a 139/445,
`RATE/min` (defecto 300). Se mide sobre el SYN, no sobre `ct state new`: por el
camino asimétrico conntrack cuenta como "new" todos los paquetes de una
transferencia en curso (miles/min), pero el ritmo de CONEXIONES nuevas reales
era ~0/min en la ventana de 1 h (`m_syn_over120 = 0`), mientras un burst de 40
subió el contador de SYN en 40. Así un par que copia ficheros sin parar no se
ve afectado; solo se frena quien ABRE conexiones a chorro. El contador
`smb_rl_drops` lo reporta `sweepguard.py` en cada pasada.

## D) Aislamiento L3 en el nodo (vmbr0<->vmbr0)

`base/snippets/nvx-aisla-l3.sh` + `.service`; despliegue de flota
`scripts/despliega-aislamiento-l3.py`; en `install.sh` para nodos nuevos;
vigilado en `node_health` (`@@AISLA3`).

`nvx-aisla` corta VM<->VM por capa 2. Pero el cliente es Administrador y puede
poner una dirección de ORIGEN ajena; el nodo entonces ENRUTA (capa 3) a la
vecina por `vmbr0`, saltándose la base. Se descarta el reenvío
`iifname "vmbr0" oifname "vmbr0"`. En operación normal ese reenvío no existe
(todo va por el túnel a la base). `MODE=count` (canario, solo cuenta) o `drop`.

Unidad con `After=` + `PartOf=nftables.service`, **nunca `Requires=`/`Wants=`**
(la trampa del `flush ruleset`; ver
`memory/neuravps-nftables-flush-ruleset-trampa.md`).

## Pendiente / decisión del operador

- **B2** (SMB por lista de pares permitidos, mismo dueño + enlazadas + grupos a
  mano): no se implementa; hay SMB legítimo entre cuentas distintas
  (TradersCoaching) que un filtro "mismo dueño" rompería. B1 lo mitiga por
  ritmo sin necesidad de conocer la propiedad.

## Aplicado y verificado (18/09/2026)

| | b0 | b1 | Verificación |
|---|---|---|---|
| C | 17:17:54Z | 17:19:21Z | RDP X.224 por las 4 direcciones (camino jool v4 y v6 nativo) y SMB público OK; `[2001:db8::1]:10863` desde 529 (FSN) y 1439 (HEL) ya no llega a 863; Internet v6 del invitado OK. Rollback real en b0: conf idéntica byte a byte. |
| A | 18:04:17Z | 18:05:03Z | Puerto de origen 137/138 hacia 3389/22 de 863: abría (control por b1 antes de aplicarlo), ya no abre por v4 ni v6; SMB 445 OK en los dos sentidos asimétricos (529→863 cruza b0 y vuelve por b1); «Mis Servidores» (UNC `\\<ipv6>.ipv6-literal.net\c$` con credencial) lista C:\ de 863 desde 1439 y desde 529. Rollback real en b0. |
| B1 | 18:09:03Z | 18:09:50Z | Ráfaga de 400 SYN desde un laboratorio: 315 conectan, 280 descartadas; SMB normal OK justo después; sweepguard lo reporta. Rollback real en b0. Sin `log` por paquete (la primera versión dejaba una línea por descarte). |
| D | 215/215 nodos en `drop` (18:17Z) | — | Canario en 2 nodos, luego 20, luego 193. Toda la flota en modo `count` antes: 0 paquetes legítimos vmbr0→vmbr0. Tras `drop`: RDP, SMB entre VMs y salida intactos; el envío con origen falso desde 1439 se descarta; la tabla sobrevive a `systemctl restart nftables`. |

conncheck en las dos direcciones tras cada paso: sin fallos nuevos (1227 `rdp_not_negotiating` y 975 colgada eran fallos del propio invitado, previos e independientes). egresscheck: 0 con salida rota.

**Medición de 1 h para A (17:03–18:03Z):** `m_attack_syn=0`, `m_udp_smb=0`, `m_udp_nbmis=0`, `m_udp_nb_ok=0`; `m_tcp_137138=4` = las cuatro sondas de laboratorio (tuplas registradas). Respuestas legítimas conservadas: `m_ret_ok` 1.510 (b0) / 70.290 (b1). SYN SMB por origen: `m_syn_over120=0` en las dos bases.
