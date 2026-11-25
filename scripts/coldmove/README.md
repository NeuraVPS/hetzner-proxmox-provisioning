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

## Dos modos: quién abre las conexiones
Todas las conexiones nodo↔nodo las abre **el nodo que corre el kit**. Al otro solo le llegan órdenes
sueltas (`zfs`, `qm`, `cat`, `sha256sum`, `rm`, `mv`…), y ninguna de ellas abre SSH.

| Caso | Modo | Dónde corre el kit | Variables del otro nodo |
|---|---|---|---|
| Entre nodos de la flota (Hetzner) | `push` (por defecto) | ORIGEN | `DST`, `DST_NODE` |
| Hacia un nodo de otro proveedor que autoriza la clave de flota (Kademar como DESTINO) | `push` | ORIGEN (Hetzner) | `DST`, `DST_NODE` |
| Desde un nodo de otro proveedor **sin** clave de flota (Kademar como ORIGEN) | `pull` (`RTMIG_MODE=pull`) | DESTINO (Hetzner) | `SRC`, `SRC_NODE` |

**Cómo se decide:** si `proxmox_nodes/<origen>.provider` existe y no es `hetzner` (Kademar:
`provider: kademar`), el modo es `pull` y el kit se lanza en el destino. Los nodos de Hetzner no llevan
el campo.

- **Opción «B+» de Iván (25/11/2026):** `0000266-KDM-9755` no tiene ni tendrá la clave de flota. Los
  nodos de Hetzner pueden entrar en él, porque autoriza la pública de flota y su cortafuegos deja
  pasar `hosts-ipv6`, pero él no puede entrar en ningún nodo nuestro.
- **Si se intenta `push` desde un nodo sin clave de flota** (`/root/.ssh/neuravps_id`), el kit se
  niega antes de abrir ninguna conexión y dice cómo lanzarlo en modo `pull`.
- **`migrate_vm.sh` rechaza `remote_migrate`** si el origen o el destino es de otro proveedor, y
  remite aquí.
- **En los dos modos**, el estado, la copia aparcada y el cerrojo viven en `$WORK` del nodo que
  corre el kit. En `pull`, eso es el destino: Kademar no guarda nada del traslado.

### En `pull`, el origen NO es fiable
Kademar puede estar comprometido. Todo lo que devuelve se valida en el nodo de Hetzner como entrada
hostil, y nada suyo se ejecuta ni se evalúa.
- **Contadores y guid:** solo se aceptan enteros decimales estrictos y acotados. Un valor raro es
  estado INDETERMINADO, y `pending` nunca lo mete en la aritmética del shell. Los nombres de
  snapshot que no cumplen el patrón `rtmig-…` se ignoran.
- **Sondas:** el estado de la VM, los sha256 y el UUID se validan por formato. La firma de CPU solo
  se acepta como `vendor|family|model`.
- **Config: perfil local con lista blanca** (`sanitize_conf`). Lo que se publica en el destino es una
  config nueva construida aquí:
  - Solo con claves permitidas y valores de formato estricto:
    - `name`, `memory`, `balloon`, `cores`, `sockets`, `vcpus`, `cpu` (modelos conocidos), `ostype`,
      `machine`, `bios`, `boot`, `scsihw`, `agent`, `smbios1` (con el UUID del traslado), `vmgenid`;
    - `numa`, `tablet`, `onboot`, `localtime`, `kvm`, `vga`;
    - `net0` (solo `virtio=<MAC>,bridge=vmbr0` y opciones numéricas);
    - discos `efidisk0`, `tpmstate0`, `scsiN`/`virtioN`/`sataN`/`unusedN`, solo `local-zfs:vm-<VMID>-disk-N`;
    - CD-ROM `none`.
  - El **hookscript es siempre el nuestro**: `shared:snippets/sync-dnat.py`.
  - Hacen abortar **antes de apagar**: `args`, `hostpci`, `usb`, `serial`, `parallel`, `audio`,
    `rng`, `virtiofs`, `lock`, `startup`, un `[PENDING]`, una clave repetida o un valor raro.
  - Se descartan las secciones de snapshot, `parent`, `meta`, `digest` y la descripción, salvo la
    marca de plantilla `#neuravps-stream-template-key`.
  - La config original se queda aparcada para un rollback a Kademar.
- **`.fw`: se reescribe en limpio desde la lista blanca** (`sanitize_fw`). Solo se admiten
  `[OPTIONS] enable: 0|1` y `[RULES]` con los grupos de la flota (`vm-default`, `vm-no-internet` y
  `vm-public-ipv6`, activos o con `|`). Una regla propia, un `IPSET`, un `ALIASES` o una
  `policy_in` hacen abortar.
