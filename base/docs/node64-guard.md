# Guarda del /64 propio del nodo (`inet nvxnode64`)

Desde el 25/11/2026. Pieza: `base/snippets/nvx-node64.py` + `nvx-node64.service` +
`nvx-node64.timer`; despliegue y marcha atrás: `scripts/despliega-node64.py`; nodos
nuevos: copia en `install.sh`.

## Qué hueco cierra

Cada nodo (salvo Kademar) tiene su /64 público on-link en `vmbr0` (`::1/64`; el host
usa `::2/128` en la uplink). Las VMs ya no usan ese /64: su IPv6 pública es el /64 de
la BASE + vmid, que la base traduce a la IDENT `2a01:4f9:c01f:e::/64` y filtra
(`ipv6Enabled`, rdp/ssh/smb, rdpguard, IPs de confianza, modo cerrado; ver
`direct-ipv6-base-policy.md`). Un invitado que conserve `<node64>::<vmid hex>` (modelo
anterior a IDENT) o que se ponga cualquier dirección del /64 del nodo (es
Administrador) quedaba accesible **directamente** desde Internet por la uplink del
nodo, sin pasar por nada de eso. Caso real: VM1409 (nodo 0000061, `ipv6Enabled=false`),
3389/445/22 abiertos desde fuera hasta el 25/11.

## Qué hace

Tabla propia en el hook **forward** (nunca input: la entrada al host y el SSH
bases→nodo no pasan por aquí). Lo reenviado hacia `vmbr0` con destino en el /64 del
nodo pasa solo si:

| regla (comentario) | qué deja pasar |
|---|---|
| `nv-excl` | destino = dirección derivada de un vmid de `/etc/neuravps/node64-guard-allow-vmids` (las `ipv6Enabled=true` del 25/11: 520, 1306, 1712, 1713) |
| `nv-infra` | origen en `[IPSET base]` de `cluster.fw` (más una copia fija en el script) o `::1`/`::2` de cada /64 de `hosts-ipv6` (los hosts, no el /64 entero: incluye invitados) |
| `nv-est` | `ct state established,related`: respuestas a lo que inicia la VM |
| `nv-final` | lo demás: `drop` (MODE=drop) o solo contador + log limitado `nvx-node64 in:` (MODE=count) |

Además `nv-salida` cuenta (sin cortar) lo que sale de `vmbr0` hacia fuera con origen en
el /64 del nodo (incluye las respuestas). **No se corta la salida**: un invitado con
dirección node64 sigue pudiendo salir por la uplink del nodo (fuera de pools y de
«Internet apagado»); es otro hueco, sin tratar aquí.

`MODE` va por drop-in (`/etc/systemd/system/nvx-node64.service.d/mode.conf`). El timer
reaplica cada 5 min, pero solo reconstruye la tabla (en una transacción atómica) si el
conjunto calculado cambia o si la tabla falta (p. ej. tras un `flush ruleset`), así que
los contadores se conservan. Sin /64 en `vmbr0` (Kademar) no pone nada.

## Operación

```bash
scripts/despliega-node64.py --solo-mirar                       # estado de la flota
scripts/despliega-node64.py --nodos <a,b> --modo count          # canario: solo cuenta
scripts/despliega-node64.py --nodos <a,b> --modo drop           # armar
scripts/despliega-node64.py --modo drop --excluir 520,1306,...  # otra lista de excluidas
# en el nodo:
python3 /usr/local/sbin/nvx-node64.py --mirar
journalctl -k | grep nvx-node64          # en modo count, quién llega
```

`FINAL` = intentos de fuera descartados (o que se descartarían); `SALIDA` = paquetes
salientes con origen node64; `EXCL` = tráfico a una VM excluida.

## Marcha atrás

- Toda la flota: `scripts/despliega-node64.py --quitar` (para y deshabilita timer y
  servicio y borra la tabla; los ficheros quedan inertes).
- Un nodo: `systemctl disable --now nvx-node64.timer nvx-node64.service; python3 /usr/local/sbin/nvx-node64.py --quitar`
- Volver a solo contar sin quitar: `--modo count`.

## Pruebas

- `unshare -rn python3 base/snippets/test-node64-netns.py` (sin root, sin producción):
  conexiones TCP reales nodo/Internet/invitado en count y drop, reconciliación, flush,
  marcha atrás y nodo sin /64.
- `python3 -m unittest tests/test_nvx_node64.py`: análisis de `cluster.fw`, ruleset y que
  `install.sh` lleva la copia vigente.
