"""Censo de reglas 64:ff9b:1::/96 en `ip6 rdpguard` (vivo: salida de
`nft -a list chain ip6 rdpguard pre`; disco: /etc/nftables.conf). Imprime
«own any handle pos»:
  own    líneas IGUALES (completas, sin sangría ni «# handle N») a la regla
         propia DENTRO de la cadena `pre`;
  any    líneas no comentadas que mencionan 64:ff9b:1::/96 en TODA la tabla
         (otras cadenas incluidas: cualquier otra es conflicto);
  handle el de la propia (vivo) o -;
  pos    ok si la propia está después del drop de bf_auto y antes del
         registro en bf_seen; bad si no; - si no está.
Lo usan deploy/rollback_nat64_limiter_exemption.sh."""
import re
import sys

mode, path, own = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
if mode == "disk":
    i = text.index("table ip6 rdpguard {")
    rest = text[i:]
    cands = [k for k in (rest.find("\ntable ", 1), rest.find("\ninclude ")) if k > 0]
    table = rest[:min(cands)] if cands else rest
    m = re.search(r"\n(\s*)chain pre \{\n(.*?)\n\1\}", table, re.S)
    pre = m.group(2) if m else ""
else:
    table = pre = text


def lines(block):
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"\s+# handle (\d+)$", line)
        yield (line[:m.start()], m.group(1)) if m else (line, None)


n_any = sum(1 for l, _ in lines(table) if "64:ff9b:1::/96" in l)
n_own, handle, pos = 0, "-", "-"
pre_lines = list(lines(pre))
for k, (l, h) in enumerate(pre_lines):
    if l == own:
        n_own += 1
        handle = h or "-"
        auto = [j for j, (x, _) in enumerate(pre_lines) if "saddr @bf_auto" in x and "drop" in x]
        seen = [j for j, (x, _) in enumerate(pre_lines) if "add @bf_seen" in x]
        pos = "ok" if auto and seen and auto[-1] < k < seen[0] else "bad"
print(n_own, n_any, handle, pos)
