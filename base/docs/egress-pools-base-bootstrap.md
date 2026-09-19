# Bootstrap de una BASE nueva con pools IPv4 de salida

Esta guía prepara una BASE nueva para participar en la flota que ya usa pools
IPv4 por VM. Es complementaria a la topología de
[`netns-jool-nat46-nat66-guide.md`](netns-jool-nat46-nat66-guide.md), la
protección de destinos de [`../sweepguard/README.md`](../sweepguard/README.md)
y el renderer de [`../snippets/sync-base-nat.py`](../snippets/sync-base-nat.py).

No crea ni modifica `egressAssignments`, `servers.egressIpv4` ni
`config/egressPools`; esos datos pertenecen a la aplicación y ya son el origen
de verdad. Tampoco mueve una VIP, un bloque /26, ni cambia el estado de la
flota. Toda operación que cambie Robot, Firestore o una BASE activa requiere su
propia autorización y ventana.

La política actual permite activación inmediata solo cuando la aplicación ha
registrado una `fleetActivationApproval` válida. Una BASE no fabrica esa
aprobación, ni rellena `noticeCompletedAt` o `fleetNotBefore`.

## Alcance y fuentes de verdad

- El código de BASE se toma de la revisión aprobada de este repositorio. No se
  copian scripts desde una BASE activa como mecanismo de instalación.
- La configuración, pares y propiedad del bloque se leen de Firestore mediante
  `config/egressPools` y `servers.egressIpv4`. El renderer solo instala mapas
  para el bloque cuyo `activeServerIp` coincide con `MAIN_IPV4` local.
- Robot es la fuente de la ubicación de los /26 y del servidor que los sostiene.
  Confirmar por GET antes y después de cualquier cambio; un timeout de POST no
  autoriza repetirlo.
- La guía de hardware ECC describe una sustitución completa y contiene contexto
  histórico de SNAT/VIPs y servidores antiguos. No aplicar sus ejemplos de SNAT
  o identificadores heredados sin contrastarlos con esta guía y la configuración
  viva.

## Matriz de configuración local

Los valores de dirección pública y de VIP se leen del inventario y Robot de la
ventana, no se derivan de esta tabla. El ejemplo de `base_setup.sh` es b0 y debe
adaptarse: no es un instalador único ni una configuración portable de b1.

| Variable o papel | b0 (Falkenstein) | b1 (Helsinki) | Regla |
|---|---|---|---|
| `MAIN_IPV4` | `116.202.118.221` | `95.216.102.179` | Direcciones actuales; sustituir por las del hierro nuevo. |
| `MAIN_IPV6` | `2a01:4f8:2b01:124::2` | `2a01:4f9:2a:2d56::2` | Direcciones actuales; sustituir por las del hierro nuevo. |
| `FAILOVER_IPV4` | `94.130.3.118` | `77.42.49.79` | VIP regional, presente localmente antes de arrancar `base-nat-boot`. |
| `FAILOVER_IPV6` | `2a01:4f8:fff2:95::2` | `2a01:4f9:fff1:5f::2` | Presencia local no demuestra propiedad en Robot. |
| `HOME_REGION` | `fsn` | `hel` | En `/etc/default/neuravps-base-tunnels`. |
| `TUNNEL_IFACE_PREFIX` | `tun-f` | `tun-h` | En `/etc/default/base-nat`; no intercambiar. |
| `IDENT_PREFIX` | `2a01:4f9:c01f:e::/64` | igual | Necesario para rutas IDENT por VM. |
| `TRANSIT_PREFIX` | `2a01:4f9:c01f:e:ffff::/112` | igual | Reservado al tránsito de túneles. |
| `VM_V4_PREFIX` | `10.64.0.0/16` | igual | Solo las IPv4 privadas de VM entran en los mapas. |
| `EGRESS_POOL_CIDRS` | `95.217.93.0/26 91.98.53.128/26` | igual | Rutas blackhole en ambas BASES. |
| `BASE_HOSTS` | BASES autorizadas durante cutover | idéntico | Incluir las que coexistan con alias distintos; no retirar la vieja antes del relevo. |

