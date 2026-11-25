# cluster.fw común: escritor único y validado

`/home/firewall/cluster.fw` en el Storage Box es la plantilla del cortafuegos de
PVE de toda la flota. Las bases solo reescriben `[IPSET hosts-ipv6]` (desde
`/var/lib/base-nat/pve_nodes.json`) y lo reparten a cada nodo.

## Cómo funciona (desde el 25/11/2026)

- `sync-base-nat.py sync nodes sync-firewall` (lo llama `on_proxmox_nodes_written`
  en una sola base: la primera que aplicó el `add`/`del`, en orden b0, b1) **solo
  encola**: escribe `/var/lib/base-nat/fw-sync.pending` y arranca un worker con
  `systemd-run --unit=nv-cluster-fw-sync-*` (vuelve en <1 s).
- Los workers se serializan con `flock` en `/var/lib/base-nat/.fw-sync.lock`; el que
  entra consume la marca y hace una pasada; el que entra sin marca no hace nada
  (las ráfagas se agrupan). Si la pasada falla, programa reintentos a los 10, 20 y
  40 min (`--retry=N`) y, agotados, avisa por correo.
- **Toda la pasada** (leer `pve_nodes.json`, publicar y repartir) va dentro del lock
  `mkdir /home/firewall/.cluster.fw.lock` del Storage Box: dos bases nunca se cruzan
  y un nodo no recibe una versión anterior después de una nueva. **El lock nunca se
  rompe solo**: si tiene más de 65 min se avisa y hay que borrarlo a mano tras
  comprobar que no hay `nv-cluster-fw-sync-*` en marcha en ninguna base.
- Publicar: descarga → reescribe hosts-ipv6 → **valida** (cabeceras como las lee PVE,
  sin repetir —PVE reinicia la sección repetida—, gramática por sección,
  `[OPTIONS] enable: 1`, `IN SSH(ACCEPT) -source +dc/base` sin restricciones y antes
  de cualquier DROP/REJECT/GROUP, `[IPSET base]` cubre las IPv6 de `BASE_HOSTS` con
  entradas positivas, nº de hosts, sin encoger >20 %) → sube a
  `cluster.fw.tmp-<base>-<pid>` → sha256 → `mv` sobre `cluster.fw`.
- Reparto: la base **empuja el contenido por SSH** a cada nodo (8 en paralelo). El
  nodo comprueba el sha256 y lo carga con `PVE::Firewall::load_clusterfw_conf`
  (cero avisos, ninguna entrada de ipset con error, la misma regla SSH efectiva y las
  IPv6 de las bases dentro de `[IPSET base]`); solo entonces lo copia a
  `/etc/pve/firewall/cluster.fw` (tmp + rename), guarda el anterior en
  `/root/cluster.fw.prev`, reinicia `pve-firewall`, comprueba `enabled/running` y
  escribe `/var/lib/nv-cluster-fw.applied`. Sin esa marca, la siguiente pasada vuelve
  a reiniciar aunque los bytes coincidan. Los nodos no necesitan la clave del Storage
  Box para esto (Kademar incluido).
- `first_boot.sh` descarga el canónico aparte y lo pasa por el mismo validador (copia
  idéntica, comprobada por test) antes de instalarlo; si no pasa, se queda la base
  escrita en el propio script y el sync de la base lo entrega después.
- Correo a soporte@ (`/etc/neuravps/hw-error-scan.env`) en rechazos, fallos del
  Storage Box y reintentos agotados; el mismo aviso no se repite en 6 h. Los nodos
  inalcanzables solo quedan en `/var/log/sync-base-nat.log`.

## Operación

```bash
/usr/local/sbin/sync-base-nat.py sync nodes sync-firewall          # encolar
/usr/local/sbin/sync-base-nat.py sync nodes sync-firewall --wait   # en primer plano
grep sync-firewall /var/log/sync-base-nat.log | tail
systemctl list-units 'nv-cluster-fw-sync-*'
```

## Marcha atrás

- Canónico: subir la copia guardada a un nombre temporal y `mv` (mismo protocolo),
  con ningún worker en marcha.
- Un nodo: `cp /root/cluster.fw.prev /etc/pve/firewall/cluster.fw && pve-firewall restart`.
- Código: reinstalar la versión anterior de `sync-base-nat.py` en las dos bases.

Origen: cola basura de 228 bytes desde el 06/09/2026 por escrituras simultáneas
sin lock desde las dos bases.
