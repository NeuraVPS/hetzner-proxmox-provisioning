# cluster.fw común: escritor único y validado

`/home/firewall/cluster.fw` en el Storage Box es la plantilla del cortafuegos de
PVE de toda la flota. Las bases solo reescriben `[IPSET hosts-ipv6]` (desde
`/var/lib/base-nat/pve_nodes.json`) y lo reparten a cada nodo.

## Cómo funciona (desde el 25/11/2026)

- `sync-base-nat.py sync nodes sync-firewall` (lo llama `on_proxmox_nodes_written`,
  siempre en b0 y en b1 solo si b0 no responde) **solo encola**: escribe
  `/var/lib/base-nat/fw-sync.pending` y arranca un worker con
  `systemd-run --unit=nv-cluster-fw-sync-*` (vuelve en <1 s).
- Los workers se serializan con `flock` en `/var/lib/base-nat/.fw-sync.lock`; el que
  entra consume la marca y hace una pasada; el que entra sin marca no hace nada
  (coalescencia de ráfagas).
- Pasada: lock `mkdir /home/firewall/.cluster.fw.lock` en el Storage Box (roto si
  tiene >15 min) → descarga → reescribe hosts-ipv6 → **valida** (secciones
  conocidas y sin repetir, gramática por sección, `[OPTIONS] enable: 1`,
  `IN SSH(ACCEPT) -source +dc/base`, `[IPSET base]` cubre las IPv6 de `BASE_HOSTS`,
  nº de hosts, sin encoger >20 %) → sube a `cluster.fw.tmp-<base>-<pid>` →
  comprueba sha256 → `mv` sobre `cluster.fw`.
- Reparto: la base **empuja el contenido por SSH** a cada nodo (8 en paralelo). El
  nodo comprueba el sha256, lo carga con `PVE::Firewall::load_clusterfw_conf` (cero
  avisos; reglas, ipsets base/hosts-ipv6 y la regla SSH desde +dc/base) y solo
  entonces lo copia a `/etc/pve/firewall/cluster.fw` (tmp + rename), guarda el
  anterior en `/root/cluster.fw.prev` y reinicia `pve-firewall` **solo si cambió**.
  Los nodos ya no necesitan la clave del Storage Box para esto (Kademar incluido).
- Rechazos (validación en base o en algún nodo) y fallos del Storage Box →
  correo a soporte@ (`/etc/neuravps/hw-error-scan.env`). Nodos inalcanzables solo
  quedan en `/var/log/sync-base-nat.log`.

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
