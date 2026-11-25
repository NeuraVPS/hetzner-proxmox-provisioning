# coldmove — traslado en frío con precopia ZFS

Sirve para mover una VM de un nodo a otro **parada**, con un corte de segundos o de un minuto en vez de
la hora que tardaría `migrate_vm.sh` copiando el disco entero con la VM apagada. Es el kit `rtmig_*`
de los traslados EX44→AX162 del 01/10 al 18/11/2026 (cortes de ~50–60 s), ahora parametrizado.

Cuándo se usa:
- Cuando no se puede migrar en caliente: cambio de fabricante o de familia de CPU (por ejemplo
  AX162/AX102 ↔ 0000266-KDM-9755), o un `cpu: host` hacia otro silicio. `migrate_vm.sh` lo rechaza.
- Cuando el disco es grande o el enlace es lento (no-Hetzner).

Siempre con el **cliente avisado de la hora del corte**.

## Piezas
| Fichero | Dónde corre | Qué hace |
|---|---|---|
| `rtmig_deploy.sh` | equipo del operador | copia el kit a `/root/rtmig/` de los dos nodos, pasando por una BASE (`JUMP`) |
| `rtmig_send.sh` | nodo ORIGEN | `base` (snapshot y envío completo), `incr`, `verify`, `pending`, `last`, `resume` |
| `rtmig_cutover.sh` | nodo ORIGEN | `shutdown`, `park`, `push`, `start`, `rollback`, `status` |
| `rtmig_common.sh` | (lo cargan los dos) | parámetros, SSH al destino, comprobación de identidad, discos |

**Parámetros comunes, por entorno:**
- `VMID`.
- `DST`: la IP del destino, tal cual está en `proxmox_nodes/<nodo>.ip`. En una BASE:
  `jq -r '."<nodo>"' /var/lib/base-nat/pve_nodes.json`.
- `DST_NODE`: el id del nodo destino. Se comprueba con `hostname` antes de hacer nada.
- Opcionales: `WORK` (por defecto `/root/rtmig`), `DISKS` (por defecto, los de la config) y
  `SSH_PORT`.

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
   los discos.
2. **Incrementales en caliente**, hasta que lo pendiente sea poco
   (`./rtmig_send.sh pending` lo estima):
   `DROP_PREV=1 ./rtmig_send.sh incr auto rtmig-<nueva>`.
3. **Corte, a la hora pactada:**
   1. `./rtmig_cutover.sh shutdown 600`: apagado ordenado. Si Windows no se apaga, **no se fuerza**:
      decide una persona.
   2. `./rtmig_cutover.sh park`: exporta el historial del reconciliador de globo (si el origen lo
      tiene) y aparca la config en `$WORK`. Así nadie puede arrancar la VM en origen. Los zvols se
      quedan.
   3. `RATE=0 ./rtmig_send.sh incr auto rtmig-final-<fecha>`: incremental final, con `VERIFY OK`.
   4. `./rtmig_cutover.sh push rtmig-final-<fecha> [balloon_mb]`:
      - comprueba guid y «0 B escritos después» en cada disco;
      - escribe la config (y el `.fw`) en el destino;
      - importa el historial del globo, si el destino tiene reconciliador.
      - Una VPS E que viene de un EX44 hacia una caja SQX (AX162, KDM) lleva `47104`.
        `migrate_vm.sh` lo vuelve a ajustar por `nodeType`.
   5. `./rtmig_cutover.sh start`.
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

El origen se queda con los zvols y los snapshots `@rtmig-*`. Se borran **a mano** pasados los días de
keep acordados (por ejemplo, con `KEEP='hasta AAAA-MM-DD'` en `rtmig_send.sh`), después de verificar.

## CPU y licencias (`push`)
La regla es la misma que en `migrate_vm.sh` y en el defrag:
- **Modelo genérico (`x86-64-v*`):** el Hardware ID de SQX no cambia; cualquier destino del mismo
  nivel vale. Un `x86-64-v4` hacia un destino sin AVX-512 no arrancaría, así que se rechaza salvo con
  `CHANGE_CPU=1`.
- **`cpu: host`/`max`, otro modelo con nombre o sin línea `cpu`, cuando el silicio cambia**
  (vendor|family|model): el Hardware ID cambia. `push` se niega si no lleva una de estas:
  - `CHANGE_CPU=1`: pone la CPU de las altas (`x86-64-v4`, o v3 sin AVX-512).
  - `ACK_HWID_CHANGE=<VMID>`: deja la CPU tal cual.
  - En los dos casos hace falta `CPU_NOTICE='<cuándo y por dónde se avisó al cliente>'`.
- Todo queda en `$WORK/cpu-change.txt`.

## Rollback
Antes de `push`, la VM sigue en el origen. Basta con no seguir: si ya se aparcó, `rollback` la
restaura y la arranca.

Después de `push` o de `start`:
1. Si la VM corre en el destino, apagarla **ordenadamente** allí (`qm shutdown`), con la decisión de
   una persona.
2. `./rtmig_cutover.sh rollback`: aparca la config del destino en su `$WORK`, restaura la del origen
   (con la CPU original) y arranca en el origen. Los discos de los dos lados se quedan.
3. Si `migrate_vm.sh` ya se había ejecutado: `migrate_vm.sh <VMID> <número del nodo origen>`, que
   vuelve a reconciliar rutas y Firestore hacia el origen.

Ojo: lo que el cliente escribió en el destino se pierde al volver, porque el origen arranca desde su
último estado.

## Apagado
`shutdown` nunca usa `forceStop`. Si vence el tiempo, la VM sigue encendida en el origen y no se ha
tocado nada. Hay que mirar por qué no se apaga (sesión, actualizaciones de Windows) y decidir con el
coordinador.

## Pruebas
`tests/test_coldmove_kit.py` ejecuta los scripts de verdad contra `qm`, `zfs`, `ssh` y `hostname`
falsos.
