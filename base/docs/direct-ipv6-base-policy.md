# Entrada IPv6 controlada en las BASES

`firewall.ipv6Enabled` autoriza la entrada directa a la IPv6 pública del VPS:
prefijo `/64` público de cada BASE + sufijo VMID. Solo `true` explícito abre
esa entrada. Un campo ausente o `false` mantiene la protección. Las direcciones
IDENT son internas y no se publican como puntos de conexión de Internet.

La política se genera en `base/snippets/base_ipv6_policy.py` y se aplica desde
`sync-base-nat.py` bajo su mismo cerrojo. El trigger de la aplicación sincroniza
ambas BASES; ya no consulta reglas PVE por VM ni vacía conntrack del nodo.
No requiere `firewall=1` en `net0`, puentes extra, scripts Windows ni tareas
periódicas dentro de invitados. El aislamiento global L2/L3 del nodo continúa
siendo necesario para impedir que una VM evite la BASE.

## Semántica y límites

- IPv6 entrante directa: DNAT del destino público al IDENT correspondiente,
  **sin cambiar el puerto**. Esta traducción precede a los mapas 1xxxx/2xxxx/3xxxx
  para que un puerto alto de una dirección dedicada nunca alcance otra VM.
- `rdpEnabled`, `sambaEnabled` y `sshEnabled` restringen también sus puertos
  nativos por esta ruta. El cortafuegos de Windows conserva su función.
- La salida IPv6, las respuestas a conexiones salientes, los pools IPv4 y la
  entrada por VIP/puerto mantienen su funcionamiento independiente.
- Una desactivación elimina solo conntrack cuyo destino ORIGINAL es la IPv6
  pública afectada (y solo sus puertos si se desactiva un servicio). No se
  eliminan conexiones de salida ni sesiones de otros VPS. Es necesario porque
  el fast path de flowtable puede saltarse el filtrado de paquetes siguientes.
- Cada BASE conoce todas las rutas de invitados. Su IPv6 pública puede alcanzar
  un VPS de la otra región mientras esa BASE siga disponible. Los prefijos
  principales de las BASES **no son VIPs que migren automáticamente**; durante
  un fallo total se usa la dirección de la BASE superviviente.
- No hay soporte implícito para IPv6 antiguas derivadas del nodo. El plan aborta
  si un registro no coincide con IDENT + VMID, en vez de prometer una política
  que ese invitado podría evitar.

## Instalación y despliegue

Runtime: `BASE_IPV6_POLICY_ENABLED` desactivado si falta; una BASE nueva usa
`1` en el bootstrap actual. `MAIN_IPV6` debe ser la dirección real de ESA BASE;
`BASE_POLICY_UPLINK` es `enp2s0` en las ECC actuales. No copiar el prefijo de
la otra región.

1. Instalar `base_ipv6_policy.py`, `install-base-ipv6-policy.py` y el
   `sync-base-nat.py` de la misma revisión aprobada en `/usr/local/sbin/`.
2. Revisar `python3 /usr/local/sbin/install-base-ipv6-policy.py`; añadir
   `--apply` para persistir el include y la bandera. No recarga nftables.
3. Ejecutar una sincronización completa y verificar la tabla `inet neura_ipv6`
   y `/etc/nftables.d/base-ipv6-policy.nft` antes de publicar la versión del
   trigger de aplicación que deja de tocar PVE.
4. Probar con un VPS interno: IPv6 directa bloqueada/permitida, acceso por
   VIP sin cambios, permiso RDP independiente y cierre de conexión existente.
5. Verificar la segunda BASE y comparar los destinos permitidos con Firestore.

El trigger nuevo exige `--require-ipv6-policy`: una BASE con bandera apagada,
módulo ausente o tabla activa ausente falla en vez de confirmar un permiso
sin aplicar. Se despliega la función sólo después de comprobar ambas BASES.

Las actualizaciones usan transacciones de una sola tabla, no `flush ruleset`.
El include conserva la última política válida para arrancar sin Firestore.
Un error de lectura o de compilación no sustituye esa política por una vacía.
La sincronización completa lee Firestore DESPUÉS de tomar el cerrojo para no
reabrir un permiso con una lectura anterior a un toggle ya aplicado.

## Reversión

`python3 /usr/local/sbin/install-base-ipv6-policy.py --disable --apply` retira
solo esta tabla y su include, y pone la bandera a cero. Restaura la ausencia
de acceso IPv6 directo anterior a esta función; no modifica mapas VIP ni
pools. La versión de aplicación debe revertirse de forma coordinada para no
presentar como aplicado un control deshabilitado en las BASES. No recargar la
configuración nft completa de una BASE activa.

## Pruebas

- `pytest base/snippets/test_base_ipv6_policy.py`: identidad, valores por
  defecto, revocación limitada y conservación del archivo ante fallos.
- `python3 base/snippets/test-base-ipv6-netns.py`: TCP real dentro de namespaces
  privados, incluidos puertos altos, permisos independientes, recarga en frío
  y acceso VIP. Requiere `nft`, `ip`, `conntrack`, `unshare` y `nsenter`.
- Estas pruebas no sustituyen un ensayo físico de reconstrucción/fallo completo
  de BASE con systemd, Jool y cambios de propiedad Robot.

Referencias técnicas: [NAT y conntrack en nftables](https://netfilter.org/projects/nftables/manpage.html),
[fast path de flowtables](https://wiki.nftables.org/wiki-nftables/index.php/Flowtables).
