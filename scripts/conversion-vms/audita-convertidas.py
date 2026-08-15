#!/usr/bin/env python3
"""Busca conversiones A MEDIAS entre las ya convertidas. Corre EN UNA BASE.

Lo aprendido de las vm 460/1754: una VM puede tener la IPv6 nueva y aun asi
estar mal por dentro. Y hay un caso que responder al RDP NO descarta — IPv6
nueva con /128 (asi que entra) pero IPv4 y puertas VIEJAS: el cliente entra a su
servidor pero su servidor NO tiene salida v4.

Compara el estado REAL del invitado contra lo esperado. Solo lee.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root")
import vmtool  # noqa: E402

st = json.load(open("/var/lib/base-nat/state.json"))
nuevas = [(int(k), v) for k, v in st.items()
          if str(v.get("ipv6") or "").lower().startswith("2a01:4f9:c01f:e:")]
if "--limite" in sys.argv:
    nuevas = nuevas[:int(sys.argv[sys.argv.index("--limite") + 1])]


def mira(par):
    v, e = par
    nip = vmtool.nodo_ip(e.get("nodeId") or "")
    if not nip:
        return v, "sin-nodo", ""
    ok, out = vmtool.por_agente(nip, v, vmtool.PS_ESTADO, t=25)
    if not ok:
        return v, "agente-mudo", out[:60]
    c = dict(kv.split("=", 1) for kv in out.split() if "=" in kv)
    mal = []
    if f"2a01:4f9:c01f:e::{v:x}/128" not in (c.get("v6") or ""):
        mal.append(f"v6={c.get('v6')}")
    if f"10.64.{v // 256}.{v % 256}/" not in (c.get("v4") or ""):
        mal.append(f"v4={c.get('v4')}")
    if "fe80::1" not in (c.get("gw6") or ""):
        mal.append(f"gw6={c.get('gw6')}")
    if "10.64.255.1" not in (c.get("gw4") or ""):
        mal.append(f"gw4={c.get('gw4')}")
    return v, ("OK" if not mal else "A-MEDIAS"), "; ".join(mal)


with ThreadPoolExecutor(max_workers=24) as ex:
    res = list(ex.map(mira, nuevas))

bien = [r for r in res if r[1] == "OK"]
medias = [r for r in res if r[1] == "A-MEDIAS"]
mudas = [r for r in res if r[1] == "agente-mudo"]
print(f"  revisadas {len(res)} convertidas: {len(bien)} correctas, "
      f"{len(medias)} A MEDIAS, {len(mudas)} con el agente mudo (no se pueden leer)")
for v, _, d in sorted(medias):
    print(f"    ⚠️ vm {v}: {d}")
if medias:
    print(f"\n  reparar con:  python3 /root/repara.py {' '.join(str(v) for v, _, _ in sorted(medias))}")
