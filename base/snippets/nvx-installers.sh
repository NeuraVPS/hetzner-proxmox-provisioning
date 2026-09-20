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
set -Eeuo pipefail

BASE_URL="${BASE_URL:-https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers}"
DESTINO="${DESTINO:-/var/www/pkg}"
FICHEROS="install_mt_from_storagebox.ps1 install_sqx_from_storagebox.ps1 install_qa_from_storagebox.ps1"
# Keep this revision in sync with the two Windows installers and the canonical
# launcher hashes below. The versioned directory prevents a half-refreshed
# launcher from being served during an app install.
HOOK_REVISION="1ecfca1bdb5b4e981f9ed9c7f66f471a911611d"
HOOK_DIR="$DESTINO/hooks/$HOOK_REVISION"
declare -A HOOK_HASHES=(
  [sqx_hook_launcher.vbs]=adb3bccebdc5b8c850d918e359a8701cf4e984ba55de238e05f5b2184168ed4a
  [mt_hook_launcher.vbs]=9b372a41e0a6bb2f910d24c76480d356f9c26485ed7167a0cd006f73e949456b
)

mkdir -p -- "$DESTINO"
case "$DESTINO" in
  /*) ;;
  *) echo "nvx-installers: DESTINO must be an absolute path" >&2; exit 1 ;;
esac
STAGE_DIR=''
HOOK_STAGE_DIR=''
cleanup_staging() {
  local d
  for d in "$STAGE_DIR" "$HOOK_STAGE_DIR"; do
    [[ -n "$d" ]] || continue
    [[ "$d" == "$DESTINO"/.nvx-* ]] || {
      echo "nvx-installers: refusing to clean unexpected staging path: $d" >&2
      continue
    }
    [[ -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup_staging EXIT HUP INT TERM
# Restos de una ejecucion cortada a medias: nginx no debe servirlos nunca.
rm -f -- "$DESTINO"/.nvx-inst.* 2>/dev/null || true
ok=0; fallo=0; igual=0

# Hooks are a prerequisite for publishing refreshed installers. Reuse an
# already-valid cache entry when GitHub is unavailable, and never replace it
# with an unverified response.
HOOK_STAGE_DIR="$(mktemp -d -p "$DESTINO" .nvx-hook-stage.XXXXXX)"
for f in "${!HOOK_HASHES[@]}"; do
  if [[ -f "$HOOK_DIR/$f" ]] && [[ "$(sha256sum "$HOOK_DIR/$f" | awk '{print $1}')" == "${HOOK_HASHES[$f]}" ]]; then
    igual=$((igual + 1)); continue
  fi
  tmp="$HOOK_STAGE_DIR/$f"
  url="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/$HOOK_REVISION/windows_vm/hooks/$f"
  if ! curl -fsSL --max-time 60 "$url" -o "$tmp"; then
    echo "nvx-installers: $f -> descarga fallida y no hay copia valida" >&2
    exit 1
  fi
  got="$(sha256sum "$tmp" | awk '{print $1}')"
  if [[ "$got" != "${HOOK_HASHES[$f]}" ]]; then
    echo "nvx-installers: $f -> hash inesperado ($got), NO lo instalo" >&2
    exit 1
  fi
done

# Seed the complete hook set before publishing dependent installers.
mkdir -p -- "$HOOK_DIR"
for f in "${!HOOK_HASHES[@]}"; do
  tmp="$HOOK_STAGE_DIR/$f"
  if [[ -f "$tmp" ]]; then
    chmod 644 -- "$tmp"
    mv -f -- "$tmp" "$HOOK_DIR/$f"
    ok=$((ok + 1))
  fi
done

STAGE_DIR="$(mktemp -d -p "$DESTINO" .nvx-stage.XXXXXX)"
stage_failed=0
for f in $FICHEROS; do
  # El temporal va EN EL DESTINO, no en /tmp: ahi son sistemas de ficheros
  # distintos y `mv` seria copiar+borrar, no un renombrado atomico. Con el
  # temporal al lado, nginx no puede llegar a ver un fichero a medias.
  tmp="$STAGE_DIR/$f"
  if ! curl -fsSL --max-time 60 "$BASE_URL/$f" -o "$tmp"; then
    echo "nvx-installers: $f -> descarga fallida, conservo la copia anterior" >&2
    rm -f "$tmp"; fallo=$((fallo + 1)); stage_failed=1; continue
  fi
  # Un .ps1 nuestro nunca baja de 5 KB. Un cuerpo corto es un 429, un 404 o una
  # pagina de error de GitHub, y sobreescribir con eso seria peor que no tocar.
  bytes=$(stat -c %s "$tmp" 2>/dev/null || echo 0)
  if [ "$bytes" -lt 5000 ]; then
    echo "nvx-installers: $f -> solo $bytes bytes, sospechoso; NO lo instalo" >&2
    rm -f "$tmp"; fallo=$((fallo + 1)); stage_failed=1; continue
  fi
done

if [[ "$stage_failed" == 0 ]]; then
  for f in $FICHEROS; do
    tmp="$STAGE_DIR/$f"
    if [ -f "$DESTINO/$f" ] && cmp -s "$tmp" "$DESTINO/$f"; then
      igual=$((igual + 1)); continue
    fi
    chmod 644 -- "$tmp"; mv -f -- "$tmp" "$DESTINO/$f"
    echo "nvx-installers: $f actualizado"; ok=$((ok + 1))
  done
else
  echo "nvx-installers: staging de instaladores incompleto; conservo todas las copias actuales" >&2
fi
echo "nvx-installers: $ok actualizado(s), $igual sin cambios, $fallo con problema"
# Solo se falla si NO hay copia utilizable de alguno: que GitHub no conteste
# teniendo copia buena no es una averia, es el modo degradado previsto.
for f in $FICHEROS; do
  [ -s "$DESTINO/$f" ] || { echo "nvx-installers: FALTA $DESTINO/$f" >&2; exit 1; }
done
for f in "${!HOOK_HASHES[@]}"; do
  [ -s "$HOOK_DIR/$f" ] || { echo "nvx-installers: FALTA $HOOK_DIR/$f" >&2; exit 1; }
  [ "$(sha256sum "$HOOK_DIR/$f" | awk '{print $1}')" = "${HOOK_HASHES[$f]}" ] || exit 1
done
exit 0