Los valores de `FAILOVER_*`, VIPs, gateway, interfaz, EAMT Jool y
`BASE_HOSTS` se anotan en el recibo de la ventana. Nunca reutilizar los valores
del bloque comentado de b0 sin sustituirlos.

## Prerrequisitos antes de instalar

1. Reservar el hierro en la misma ubicación que las failover IP que pueda recibir.
   Confirmar con Robot que los /26 siguen en su BASE activa; preparar una BASE no
   implica concederle bloques.
2. Tener la red de la máquina, VIPs con `preferred_lft 0`, Jool/NAT46/NAT66 y el
   ruleset estático de `nftables` según la guía Jool. Debe incluir el SNAT general
   de seguridad y la cadena `postrouting` que el renderer podrá ampliar.
3. Crear `/var/lib/base-nat` antes de iniciar `nftables`; instalar y arrancar
   `veth-host.service` antes de la primera carga de reglas.
4. Desde una BASE ya permitida, añadir el /64 nuevo al `[IPSET base]` de
   `cluster.fw` y propagarlo a nodos. La BASE nueva no puede hacer este bootstrap
   por sí misma.
5. Preparar secretos fuera del repositorio: service account Firebase en
   `/etc/firebase-credentials.json` con modo `0600`, credenciales del Storage Box
   para `cluster.fw`, token DNS si se emite TLS y secreto/URL de `pve-set-ticket`
   si se instala el proxy. Verificar que la cuenta Firebase puede leer
   `servers`, `proxmox_nodes` y `config/egressPools`.