- **Historial del globo:** el de un origen no fiable no se importa.
- **Agente SSH:** nunca se reenvía. El kit usa `-a -o ForwardAgent=no -o ClearAllForwardings=yes`
  y `migrate_vm.sh` igual. Los runners de `run_remotes` solo reenvían el agente a las IPs de la
  flota (`2a01:4f8::/32` y `2a01:4f9::/32`).

### Corte ligado al par origen/destino
- En `pull`, `DST_NODE` es obligatorio y tiene que ser este nodo.
- **`shutdown` no apaga sin una precopia común verificada con ese destino:** algún `@rtmig-*` con el
  mismo guid en todos los discos de los dos lados. Así, otro nodo sin la precopia no puede apagar la
  VM, y el corte nunca dura una copia entera.

## Piezas
| Fichero | Dónde corre | Qué hace |
|---|---|---|
| `rtmig_deploy.sh` | equipo del operador | copia el kit a `/root/rtmig/` de los nodos, pasando por una BASE (`JUMP`). En `pull` basta con `ONLY=dst` |
| `rtmig_send.sh` | nodo que corre el kit | `base` (snapshot y envío completo), `incr`, `verify`, `pending`, `last`, `resume` |
| `rtmig_cutover.sh` | nodo que corre el kit | `shutdown`, `park`, `push`, `start`, `close`, `rollback`, `status` |
| `rtmig_common.sh` | (lo cargan los dos) | modos, parámetros y su validación, ejecución por rol, identidad, sondas, estado, inventario |

**Parámetros comunes, por entorno:**
- `VMID`; `RTMIG_MODE` (`push` por defecto, o `pull`).
- En `push`, `DST` y `DST_NODE`; en `pull`, `SRC` y `SRC_NODE`.
  - La IP es la de `proxmox_nodes/<nodo>.ip`, en forma canónica y sin zona (`%eth0`). En una BASE:
    `jq -r '."<nodo>"' /var/lib/base-nat/pve_nodes.json`.
  - El id del nodo se comprueba con `hostname` en él antes de hacer nada.
  - En `pull`, el nodo destino es el propio; si se pone `DST_NODE`, tiene que coincidir con
    `hostname`.
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

El código no lleva IPs, VMIDs ni credenciales: usa la clave de la flota `~/.ssh/neuravps_id`, que
tiene que estar en el nodo que corre el kit. El otro nodo solo necesita autorizar su pública.

**Red:** va nodo↔nodo por SSH, abierto por el nodo que corre el kit. El otro nodo tiene que aceptarlo
en el puerto 22, igual que cualquier nodo de la flota: los dos tienen que estar en `proxmox_nodes` y
haber pasado `sync-base-nat.py sync nodes` y `sync-firewall` (IPSET `hosts-ipv6`). En `pull`, el
origen no necesita llegar al destino.

**Ritmo:** `RATE` por defecto es `30m`, unos 250 Mbit/s por flujo; está pensado para un enlace
no-Hetzner de ~300 Mbit/s, como Kademar. Dentro de Hetzner se puede subir a `60m`–`110m`. En la
incremental final, con la VM ya apagada, se usa `RATE=0`. `ZSTD=1` recomprime en el cable si el cuello
es la red.

## Flujo
- **`push`**, en el nodo origen: `cd /root/rtmig && export VMID=… DST=… DST_NODE=…`.
- **`pull`**, en el nodo destino: `cd /root/rtmig && export RTMIG_MODE=pull VMID=… SRC=… SRC_NODE=…`.

Las órdenes son las mismas en los dos modos. En `pull`, «el origen» de cada paso se ejecuta por SSH
desde el destino: el apagado ordenado, el aparcado de la config, el `zfs send` y la restauración del
rollback.

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
      inventario de discos, CPU y licencias (ver abajo; el `ACK` va ya en esta orden) y destino
      libre. Luego el apagado ordenado. Si Windows no se apaga, **no se fuerza**: decide una persona.
   2. `./rtmig_cutover.sh park`: exporta el historial del reconciliador de globo (si el origen lo
      tiene y el modo es `push`) y aparca la config en `$WORK`. Así nadie puede arrancar la VM en
      origen. Los zvols se quedan.
      - La config y el `.fw` llegan primero a nombres provisionales (`*.staging`). Solo cuando han
        llegado los dos se pasan a los definitivos y se abre el estado.
      - Si algo cae antes, no queda nada «aparcado» y `park` se repite sin más.
      - Si cae después, al quitar la config del origen, `park` se retoma, y `rollback` también
        funciona desde ahí.
   3. `RATE=0 ./rtmig_send.sh incr auto rtmig-final-<fecha>`: incremental final, con `VERIFY OK`.
   4. `./rtmig_cutover.sh push rtmig-final-<fecha> [balloon_mb]`:
      - exige que el origen siga sin config (respuesta positiva);
      - comprueba guid y «0 B escritos después» en cada disco del inventario;
      - escribe primero el `.fw` y **publica la config al final**, ya con la CPU y el balloon
        aplicados. Cada fichero se escribe a `<ruta>.rtmig-tmp`, se comprueba allí su sha256 y solo
        entonces se renombra con `mv -n`, que pmxcfs hace atómico. Después se relee y se comparan
        el sha256 y el UUID;
      - si el origen no tiene `.fw`, el destino tampoco puede tenerlo;
      - con un cambio `[PENDING]` de cpu o balloon que haya que fijar, se niega. La CPU que decide
        la guarda es la que arrancará, es decir, la de `[PENDING]` si la hay;
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

