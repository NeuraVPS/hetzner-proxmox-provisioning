#!/usr/bin/env python3
"""Auditoría de persistencia en TODA la flota. Solo lee."""
import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent
B64 = base64.b64encode((DIR / "persiste.sh").read_bytes()).decode()
nodos = json.loads(subprocess.run(
    ["ssh", "-n", "b1", "cat /var/lib/base-nat/pve_nodes.json"],
    capture_output=True, text=True, timeout=90).stdout)


def uno(item):
    n, ip = item
    r = subprocess.run(
        ["ssh", "-n", "b1", f"ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=no "
                            f"root@{ip} 'echo {B64} | base64 -d | bash'"],
        capture_output=True, text=True, timeout=90)
    return n, " ".join(r.stdout.split())


with ThreadPoolExecutor(max_workers=12) as ex:
    res = list(ex.map(uno, sorted(nodos.items(), key=lambda kv: int(kv[0].split("-")[0]))))

completos = [n for n, s in res if s.endswith("=> 9/9")]
malos = [(n, s) for n, s in res if not s.endswith("=> 9/9")]
print(f"  ✅ completos (9/9): {len(completos)}/{len(res)}")
if malos:
    print(f"  ❌ INCOMPLETOS: {len(malos)}")
    for n, s in malos:
        print(f"     {n:26s} {s}")
