# Pools IPv4 de salida: configuración y operación de las bases

**Activos desde el 19/09/2026 a las 15:32 UTC.** Para construir una base
vacía, seguir [el bootstrap completo](egress-pools-base-bootstrap.md).
Esta nota describe las piezas de egress y la política vigente.

| Región | Base actual | Bloque | IPs usadas |
|---|---|---|---|
| HEL | b1 `95.216.102.179` | `95.217.93.0/26` | `.1` a `.62` |
| FSN | b0 `116.202.118.221` | `91.98.53.128/26` | `.129` a `.190` |

Cada base está preparada para recibir cualquiera de los bloques durante una
sustitución de hardware. Los bloques permanecen anclados a su base; el
watchdog **no los mueve** durante un mantenimiento o failover normal
(`movePoolsOnFailover=false`). El cambio de hardware requiere el procedimiento
específico del [runbook de la aplicación, §8.2](https://github.com/NeuraVPS/NeuraVPS/blob/master/docs/EGRESS_IPV4_POOLS_ROLLOUT.md).

## Piezas versionadas en este repositorio

- `base/base_setup.sh`: valores de ejemplo de b0, ambos CIDRs, prefijos de
  identidad/tránsito, servicios y preparación de mapas. Adaptar región e IPs.
- `base/snippets/base-nat-boot.sh`: restaura **ambas** rutas blackhole de
  `EGRESS_POOL_CIDRS` antes de reconciliar los mapas. Evita que tráfico no
  solicitado al bloque rebote hacia Hetzner; las rutas locales /32 y las
  traducciones de conntrack establecidas conservan su funcionamiento.
- `base/snippets/persist-egress-pools-nft.py --apply`: declara los dos mapas y
  coloca el salto antes del SNAT general; actualiza vivo y persistencia.
- `base/snippets/sync-base-nat.py`: lee `config/egressPools` y el mirror
  `servers.egressIpv4`, aplica únicamente los mapas del bloque autorizado a
  `MAIN_IPV4` y guarda sus elementos en
  `/etc/nftables.d/base-nat-egress-pools.nft`.
- `base/sweepguard/deploy_guest_dst_scope.sh`: ambos /26 en las direcciones
  propias `ip rdpguard nuestras4`, en vivo y persistidas.
- `base/snippets/persist-audit.sh`: auditoría de rutas/túneles/persistencia.

El asignador, la conciliación con Hetzner, el panel y la CLI
`scripts/egress_pools_admin.py` están en **NeuraVPS/NeuraVPS**. El estado
de activación, propietarios y pares por VM está en **Firestore**. No copiar
credenciales ni ledgers de clientes a este repositorio ni recrear los pares
al reinstalar una base.

## Política vigente de activación

`sync-base-nat.py` permite la flota si `enabled=true`, `fleetWide=true` y se
cumple uno de los dos registros auditados: el aviso con su plazo original, o
una aprobación válida `fleetActivationApproval.mode=immediate_operator` con
`approvedAt` y `reason`. **Producción usa la segunda vía**, autorizada el 19/09;
`noticeCompletedAt` y `fleetNotBefore` quedan vacíos. No enviar avisos ni
inventar fechas de aviso para superar la comprobación. Las IPs compartidas
ya asignadas se muestran en el panel (`showToCustomers=true`, `effectiveFrom=null`).

La preparación inicial se hizo con mapas vacíos; ese es un resultado histórico,
no el estado actual. Ahora el mapa de la región propietaria está poblado. Una
base nueva todavía sin autorización del bloque debe tenerlo vacío; solo se
puebla tras confirmar enrutado y conceder su uso. Comparar siempre con el
estado actual de Firestore y Robot, no con un recuento histórico fijo.

## Validación y límites

Pruebas: `test_egress_pools_plan.py`, `test_sweepguard_dst.py` y
`test-egress-pools-netns.py`. El laboratorio de namespaces conserva una conexión
TCP al activar/revertir el mapa, prueba reserva cruzada, rechazo de config
inválida y recarga de la persistencia. También se verificaron canarios reales
y el traslado controlado de los /26. **No se ha ensayado un reinicio real de
las bases en producción tras activar estos pools**; una base nueva debe superar
la prueba de reinicio antes de recibir tráfico.

La sonda periódica en invitados no se amplía con este despliegue. La versión
anterior de `neuravps-egresscheck.py` se mantiene en producción; las pruebas de
dirección pública se hacen de forma acotada en canarios internos.

## Rollback con la flota activa

Desde la CLI de la aplicación: `set enabled false --apply` desactiva los mapas,
o `set fleetWide false --apply` deja solo los canarios. Verificar ambas bases;
ocultar las IPs del panel si se mantiene la retirada. Conservar el libro de
asignaciones y las rutas blackhole. Las conexiones establecidas mantienen su
dirección hasta que se cierren; **no vaciar conntrack ni recargar nftables.conf**.

Para retirar además la estructura en una intervención controlada:
`persist-egress-pools-nft.py --rollback --apply` quita solo el salto y lo
persiste. `--rollback` sin `--apply` es simulación. No restaurar un renderer
antiguo dejando mapas activos sin reconciliación, ni retirar rutas mientras
existan sesiones que usen el bloque. Las copias previas del despliegue inicial
se guardaron en `/root/egress-backup-20260919` en cada base.
