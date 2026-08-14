#!/usr/bin/env bash
# Borra el muñon de una migracion remota interrumpida. Se ejecuta EN EL DESTINO.
# Guardas duras: solo actua si esta caja ES el destino esperado Y la VM esta en
# `inmigrate` con `lock: migrate`. Cualquier otra cosa aborta sin tocar nada —
# equivocarse de lado aqui destruye la VM viva del cliente.
set -u
VMID="${1:?falta vmid}"
ESPERADO="${2:?falta hostname destino esperado}"

H=$(hostname)
[ "$H" = "$ESPERADO" ] || { echo "❌ ABORTO: estoy en $H, esperaba $ESPERADO"; exit 1; }

EST=$(qm status "$VMID" --verbose 2>/dev/null | sed -n 's/^qmpstatus: //p')
LOCK=$(qm config "$VMID" 2>/dev/null | sed -n 's/^lock: //p')
echo "  $H: vm $VMID qmpstatus=$EST lock=$LOCK"
[ "$EST" = "inmigrate" ] || { echo "❌ ABORTO: qmpstatus es '$EST', no 'inmigrate' — esto NO es un muñon"; exit 1; }
[ "$LOCK" = "migrate" ] || { echo "❌ ABORTO: lock es '$LOCK', no 'migrate'"; exit 1; }

echo "  guardas OK — es un muñon de migracion. Borrando."
qm unlock "$VMID" 2>/dev/null
qm stop "$VMID" --skiplock 1 2>/dev/null || true
sleep 3
qm destroy "$VMID" --skiplock 1 --purge 1 2>&1 | sed 's/^/    /'
echo "  --- despues: $(qm list | grep -c " $VMID ") entradas, discos: $(zfs list -t volume 2>/dev/null | grep -c "vm-$VMID-")"
