# Política SMB entre cuentas en BASE

`base/snippets/base_smb_policy.py` contiene un planificador puro para limitar SMB
entre invitados al mismo titular, a una relación directa entre cuentas o a un
par de servidores aprobado expresamente, y un reconciliador que consulta
Firestore con proyecciones, valida la lectura completa y aplica `nft`.
Una lectura fallida conserva la última política válida.

## Documento Firestore

El documento previsto es `config/smbPolicy`:

```json
{
  "schemaVersion": 1,
  "version": 1,
  "mode": "audit",
  "partnerPairs": [
    {
      "serverIdA": "firestore-server-id-a",
      "ownerUidA": "current-owner-uid-a",
      "serverIdB": "firestore-server-id-b",
      "ownerUidB": "current-owner-uid-b"
    }
  ]
}
```

`mode` omiso es `audit`; los únicos valores aceptados son `audit` y `enforce`.
`schemaVersion` y `version` son obligatorios. Cada partner fija el ID de ambos
documentos de servidor y sus propietarios actuales. Si una migración, baja o
reutilización ya no conserva esa combinación, esa excepción se omite y queda
una advertencia auditable con su índice. No hereda permiso por VMID ni por la
dirección nueva. Una estructura partner inválida, una versión no soportada o
una lectura incompleta sí rechazan toda la reconciliación. La observación de
tráfico nunca añade partners.

Las cuentas enlazadas se examinan como una arista directa y mutua de
`linkedAccountIds`: no se calcula cierre transitivo. Por ello revocar A↔B no
vuelve a autorizar A↔B a través de C.

## Reconciliación segura

El llamador debe obtener la colección completa de `servers`, la colección
completa de `users` y `config/smbPolicy`. Debe representar un error, timeout o
lectura parcial como `None`, que produce `InputUnavailable`; no se convierte en
una lista vacía. Se validan propietario, VMID único, direcciones de invitado
únicas y asociaciones partner vigentes. Las direcciones de tránsito de túnel
BASE no se aceptan como direcciones de invitados.

Los candidatos no dependen de que el documento siga presente: son el rango
canónico completo VMID 100–9999 (`10.64.<vmid_hi>.<vmid_lo>` y
`2a01:4f9:c01f:e::<vmid_hex>`). Una baja, reutilización de VMID o alta a medio
provisionar sigue siendo candidata a bloqueo. Sólo un documento completo cuya
pareja IPv4/IPv6 coincide exactamente con ese VMID añade una pareja permitida.
Tránsito `2a01:4f9:c01f:e:ffff::/112`, infraestructura BASE y direcciones
fuera del rango no reciben permiso ni bloqueo de esta política.

Un documento individual incompleto, con propietario ausente o VMID/IP duplicado
se omite con aviso y revoca sus permisos anteriores. Un fallo de la consulta
completa conserva la política instalada. `config/smbPolicy` puede faltar sólo
en el primer bootstrap audit; si desaparece después de un recibo enforce, la
reconciliación falla y no cae a audit.

Una vez validado, el llamador prepara atómicamente el include persistente,
aplica `render_nft_update(plan, installed_mode)` en una sola transacción
`nft -f` y sólo tras revocar las sesiones retiradas llama a `write_last_good`.
Si nft rechaza la transacción restaura el include anterior; el recibo sólo
certifica el éxito completo. Si el modo no cambia, la actualización sólo vacía
y rellena los cuatro sets, por lo que conserva los contadores de auditoría; si
cambia, sustituye de forma atómica únicamente la cadena `forward` de su propia
tabla. El bootstrap `render_nft_bootstrap(plan)` crea solamente la tabla propia
`inet nvx_smb_policy`; no usa `flush ruleset`.

La cadena se engancha a `forward` con prioridad `-5`, antes de la aceptación
SMB existente. Las reglas conocidas sólo cuentan y continúan: no reemplazan ni
saltan el límite de SYN SMB ya desplegado. El rango candidato deja fuera el
tráfico SNAT/NAT64 de BASE y tránsito, sin crearles un permiso genérico.

En auditoría los candidatos no permitidos incrementan un contador y siguen al
firewall actual. En enforcement se descartan. Tras retirar una pareja, el
runtime enumera sólo conntracks SMB candidatos y elimina las tuplas originales
exactas que ya no están permitidas; el kernel elimina su offload asociado. No
vacía conntrack ni reinicia la flowtable compartida. La cobertura incluye todos los
destinos TCP/UDP 135/137/138/139/445 que admite la cadena antigua,
con retorno TCP 135/139/445 que no sea un SYN a secas, y NetBIOS UDP
137→137 y 138→138. No se usa `ct state new`, porque las rutas entre bases son
asimétricas. Un paquete con origen TCP 137 no recibe ningún tratamiento de
retorno y sigue siendo candidato normal si intenta llegar a 445.

## Integración y despliegue gradual

1. Instalar el script y su include. El comando es
   `base_smb_policy.py sync-policy` (el alias `fullsync` existe). La CLI toma
   el mismo `flock` que el full-sync BASE, y dentro del bloqueo lee la presencia
   de `inet nvx_smb_policy` y el recibo last-good para conocer el modo instalado;
   no acepta un modo supuesto. El flag compatible `--installed-mode` sólo sirve
   para comprobar que coincide con el recibo. Una tabla ausente se reconstruye
   tras una lectura completa en el modo revisado de la configuración, también
   con recibo enforce. No llamarlo
   por evento de una sola VM: lee por sí mismo las tres fuentes completas con
   las proyecciones mínimas y sin secretos en sus logs.
2. Instalar primero el bootstrap en modo `audit` y validar `nft -c -f` con los
   elementos reales. A continuación usar `render_nft_update` con el modo
   instalado para conservar los contadores de auditoría en sincronizaciones de
   inventario normales.
3. Revisar los contadores y aprobar uno a uno los `partnerPairs` necesarios.
   Sólo después cambiar el documento a `enforce` en una ventana revisada.
4. Para el cambio de modo, pasar el modo instalado a `render_nft_update`; ésta
   regenera de forma atómica la cadena de la tabla propia, conserva los sets y
   no recarga ni vacía el ruleset global.

El bootstrap `base/base_setup.sh` instala el módulo, include y bandera
`BASE_SMB_POLICY_ENABLED=1`. En una BASE existente se instalan esos mismos
artefactos y se ejecuta `sync-base-nat.py sync policy`. Un full sync también
reconcilia la política, bajo el mismo cerrojo. No se añade un sondeo de Windows
ni una consulta de todas las cuentas en cada toggle del panel.

Los eventos de alta, baja, cambio de propietario, vinculación y
desvinculación usan `sync-base-nat.py sync policy --require-smb-policy`. La
bandera exige `BASE_SMB_POLICY_ENABLED=1`, el módulo instalado, tabla viva y
recibo de esquema actual; una BASE apagada, parcial o sin runtime seguro sale
con error para que el trigger reintente ambas bases.

**El despliegue inicial permanece en auditoría.** Antes de activar enforcement
hay que clasificar las conexiones entre cuentas observadas, registrar partners
verificados y conectar los eventos de alta/baja/vinculación/desvinculación al
refresco de permisos. Hasta entonces, el estado audit no promete aislamiento
por titular; conserva el filtrado de puertos, el aislamiento de nodo y los
accesos legítimos existentes. No debe cambiarse sólo el modo en Firestore y
considerar ese trabajo completado.
