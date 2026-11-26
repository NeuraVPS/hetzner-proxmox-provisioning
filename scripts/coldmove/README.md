# coldmove — traslado en frío con precopia ZFS

Sirve para mover una VM de un nodo a otro **parada**, con un corte de segundos o de un minuto en vez de
la hora que tardaría `migrate_vm.sh` copiando el disco entero con la VM apagada. Es el kit `rtmig_*`
de los traslados EX44→AX162 del 01/10 al 18/11/2026 (cortes de ~50–60 s), ahora parametrizado.

Cuándo se usa:
- Cuando no se puede migrar en caliente: cualquier cambio de silicio (vendor|family|model; por
  ejemplo AX162 ↔ AX102 ↔ 0000266-KDM-9755), sea cual sea el modelo de CPU. `migrate_vm.sh` lo
  rechaza en caliente.
- Cuando el disco es grande o el enlace es lento (no-Hetzner).

Siempre con el **cliente avisado de la hora del corte**.

## Piezas
| Fichero | Dónde corre | Qué hace |
|---|---|---|
| `rtmig_deploy.sh` | equipo del operador | copia el kit a `/root/rtmig/` de los dos nodos, pasando por una BASE (`JUMP`) |
| `rtmig_send.sh` | nodo ORIGEN | `base` (snapshot y envío completo), `incr`, `verify`, `pending`, `last`, `resume` |
| `rtmig_cutover.sh` | nodo ORIGEN | `shutdown`, `park`, `push`, `start`, `close`, `rollback`, `status` |
| `rtmig_common.sh` | (lo cargan los dos) | parámetros y su validación, SSH al destino, identidad, sondas, estado, inventario de discos |

**Parámetros comunes, por entorno:**
- `VMID`.
- `DST`: la IP del destino, tal cual está en `proxmox_nodes/<nodo>.ip`, en forma canónica y sin zona
  (`%eth0`). En una BASE: `jq -r '."<nodo>"' /var/lib/base-nat/pve_nodes.json`.
- `DST_NODE`: el id del nodo destino. Se comprueba con `hostname` antes de hacer nada.
- Opcionales: `WORK` (por defecto `/root/rtmig`), `SSH_PORT` y `DISKS`. Si se pone `DISKS`, tiene
  que coincidir exactamente con el inventario de la config.

**Inventario de discos.** Se lee de la config completa (todas las secciones, incluidos los `unusedN`).
Solo se admiten zvols `local-zfs:vm-<VMID>-disk-N`. Cualquier otro volumen hace abortar: otro
almacenamiento, el disco de otra VM, `base-`, `vmstate` o una ruta cruda. Los CD-ROM no cuentan.

**Fases, estado y cerrojo.** `park` abre `$WORK/<VMID>.state`, que liga el traslado a la VMID, al UUID
(`smbios1`), al nodo origen, al nodo e IP destino, al inventario y, desde `push`, al snapshot final
y al sha256 de la config publicada. También liga el pool (`POOLDS`) y el sha256 del `.fw`.
- Fases: `parked → publishing → pushed → starting → started → done`, o
  `rollingback → rolledback`.
- Las fases intermedias (`publishing`, `starting`, `rollingback`) se registran **antes** de actuar:
  si se pierde la confirmación, se repite la misma orden y el kit reconcilia lo que encuentra. Por
  ejemplo, si la VM ya está corriendo en destino con la config publicada, `start` solo marca
  `started`.
- Cada paso exige la fase anterior y los mismos datos.
- Solo puede correr un proceso del kit por VM a la vez (`$WORK/<VMID>.lock`).
- **Nada ajeno se toca.** El kit solo retira del destino una config con el UUID del traslado, y un
  `.fw` que sea de esa config o tenga el sha256 que publicó. Una config o un `.fw` de otra VM con
  la misma VMID hacen abortar.

**Respuestas positivas.** Un fallo de SSH nunca se toma por «no hay config». Cada consulta al destino
tiene que traer su marcador final; si no, el estado es **INDETERMINADO** y el paso aborta sin tocar
nada. En las consultas ZFS solo cuenta como «no existe» el mensaje `dataset does not exist`;
cualquier otro error de `zfs` también es INDETERMINADO. Un disco ya verificado en `push` que deja de
dar un número de bytes escritos impide el rollback.

El código no lleva IPs, VMIDs ni credenciales: usa la clave de la flota `~/.ssh/neuravps_id`, que ya
está en cada nodo.

**Red:** va nodo↔nodo por SSH. El destino tiene que aceptar al origen en el puerto 22, igual que
cualquier nodo de la flota: los dos tienen que estar en `proxmox_nodes` y haber pasado
`sync-base-nat.py sync nodes` y `sync-firewall` (IPSET `hosts-ipv6`).

**Ritmo:** `RATE` por defecto es `30m`, unos 250 Mbit/s por flujo; está pensado para un enlace
no-Hetzner de ~300 Mbit/s, como Kademar. Dentro de Hetzner se puede subir a `60m`–`110m`. En la
incremental final, con la VM ya apagada, se usa `RATE=0`. `ZSTD=1` recomprime en el cable si el cuello
es la red.

