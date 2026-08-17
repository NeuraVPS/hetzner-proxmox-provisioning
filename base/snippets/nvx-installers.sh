#!/usr/bin/env bash
# Refresca en la BASE los instaladores que ejecutan los invitados.
#
# QUE RESUELVE
# Hasta ahora cada VM se bajaba su instalador de raw.githubusercontent.com al
# aprovisionarse y al reinstalarse. Con la conversion de salida, las 1876 VMs
# comparten las dos IPv4 de las bases, asi que el limite por IP de GitHub —que
# antes se repartia entre 227 IPs de nodo— cae entero sobre dos. Ya nos mordio:
# la instalacion de MT de la vm520 fallo con 429 el 2026-07-08.
#
# Ahora el invitado lo pide a la base y la base lo refresca de GitHub UNA VEZ
# POR HORA. Se pasa de "una peticion por VM" a "dos por hora en toda la flota",
# y de paso el invitado ya no necesita salir a Internet para instalarse: la
# descarga es interna (invitado -> nodo -> tunel -> base, entrega local a
# nginx) y va por IPv6, que GitHub ni siquiera tiene.
#
# ⚠️ SOLO SE REEMPLAZA SI LA DESCARGA VA BIEN. Un fichero a medias o un 429
# dejarian a los invitados instalando basura, en silencio y solo en las VMs
# nuevas — que es de las cosas mas caras de descubrir. Si GitHub no contesta se
# conserva la copia anterior, que es exactamente lo que se quiere.
set -u

BASE_URL="${BASE_URL:-https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers}"
DESTINO="${DESTINO:-/var/www/pkg}"
FICHEROS="install_mt_from_storagebox.ps1 install_sqx_from_storagebox.ps1 install_qa_from_storagebox.ps1"

mkdir -p "$DESTINO"
# Restos de una ejecucion cortada a medias: nginx no debe servirlos nunca.
rm -f "$DESTINO"/.nvx-inst.* 2>/dev/null
ok=0; fallo=0; igual=0

for f in $FICHEROS; do
  # El temporal va EN EL DESTINO, no en /tmp: ahi son sistemas de ficheros
  # distintos y `mv` seria copiar+borrar, no un renombrado atomico. Con el
  # temporal al lado, nginx no puede llegar a ver un fichero a medias.
  tmp="$(mktemp -p "$DESTINO" .nvx-inst.XXXXXX)"
  if ! curl -fsSL --max-time 60 "$BASE_URL/$f" -o "$tmp"; then
    echo "nvx-installers: $f -> descarga fallida, conservo la copia anterior" >&2
    rm -f "$tmp"; fallo=$((fallo + 1)); continue
  fi
  # Un .ps1 nuestro nunca baja de 5 KB. Un cuerpo corto es un 429, un 404 o una
  # pagina de error de GitHub, y sobreescribir con eso seria peor que no tocar.
  bytes=$(stat -c %s "$tmp" 2>/dev/null || echo 0)
  if [ "$bytes" -lt 5000 ]; then
    echo "nvx-installers: $f -> solo $bytes bytes, sospechoso; NO lo instalo" >&2
    rm -f "$tmp"; fallo=$((fallo + 1)); continue
  fi
  if [ -f "$DESTINO/$f" ] && cmp -s "$tmp" "$DESTINO/$f"; then
    rm -f "$tmp"; igual=$((igual + 1)); continue
  fi
  chmod 644 "$tmp"
  mv -f "$tmp" "$DESTINO/$f"
  echo "nvx-installers: $f actualizado ($bytes bytes)"
  ok=$((ok + 1))
done

echo "nvx-installers: $ok actualizado(s), $igual sin cambios, $fallo con problema"
# Solo se falla si NO hay copia utilizable de alguno: que GitHub no conteste
# teniendo copia buena no es una averia, es el modo degradado previsto.
for f in $FICHEROS; do
  [ -s "$DESTINO/$f" ] || { echo "nvx-installers: FALTA $DESTINO/$f" >&2; exit 1; }
done
exit 0
