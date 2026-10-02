# Node-liveness watchdog (dónde vive y qué necesita de los nodos)

**El código NO está en este repo**: es una Cloud Function del repo NeuraVPS
(`functions/node_liveness.py`, registrada en `main.py` como
`node_liveness_check`, cada 5 min). Se documenta aquí porque es la pareja del
[failover watchdog de bases](failover-watchdog.md) y porque la pregunta
natural es "¿qué hay que instalar en un nodo nuevo?" — **respuesta: nada**.

## Por qué un nodo nuevo queda cubierto automáticamente

1. La función descubre la flota leyendo **`proxmox_nodes`** en cada barrido:
   en cuanto el doc del nodo existe con su campo `ip` (o `server_ipv4`) — que
   se crea al darlo de alta en admin/nodos, antes incluso de `first_boot` —
   el nodo entra en la sonda. Sin agentes, sin systemd units, sin estado en
   el nodo.
2. La sonda se **lanza DESDE las dos bases** (1 SSH por base y por barrido;
   script efímero, sin huella persistente) y, desde el 02/10/2026, **no se
   conforma con el banner**: entra en el nodo con `/root/.ssh/neuravps_id` de
   la base y ejecuta `pveversion`. Vivo = la clave entra y el comando corre.
   Estados: `no_banner`, `auth_failed` (banner pero clave rechazada o login
   colgado), `cmd_failed` (clave OK, comando falla) y `rescue` (clave
   rechazada y huella de host distinta de la del último login bueno, que la
   función guarda en `config/node_liveness.hostKeys`). Medido: los 206 nodos
   en ~10 s por base.
   Los nodos filtran `:22` por origen y las bases están en la allowlist —
   desde GCP directo NO se puede (211/211 falso-down en el primer despliegue).
   Un nodo cuenta como caído solo si TODAS las bases que respondieron lo ven
   caído; si ninguna base responde, el barrido se abstiene (caída de base =
   territorio del failover watchdog).
3. Requisito node-side: sshd en `:22` accesible desde las bases y la clave
   de las bases en `authorized_keys` — las dos cosas, ciertas por construcción
   en toda instalación Proxmox de `install.sh`. **Un nodo en el rescate de
   Hetzner cuenta como CAÍDO** (antes contaba como vivo porque también da
   banner: 0000051, 02/10/2026, 49 VMs ~5,5 h de más). Lo mismo un nodo con
   sshd vivo que rechaza la clave (0000089, 0000131: disco encravado o
   `/etc/pve` sin montar, porque `authorized_keys` es un enlace a
   `/etc/pve/priv`).

## Escalera (config en Firestore `config/node_liveness`, kill-switch `enabled:false`)

dark ≥10 min → email soporte@ · ≥20 min → Robot `hw` reset (autoReset) ·
≥28 min corte de corriente + encendido · ≥40 min y ≥15 tras el hw → Robot
`man` = técnico físico (autoManual) · recordatorio cada 6 h · email de
recuperación (solo con clave + comando OK). **En rescate:** aviso «nodo en
RESCATE de Hetzner», nada de hw/corte/técnico; si la escalera ya había actuado
y `GET /boot/<n>/rescue` da `active:false`, un `hw` que arranca el disco
(`autoRescueReboot`, a los 15 min en rescate, una vez por caída). Interruptor
de la sonda nueva sin desplegar: `authProbe:false` (vuelve a solo banner). Presupuesto Robot 6 llamadas/h
(transaccional; el límite real del API es ~100/h compartido). Eventos en
`node_liveness_events`.

## Regla operacional

**Congela (`frozen: true`) el nodo durante instalaciones/reinstalaciones,
trabajo en el rescate y mantenimientos largos** — es la misma convención que auto_provision/defrag y
el watchdog lo salta por completo. Un reboot normal (POST 3-11 min en AX162)
queda por debajo de todos los umbrales y no dispara nada.

Origen: incidente 2026-07-16/17 del nodo 0000102 (freeze de plataforma, 12 VMs,
~12 h sin detección; TCO y resets remotos ignorados, solo funcionó el ciclo
físico del técnico). Doc canónico en NeuraVPS:
`docs/INCIDENT_2026-07-17_node0000102_platform_freeze.md`.
