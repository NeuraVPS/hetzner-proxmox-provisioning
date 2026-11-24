"""Censo de reglas 64:ff9b:1::/96 en `ip6 rdpguard pre` (vivo: salida de
`nft -a list chain`; disco: /etc/nftables.conf). Imprime «own any handle»:
own = líneas IGUALES (completas, sin sangría ni «# handle N») a la regla propia;
any = líneas no comentadas que mencionan 64:ff9b:1::/96 dentro de ip6 rdpguard;
handle = el de la propia (vivo) o -. Lo usan deploy/rollback_nat64_limiter_exemption.sh."""
import re
import sys

mode, path, own = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
if mode == "disk":
    i = text.index("table ip6 rdpguard {")
    rest = text[i:]
    cands = [k for k in (rest.find("\ntable ", 1), rest.find("\ninclude ")) if k > 0]
    text = rest[:min(cands)] if cands else rest
n_own = n_any = 0
handle = "-"
for raw in text.splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    m = re.search(r"\s+# handle (\d+)$", line)
    if m:
        line, h = line[:m.start()], m.group(1)
    else:
        h = None
    if "64:ff9b:1::/96" not in line:
        continue
    n_any += 1
    if line == own:
        n_own += 1
        handle = h or "-"
print(n_own, n_any, handle)