## Flujo
En el nodo origen: `cd /root/rtmig && export VMID=… DST=… DST_NODE=…`.

1. **Precopia, con la VM encendida** (lanzada con `systemd-run` o `nice`, que tarda horas):
   `./rtmig_send.sh base rtmig-$(date -u +%Y%m%dT%H%MZ)` → tiene que acabar en `VERIFY OK` en todos
   los discos. **Si se corta, se repite la MISMA orden con el MISMO nombre:** salta los discos que ya
   están, reanuda el que tiene token y envía los que faltan. Lo mismo vale para `incr`.
2. **Incrementales en caliente**, hasta que lo pendiente sea poco
   (`./rtmig_send.sh pending` lo estima):
   `DROP_PREV=1 ./rtmig_send.sh incr auto rtmig-<nueva>`. La base de cada incremental se guarda en
   `$WORK/<VMID>.incr-<nueva>.prev`, así que repetir la misma orden, aunque ya hayan llegado todos
   los discos, usa la misma base, verifica por guid y limpia de forma idempotente.
3. **Corte, a la hora pactada:**
   1. `./rtmig_cutover.sh shutdown 600`: primero las comprobaciones previas, **antes de apagar**:
      - que la config **no tenga cambios pendientes**: ninguna sección `[PENDING]`, reconocida como
        en PVE, sin distinguir mayúsculas y con espacios finales. PVE los aplica al parar la VM, así
        que la config validada no sería la que se traslada.
        - Si los hay, se aplican (reiniciando la VM) o se revierten (`qm set <VMID> --revert
          <claves>`) y se vuelve a lanzar.
        - Si la config no se puede leer bien, también aborta.
        - Justo antes de `qm shutdown` se vuelve a leer: tiene que seguir limpia y ser idéntica (sha256)
          a la comprobada.
        - Tras el apagado se compara otra vez. Si alguien metió un cambio pendiente durante el
          apagado, PVE lo aplica al parar: `shutdown` lo detecta y `park` no aparca esa config;
        - `park` copia primero a un fichero provisional. Solo lo promueve a la copia aparcada si su
          sha256 es el que comprobó `shutdown`, y si el origen sigue siendo idéntico. Si no, la
          config se queda en el origen y lo provisional se descarta;
      - inventario de discos, CPU y licencias (ver abajo; el `ACK` va ya en esta orden) y destino
        libre.

      Luego viene el apagado ordenado. Si Windows no se apaga, **no se fuerza**: decide una persona.
   2. `./rtmig_cutover.sh park`: exporta el historial del reconciliador de globo (si el origen lo
      tiene) y aparca la config en `$WORK`. Así nadie puede arrancar la VM en origen. Los zvols se
      quedan.
   3. `RATE=0 ./rtmig_send.sh incr auto rtmig-final-<fecha>`: incremental final, con `VERIFY OK`.
   4. `./rtmig_cutover.sh push rtmig-final-<fecha> [balloon_mb]`:
      - exige que el origen siga sin config (respuesta positiva);
      - comprueba guid y «0 B escritos después» en cada disco del inventario;
      - escribe primero el `.fw` y **publica la config al final**, ya con la CPU y el balloon
        aplicados. Cada fichero se escribe a `<ruta>.rtmig-tmp`, se comprueba allí su sha256 y solo
        entonces se renombra con `mv -n`, que pmxcfs hace atómico. Después se relee y se comparan
        el sha256 y el UUID;
      - si el origen no tiene `.fw`, el destino tampoco puede tenerlo;
      - con un cambio `[PENDING]` de cpu o balloon que haya que fijar, se niega (segundo seguro: el
        primero es que `shutdown` ya no deja apagar con cambios pendientes);
      - la decisión de CPU y el aviso quedan en el estado antes de publicar. El cierre del push es
        uno solo para el camino normal y para la reanudación: verifica la config y el `.fw`, deja
        la constancia en `cpu-change.txt` una sola vez e importa el historial del globo una sola
        vez;
      - importa el historial del globo, si el destino tiene reconciliador.
      - Una VPS E que viene de un EX44 hacia una caja SQX (AX162, KDM) lleva `47104`.
        `migrate_vm.sh` lo vuelve a ajustar por `nodeType`.
   5. `./rtmig_cutover.sh start`: solo tras un `push` completo, y con la config del destino igual a la
      publicada.
4. **En una BASE:** `migrate_vm.sh <VMID> <número del nodo destino>`. Entra por la rama
   «VM is already on dest» y solo reconcilia:
   - NAT local, hookscript y Firestore (`nodeId`); el disparador mueve las rutas de la otra base;
   - globo de la VPS E por `nodeType`;
   - sonda RDP;
   - **`w32tm /resync`** por el guest agent. Si Windows aún arranca, solo deja una nota: al arrancar
     ya sincroniza.
   - Hay que comprobar que imprime `Firestore updated.`.
5. **Verificar desde fuera:** RDP, SMB y SSH por las rutas del cliente. Después, quitar `maintenance`
   si se puso.
