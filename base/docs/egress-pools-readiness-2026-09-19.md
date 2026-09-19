# Preparación de bases para los pools IPv4 de salida

Los bloques HEL `95.217.93.0/26` y FSN `91.98.53.128/26` pertenecen respectivamente
a las bases `95.216.102.179` y `116.202.118.221`. Cada base queda preparada para
recibir cualquiera de los bloques durante una sustitución de hardware.

- `EGRESS_POOL_CIDRS` en `/etc/default/base-nat` repone las rutas blackhole al
  ejecutar `base-nat-boot.sh`. Impide que tráfico no solicitado al bloque rebote
  hacia la puerta de Hetzner. Las rutas locales /32 y conntrack tienen prioridad.
- Los dos bloques forman parte del set `ip rdpguard nuestras4`: al actualizar
  una base existente hay que añadirlos al set vivo y a su definición persistida.
- `persist-egress-pools-nft.py --apply` instala las dos tablas de asignaciones
  vacías y el salto anterior al SNAT general. Es una transacción incremental;
  **no recargar nftables.conf ni borrar conntrack**.
- `sync-base-nat.py` conserva la limitación a canarios hasta que exista evidencia
  `noticeCompletedAt` y `fleetNotBefore` con siete días completos de separación,
  la fecha haya llegado y `fleetWide=true`. Fechas ISO UTC o timestamps aware.
  Mantener ambos registros después de activar la flota.

La preparación se aplica primero en b0 y luego en b1 con copias de los ficheros
previos y comprobación de hashes. Los mapas permanecen vacíos con `enabled=false`.
No se cambia la selección de VIPs ni las sesiones existentes.

Validación del cambio: `test_egress_pools_plan.py`, `test_sweepguard_dst.py` y
`test-egress-pools-netns.py`. La prueba de namespaces conserva una conexión TCP
abierta al activar y revertir el mapa, prueba reserva cruzada, rechazo de config
inválida y recarga del estado persistido en un entorno aislado. No equivale a
haber reiniciado una base de producción; ese ensayo pertenece a una ventana de
mantenimiento que contemple las conexiones de los clientes.

La sonda periódica en invitados no se amplía con este despliegue. La versión
anterior de `neuravps-egresscheck.py` se mantiene en producción; las verificaciones
adicionales de dirección pública se hacen de forma acotada en canarios internos.

Rollback: `persist-egress-pools-nft.py --rollback` retira solamente el salto; las
conexiones ya establecidas mantienen la dirección hasta cerrarse. Restaurar los
ficheros de la copia previa si se retira el código. No quitar rutas de los bloques
mientras queden sesiones que utilicen sus direcciones.