6. Coordinar con aplicación que los triggers, reglas Firestore y las listas de
   `BASE_SSH_HOST_IPS` ya conocen las BASES que coexistirán. La BASE no sustituye
   el despliegue completo de Cloud Functions. Un hierro nuevo necesita además
   su entrada completa en `functions/failover_watchdog.py` y selección válida
   en `config/failover_watchdog.activeBases`; no admite una IP arbitraria.
   [Selección de hardware de la aplicación](https://github.com/NeuraVPS/NeuraVPS/blob/master/docs/BASE_FAILOVER_HARDWARE_SELECTION.md).

## Identidad de nodo, instaladores y primer arranque

Los **nodos Proxmox nuevos** usan [`../../install.sh`](../../install.sh) y
[`../../first_boot.sh`](../../first_boot.sh); no existe `first_login.sh`.
El instalador ya prepara rutas de invitados por GRE a las VIPs, MSS y
`rp_filter` persistente. No necesita conocer los /26: el SNAT y el reparto
por VM residen en las BASES y en la aplicación.

Una BASE no es un nodo Proxmox de cliente. No añadirla a `proxmox_nodes`, no
asignarle un `proxmoxId` y no ejecutar instaladores de invitado para marcarla
como preparada. Su identidad operativa es el alias de BASE, sus direcciones de
red y el conjunto de VIPs autorizado para la ventana.

La preparación se realiza en el primer arranque del hierro, antes de darle una
VIP o hacerlo salto de operaciones. No se difiere a un "first login" manual:
los servicios `veth-host`, nftables, túneles y `base-nat-boot` deben quedar
habilitados y comprobados por systemd. El acceso interactivo posterior sirve
para revisar el recibo, no para completar pasos que condicionan el enrutado.

La guía Jool es la topología inicial. Para el modelo actual hacen falta además
las reglas de [`persist-egress-nft.py`](../snippets/persist-egress-nft.py)
(SNAT general `10.64/16` y `10.65/16`, salida IPv6, GRE y forward de túneles),
las dos canónicas **IPv6** de
[`canon-snat-por-vip.py`](../snippets/canon-snat-por-vip.py) y el
[aislamiento vigente](aislamiento-endurecimiento-2026-09-18.md).
El primer helper lleva las IPs y la interfaz de las ECC actuales: adaptarlo al
hierro nuevo antes de aplicarlo; persiste reglas, no las carga en vivo.
En una BASE fría se carga la configuración completa antes de crear túneles y
repoblar mapas. En una BASE activa no se recarga globalmente.
No confundir las canónicas IPv6 con la propuesta histórica descartada de SNAT
**IPv4** a la VIP: la salida IPv4 usa pools y conserva el fallback a la IP
principal. `persist-egress-pools-nft.py` amplía esa topología con su salto.

El fichero de entorno de la BASE debe pertenecer a `root`, no contener secretos
en texto de repositorio y conservar permisos restrictivos. El script de boot no
puede reparar una identidad de red errónea: solo espera las direcciones exigidas,
restaura rutas blackhole y sincroniza el estado que ya está autorizado.

## Instalación en frío

Ejecutar por fases y detenerse ante un fallo. `base_setup.sh` descarga y habilita
varias piezas, pero presupone la topología Jool/nft, las credenciales y valores
por BASE; no promete una reconstrucción E2E por un único comando.

1. Partir de una revisión fijada del repositorio y revisar los hashes de:
   `sync-base-nat.py`, `base-nat-boot.sh`, su unidad, los scripts de túnel,
   `persist-egress-pools-nft.py`, `persist-audit.sh` y
   `zz-neuravps-rpfilter.conf`. Fijar `PROVISIONING_REF` al SHA revisado antes
   de ejecutar los pasos de `base_setup.sh`: sus descargas usarán esa revisión
   (el valor por defecto es `master`).
2. Instalar dependencias y el runtime Firebase según `base_setup.sh`. Instalar
   la credencial antes de habilitar `base-nat-boot.service`.
3. Escribir `/etc/default/base-nat` con la matriz anterior, incluidos
   `IDENT_PREFIX`, `TRANSIT_PREFIX`, `VM_V4_PREFIX`, `TUNNEL_IFACE_PREFIX`,
   `EGRESS_POOL_CIDRS`, el estado local y `BASE_HOSTS`.
4. Crear los includes vacíos bajo `/etc/nftables.d/`, instalar el drop-in de
   `nftables`, validar el ruleset con `nft -c -f /etc/nftables.conf` y arrancar
   el servicio. El include de egress usa comodín, pero su ausencia no sustituye
   declarar los mapas y el salto.
5. Instalar `neuravps-base-tunnels.service`, el fichero de región, la tabla de
   nodos y `zz-neuravps-rpfilter.conf`; recargar sysctl y confirmar que cada
   `tun-*` tiene `rp_filter=0`. Instalar los túneles antes de la sincronización
   final de rutas por VM. La tabla `/etc/neuravps/tunnel-nodes.conf` se genera
   con `sync-base-nat.py sync nodes`; instalar primero nginx, mapa y certificado
   para que ese sync pueda validarlos. No sembrar la tabla a mano.
6. Instalar `base-nat-boot.service` y `sync-base-nat.py`. El boot restaura las
   dos rutas blackhole antes de consultar Firestore. No recargar `nftables` en
   una BASE activa para aplicar pools y nunca vaciar conntrack.
7. Instalar `persist-egress-pools-nft.py` y ejecutar:

   ```bash
   python3 /usr/local/sbin/persist-egress-pools-nft.py --apply
   ```

   El bootstrap aborta si falla su descarga o aplicación.
8. Instalar sweepguard antes de añadir el filtro por destinos. Sobre las tablas
   `rdpguard` iniciales de la guía Jool, los helpers están en `base/sweepguard/`:
   `deploy_sweepguard.sh`, `deploy_portguard.sh`, `extend_smb_range.sh`,
   `extend_ssh_range.sh`, `deploy_guest_egress_exemption.sh` y finalmente
   `deploy_guest_dst_scope.sh`, junto a `sweepguard.py`. Revisar cada resultado:
   el último requiere las exenciones de los anteriores. Incluye los dos /26 en
   `nuestras4`; para hierro nuevo actualizar también sus IPs principales en
   `nuestras4/6` y las listas permitidas. Confirmar reglas y sets en vivo y
   persistidos. El endurecimiento de aislamiento se aplica después de construir
   los forwards que modifica, antes de recibir tráfico.
9. Ejecutar `systemctl enable --now neuravps-base-tunnels.service` y
   `systemctl enable --now base-nat-boot.service`, seguido de:

   ```bash
   # Instalar el auditor desde el checkout fijado (raíz del repositorio).
   install -m 0755 base/snippets/persist-audit.sh /usr/local/sbin/persist-audit.sh
   python3 /usr/local/sbin/sync-base-nat.py sync
   /usr/local/sbin/persist-audit.sh base
   ```

   El sync completo lee la configuración vigente y escribe los elementos de los
   mapas solo si la BASE es propietaria del bloque según `activeServerIp`.

## Validación obligatoria antes de una VIP

No usar conteos fijos: comparar los mapas y los ficheros persistidos contra la
configuración y pares vigentes, y confirmar que el bloque no se anuncia desde
dos BASES.

1. `systemctl is-enabled` e `is-active` para nftables, veth, Jool, nginx, túneles y
   `base-nat-boot`; revisar que la unidad de boot no agotó sus reintentos.
2. Confirmar dos rutas blackhole de `EGRESS_POOL_CIDRS`, mapas `egress_hel4` y
   `egress_fsn4`, salto `egress_pools` antes del SNAT general e include
   `base-nat-egress-pools*.nft` en `/etc/nftables.conf`.
3. Confirmar rutas `/128` IPv6 y `/32` IPv4 de las VMs del modelo IDENT hacia
   el túnel de su nodo, y que `base-nat-egress-pools.nft` y los mapas vivos tienen
   exactamente los mismos elementos. Ejecutar `persist-audit.sh base` y revisar
   también el rp_filter efectivo por interfaz, no solo `all/default`.
4. Ejecutar `test_egress_pools_plan.py`, `test_sweepguard_dst.py` y
   `test-egress-pools-netns.py` desde la revisión fijada. No instalar una sonda
   recurrente dentro de invitados; las comprobaciones guest se limitan a pruebas
   aprobadas y acotadas.
5. Reiniciar la BASE fría y repetir los puntos 1–3. Esta prueba es obligatoria
   antes de que reciba una VIP o un /26; no se ha sustituido por la preparación
   de las BASES activas.
6. Conservar un recibo con hashes, configuración sin secretos, estado de
   servicios, blackholes, resultado de auditoría y GET de Robot. No incluir
   secretos, direcciones de clientes ni contenido de Firestore.

## Entrada coordinada y rollback

El estado de control, asignador, CLI, panel y procedimiento completo están en
el [runbook de la aplicación, §8.2](https://github.com/NeuraVPS/NeuraVPS/blob/master/docs/EGRESS_IPV4_POOLS_ROLLOUT.md).
No ejecutar `init` ni `backfill` para reconstruir una base: volver a leer el
ledger existente. Los secretos se recuperan de sus almacenes autorizados y el
estado de sus respaldos; ninguno se publica en Git.

Solo después de la validación y de la aprobación de la ventana: confirmar
`activeServerIp` y Robot, mover un bloque mediante el flujo de aplicación que
revoca, espera y concede, y verificar la convergencia de ambos renderers. El
`failover_watchdog` conserva `movePoolsOnFailover=false`; una conmutación normal
de VIP no mueve los /26.

Si la BASE nueva falla antes de recibir tráfico, retirar su preparación no debe
tocar ledger, configuración de flota, bloques ni conntrack de las BASES activas.
Tras recibir tráfico, seguir el rollback coordinado del reemplazo de hardware,
restaurando destinos de Robot y permisos de uso al destino sano validado. El
rollback global de pools es una operación distinta descrita en
[la guía de operación](egress-pools-readiness-2026-09-19.md). Restaurar un
fichero antiguo por sí solo no corrige mapas persistidos.