6. `./rtmig_cutover.sh close`: cierra el estado (fase `done`) y archiva la copia aparcada.

El origen se queda con los zvols y los snapshots `@rtmig-*`. Se borran **a mano** pasados los días de
keep acordados (por ejemplo, con `KEEP='hasta AAAA-MM-DD'` en `rtmig_send.sh`), después de verificar.

## CPU y licencias (`shutdown` y `push`)
La regla es la misma que en `migrate_vm.sh` y en el defrag. La vía fría directa de `migrate_vm.sh`
(VM parada, sin kit) pide la misma constancia: `ACK_HWID_CHANGE=<VMID>` y `CPU_NOTICE`.
- **Modelo genérico (`x86-64-v*`):** el Hardware ID de SQX no cambia; cualquier destino del mismo
  nivel vale. Un `x86-64-v4` hacia un destino sin AVX-512 no arrancaría, así que se rechaza salvo con
  `CHANGE_CPU=1`.
- **Cambio de modelo pendiente.** `shutdown` lee con qué CPU corre de verdad la VM (la línea `-cpu`
  de QEMU) y la compara con la del próximo arranque. Si son de clase distinta, por ejemplo `host` en
  marcha con la config ya en `x86-64-v4`, el Hardware ID cambia aunque el silicio no cambie, y se
  pide la misma constancia. La CPU en marcha queda en el estado (`RUN_CPU`) para `push`.
- **`cpu: host`/`max`, otro modelo con nombre o sin línea `cpu`, cuando el silicio cambia**
  (vendor|family|model): el Hardware ID cambia. `shutdown` y `push` se niegan si no llevan una de
  estas:
  - `CHANGE_CPU=1`: pone la CPU de las altas (`x86-64-v4`, o v3 sin AVX-512).
  - `ACK_HWID_CHANGE=<VMID>`: deja la CPU tal cual.
  - En los dos casos hace falta `CPU_NOTICE='<cuándo y por dónde se avisó al cliente>'`.
- **Hacia Kademar (`DST_NODE` `*-KDM-*`, 26/11/2026) solo entra CPU genérica.** Una VM `host`/`max`,
  `kvm64` o con modelo con nombre exige `CHANGE_CPU=1` (`ACK_HWID_CHANGE` se rechaza en `shutdown`, antes
  de apagar nada): es la única CPU con la que podrá volver a salir EN CALIENTE (`migrate_vm.sh`,
  Genoa ↔ Turin) si Kademar se avería. El aviso al cliente hace falta igual, porque el silicio cambia.
  Con CPU genérica, mejor que el kit: `migrate_vm.sh` en caliente (ver `docs/KADEMAR_NODE.md`).
- Todo queda en `$WORK/cpu-change.txt`, una vez publicada la config.

## Rollback
Antes de `park`, la VM sigue en el origen: basta con no seguir. Si ya está aparcada (fases `parked`,
`pushed` o `started`):
1. Si la VM corre en el destino, apagarla **ordenadamente** allí (`qm shutdown`), con la decisión de
   una persona.
2. `./rtmig_cutover.sh rollback`. Exige una respuesta positiva del destino: config ausente, o
   presente y parada.
   - **Si la VM llegó a arrancar en el destino, o sus discos tienen escrituras después del snapshot
     final,** volver al origen pierde ese trabajo. Hace falta `ACK_DISCARD_DEST_WRITES=<VMID>` y queda
     anotado en el estado.
   - Copia la config y el `.fw` del destino a su `$WORK` (`*.conf.dst-<fecha>` y `*.fw.dst-<fecha>`),
     los retira y confirma que ya no están.
   - Restaura en el origen el `.fw` y la config **por separado**, cada uno con un fichero temporal
     y `mv -n`, y comprueba el sha256 de cada uno contra la copia aparcada (o la ausencia del
     `.fw`, si no lo había). **Solo arranca con los dos verificados**; si se corta, la misma orden
     lo retoma.
   - Escribe la fase `rolledback` y después archiva la copia aparcada (`*.rolledback-<fecha>`): no
     se reutiliza nunca. Si se corta entre las dos cosas, el siguiente paso del kit termina el
     archivado.
   - Los discos de los dos lados se quedan.
3. Si `migrate_vm.sh` ya se había ejecutado: `migrate_vm.sh <VMID> <número del nodo origen>`, que
   vuelve a reconciliar rutas y Firestore hacia el origen.

Si el SSH cae a mitad del rollback, el paso aborta como INDETERMINADO. El origen no arranca, y se
repite cuando el destino conteste.

## Apagado
`shutdown` nunca usa `forceStop`. Si vence el tiempo, la VM sigue encendida en el origen y no se ha
tocado nada. Hay que mirar por qué no se apaga (sesión, actualizaciones de Windows) y decidir con el
coordinador.

## Pruebas
`tests/test_coldmove_kit.py` ejecuta los scripts de verdad contra `qm`, `zfs`, `ssh` y `hostname`
falsos. Cubre también el SSH que cae a mitad, la publicación truncada y la reanudación de envíos.
