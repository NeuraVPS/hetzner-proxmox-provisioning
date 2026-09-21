# Política SMB entre cuentas en BASE

`base/snippets/base_smb_policy.py` es un planificador puro para limitar SMB
entre invitados al mismo titular, a una relación directa entre cuentas o a un
par de servidores aprobado expresamente. No consulta Firestore ni ejecuta
`nft`; el sincronizador de BASE debe hacerlo después de una lectura completa y
fresca, y abortar si cualquiera de esas lecturas falla.

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
reutilización ya no conserva esa combinación, el documento se rechaza antes de
modificar nftables. La observación de tráfico nunca añade partners.

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

Una vez validado, el llamador aplica `render_nft_update(plan, installed_mode)`
en una sola transacción `nft -f` y sólo entonces llama a `write_last_good`.
Un fallo de entrada o de nft deja intactos los sets y el fichero last-good
previo. Si el modo no cambia, la actualización sólo vacía y rellena los cuatro
sets, por lo que conserva los contadores de auditoría; si cambia, sustituye de
forma atómica únicamente la cadena `forward` de su propia tabla. El bootstrap
`render_nft_bootstrap(plan)` crea solamente la tabla propia
`inet nvx_smb_policy`; no usa `flush ruleset`.

La cadena se engancha a `forward` con prioridad `-5`, antes de la aceptación
SMB existente. Las reglas conocidas sólo cuentan y continúan: no reemplazan ni
saltan el límite de SYN SMB ya desplegado. Los candidatos son exclusivamente
dos direcciones registradas de invitados. Así el tráfico SNAT/NAT64 de BASE no
recibe un permiso genérico ni se bloquea por esta política.

En auditoría los candidatos no permitidos incrementan un contador y siguen al
firewall actual. En enforcement se descartan. Ambos casos comprueban la ida
TCP a 445 y la vuelta con origen 445 que no sea un SYN a secas; no se usa
`ct state new`, porque las rutas entre bases son asimétricas.

## Integración pendiente de revisión

1. Añadir al sincronizador existente la lectura completa y con timeout de las
   tres fuentes; no ejecutar una actualización desde un evento de una sola VM.
2. Instalar primero el bootstrap en modo `audit` y validar `nft -c -f` con los
   elementos reales. A continuación usar `render_nft_update` con el modo
   instalado para conservar los contadores de auditoría en sincronizaciones de
   inventario normales.
3. Revisar los contadores y aprobar uno a uno los `partnerPairs` necesarios.
   Sólo después cambiar el documento a `enforce` en una ventana revisada.
4. Para el cambio de modo, pasar el modo instalado a `render_nft_update`; ésta
   regenera de forma atómica la cadena de la tabla propia, conserva los sets y
   no recarga ni vacía el ruleset global.