## Vuelta al mismo nodo
Ejemplo: una VM que fue de un nodo de Hetzner **A** a Kademar (en `push`) y vuelve a **A** (en `pull`,
lanzado en A).
- **A conserva sus zvols `vm-<VMID>-disk-N` y los `@rtmig-*` del viaje de ida**, porque el kit nunca
  borra el origen. Kademar tiene los mismos snapshots, con el mismo guid.
- **No se usa `base`:** se niega sin pisar nada («el destino ya tiene … sin @SNAP: no piso nada»).
- **La vuelta empieza con** `RTMIG_MODE=pull SRC=<IP Kademar> SRC_NODE=0000266-KDM-9755
  DST_NODE=<A> ./rtmig_send.sh incr auto rtmig-<nueva>`.
  - `auto` encuentra el último `@rtmig-*` común (el final de la ida, con el mismo guid en los tres
    discos).
  - El incremental se recibe con `-F`, que devuelve el zvol de A a ese snapshot. La VM estuvo
    aparcada en A, así que no hay escrituras que perder.
- **Después, el corte de siempre:** `shutdown`, `park`, `incr` final, `push` y `start`.
- El estado anterior de A está en `done` o `rolledback`, así que no estorba.
- No borrar los zvols de A antes de que pase el keep acordado: son la base de la vuelta.

## CPU y licencias (`shutdown` y `push`)
La regla es la misma que en `migrate_vm.sh` y en el defrag. La vía fría directa de `migrate_vm.sh`
(VM parada, sin kit) pide la misma constancia: `ACK_HWID_CHANGE=<VMID>` y `CPU_NOTICE`.
- **Modelo genérico (`x86-64-v*`):** el Hardware ID de SQX no cambia; cualquier destino del mismo
  nivel vale. Un `x86-64-v4` hacia un destino sin AVX-512 no arrancaría, así que se rechaza salvo con
  `CHANGE_CPU=1`.
- **Cambio de modelo pendiente.** `shutdown` lee con qué CPU corre de verdad la VM (la línea `-cpu`
  de QEMU) y la compara con la del próximo arranque. Si son de clase distinta, por ejemplo `host` en
  marcha con `[PENDING] cpu: x86-64-v4`, el Hardware ID cambia aunque el silicio no cambie, y se
  pide la misma constancia. La CPU en marcha queda en el estado (`RUN_CPU`) para `push`.
- **`cpu: host`/`max`, otro modelo con nombre o sin línea `cpu`, cuando el silicio cambia**
  (vendor|family|model): el Hardware ID cambia. `shutdown` y `push` se niegan si no llevan una de
  estas:
  - `CHANGE_CPU=1`: pone la CPU de las altas (`x86-64-v4`, o v3 sin AVX-512).
  - `ACK_HWID_CHANGE=<VMID>`: deja la CPU tal cual.
  - En los dos casos hace falta `CPU_NOTICE='<cuándo y por dónde se avisó al cliente>'`.
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
repite cuando el otro nodo conteste.

En `pull`, el rollback se lanza igual, desde el destino: apaga y retira lo suyo en local y
restaura el `.fw` y la config en el origen por SSH. La copia aparcada está en el destino.

## Apagado
`shutdown` nunca usa `forceStop`. Si vence el tiempo, la VM sigue encendida en el origen y no se ha
tocado nada. Hay que mirar por qué no se apaga (sesión, actualizaciones de Windows) y decidir con el
coordinador.

## Pruebas
`tests/test_coldmove_kit.py` ejecuta los scripts de verdad contra `qm`, `zfs`, `ssh` y `hostname`
falsos.
- Cada escenario se ejecuta en los dos modos.
- El `ssh` falso falla, y lo deja anotado, si una orden que llegó por SSH intenta abrir otro SSH. Así
  se comprueba que el nodo remoto (Kademar, en `pull`) nunca se conecta a nadie.
- Cubre también el SSH que cae a mitad, la publicación truncada y la reanudación de envíos.
