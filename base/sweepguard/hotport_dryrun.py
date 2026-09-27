#!/usr/bin/env python3
"""Evaluacion en seco del detector hot-port: regla VIEJA frente a regla NUEVA.

No toca nada: lee volcados de conntrack (o /proc/net/nf_conntrack en la propia
base) y, si se le da, el journal de sweepguard. Aplica las dos reglas con los
MISMOS umbrales y explica cada diferencia. bf_allow y bf_auto se ignoran a
proposito: se evalua lo que el detector decidiria, como si la IP no estuviera
exenta ni bloqueada ya.

    # en la base, contra el estado de ahora mismo
    python3 hotport_dryrun.py /proc/net/nf_conntrack

    # desde fuera, con varias muestras separadas unos minutos
    for i in 1 2 3; do ssh -n b1 cat /proc/net/nf_conntrack > ct_b1_$i.txt; sleep 180; done
    ssh -n b1 journalctl -u neuravps-sweepguard.service --since -2d -o short-iso > j_b1.txt
    ssh -n b1 nft -j list set ip rdpguard nuestras4 > ours_b1.json
    ssh -n b1 sysctl -n net.netfilter.nf_conntrack_tcp_timeout_established   # -> --est-timeout
    python3 hotport_dryrun.py --label b1 --ours-json ours_b1.json --est-timeout 432000 \\
        --journal j_b1.txt --watch 79.158.167.228 ct_b1_*.txt

La hora de cada muestra es el mtime del fichero (captura -> fichero sin copiar
despues), o `fichero@epoch` para darla a mano.
"""
import argparse
import datetime
import importlib.util
import ipaddress
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("sweepguard", os.path.join(HERE, "sweepguard.py"))
sg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sg)

_J_RE = re.compile(r"^(\S+) .*?sweepguard: (ip6?): BLOCKED (\S+) \((\d+) (?:live )?connections onto "
                   r"ONE attacked port \(port (\d+)")


def ours_from_json(path):
    """Predicado 'destino nuestro' desde `nft -j list set ... nuestras4`."""
    with open(path) as fh:
        objs = json.load(fh).get("nftables", [])
    old = sg.nft_json
    sg.nft_json = lambda args: objs
    try:
        fam = "ip6" if any(o.get("set", {}).get("family") == "ip6" for o in objs) else "ip"
        return sg.guarded_destinations(fam)
    finally:
        sg.nft_json = old


def load_sample(spec, family, ours):
    path, _, epoch = spec.partition("@")
    t = float(epoch) if epoch else os.stat(path).st_mtime
    want = "ipv4" if family == "ip" else "ipv6"
    out = []
    with open(path) as fh:
        for ln in fh:
            if not ln.startswith(want) or "dport=" not in ln:
                continue
            e = sg.parse_ct(ln)
            if e is None or not sg.in_range(e["dport"]):
                continue
            if ours is not None and not ours(e["dst"]):
                continue
            e["src"] = sg.canon(e["src"])
            if family == "ip6":
                try:
                    if ipaddress.ip_address(e["src"]) in sg.NAT64:
                        continue
                except ValueError:
                    continue
            out.append(e)
    return path, t, out


# La regla desplegada hasta el 27/09/2026, con sus umbrales de entonces.
OLD_CFG = {"hotPortMinSrc": 5, "hotPortMinTotal": 20}


def old_rule(entries, cfg=OLD_CFG):
    """La regla de antes: toda conexion TCP cuenta, viva o muerta."""
    pair, per_port = defaultdict(int), defaultdict(int)
    for e in entries:
        if e["proto"] == "tcp":
            pair[(e["src"], e["dport"])] += 1
            per_port[e["dport"]] += 1
    out = {}
    for (a, p), n in pair.items():
        if n >= cfg["hotPortMinSrc"] and per_port[p] >= cfg["hotPortMinTotal"]:
            if a not in out or n > out[a][0]:
                out[a] = (n, p)
    return out


def describe(addr, port, entries, est, cfg):
    """Por que la regla nueva no la bloquea: numeros de esa fuente en ese puerto."""
    mine = [e for e in entries if e["src"] == addr and e["dport"] == port and e["proto"] == "tcp"]
    live = [e for e in mine if sg.is_live(e, est, cfg["hotPortMaxIdle"])]
    port_live = sum(1 for e in entries if e["proto"] == "tcp" and e["dport"] == port
                    and sg.is_live(e, est, cfg["hotPortMaxIdle"]))
    idles = [est - e["timeout"] for e in mine if e["state"] == "ESTABLISHED" and not e["offload"]
             and e["timeout"] is not None and est is not None]
    idle_txt = f"silencio min {min(idles) // 60} min" if idles else "sin ESTABLISHED"
    return len(mine), len(live), port_live, idle_txt


def fmt_t(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%d/%m %H:%M:%SZ")


def journal_blocks(path, family):
    out = []
    with open(path) as fh:
        for ln in fh:
            m = _J_RE.search(ln)
            if not m or m.group(2) != family:
                continue
            ts = datetime.datetime.fromisoformat(m.group(1)).timestamp()
            out.append((ts, sg.canon(m.group(3)), int(m.group(4)), int(m.group(5))))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("samples", nargs="+", help="volcados de conntrack (fichero o fichero@epoch)")
    ap.add_argument("--label", default="base")
    ap.add_argument("--family", default="ip", choices=["ip", "ip6"])
    ap.add_argument("--ours-json", help="nft -j list set <fam> rdpguard nuestras4/6")
    ap.add_argument("--est-timeout", type=int, help="nf_conntrack_tcp_timeout_established de la base "
                    "(por defecto el de esta maquina)")
    ap.add_argument("--journal", help="journal de sweepguard (-o short-iso)")
    ap.add_argument("--watch", action="append", default=[], help="IP con veredicto detallado")
    ap.add_argument("--config", help="JSON con umbrales (por defecto DEFAULTS de sweepguard.py)")
    a = ap.parse_args(argv)

    cfg = dict(sg.DEFAULTS)
    if a.config:
        with open(a.config) as fh:
            cfg.update(json.load(fh))
    est = a.est_timeout if a.est_timeout is not None else sg.established_timeout()
    if a.ours_json:
        ours = ours_from_json(a.ours_json)
    elif os.path.exists(sg.CONNTRACK) and a.samples == [sg.CONNTRACK]:
        ours = sg.guarded_destinations(a.family)       # en la propia base
    else:
        ours = None
    watch = {sg.canon(w) for w in a.watch}

    print(f"# {a.label} — hot-port vieja (minSrc={OLD_CFG['hotPortMinSrc']} minTotal="
          f"{OLD_CFG['hotPortMinTotal']}, todas las conexiones) vs nueva (minSrc={cfg['hotPortMinSrc']} "
          f"minTotal={cfg['hotPortMinTotal']} vivas, maxIdle={cfg['hotPortMaxIdle']}s "
          f"memoria={cfg['sessionMemorySeconds']}s est_timeout={est} "
          f"filtro destino={'si' if ours else 'NO'})")
    samples = sorted((load_sample(s, a.family, ours) for s in a.samples), key=lambda x: x[1])
    sessions = {}
    summary = {"old": set(), "new": set(), "ghost": set(), "session": set(), "port": set()}
    for path, t, entries in samples:
        old = old_rule(entries)
        new, skipped = sg.hot_port_scan(entries, cfg, est, sessions, now=t)
        n_tcp = sum(1 for e in entries if e["proto"] == "tcp")
        n_live = sum(1 for e in entries if e["proto"] == "tcp" and sg.is_live(e, est, cfg["hotPortMaxIdle"]))
        print(f"\n## {os.path.basename(path)} @ {fmt_t(t)}: TCP a forwards {n_tcp}, vivas {n_live}; "
              f"vieja {len(old)}, nueva {len(new)}, sesiones recordadas {len(sessions)}")
        summary["old"] |= set(old)
        summary["new"] |= set(new)
        for addr in sorted(set(old) | set(new), key=lambda x: -(old.get(x) or new.get(x))[0]):
            if addr in new:
                n, p = new[addr]
                mark = "sigue" if addr in old else "SOLO NUEVA"
                print(f"  {mark:10} {addr:18} {n:4} vivas → puerto {p} (VM {sg.vm_slot(p)})")
                continue
            n, p = old[addr]
            tot, live, port_live, idle_txt = describe(addr, p, entries, est, cfg)
            if addr in skipped:
                why = skipped[addr][2]
                summary["session"].add(addr)
            elif live < cfg["hotPortMinSrc"]:
                why = f"fantasma: {live}/{tot} vivas ({idle_txt})"
                summary["ghost"].add(addr)
            else:
                why = f"puerto con {port_live} vivas < {cfg['hotPortMinTotal']}"
                summary["port"].add(addr)
            print(f"  {'deja':10} {addr:18} {tot:4} conex. → puerto {p} (VM {sg.vm_slot(p)}): {why}")
        for w in sorted(watch):
            mine = [e for e in entries if e["src"] == w]
            if not mine:
                print(f"  [watch] {w}: sin entradas")
                continue
            by = defaultdict(lambda: [0, 0])
            for e in mine:
                k = (e["proto"], e["dport"])
                by[k][0] += 1
                by[k][1] += sg.is_live(e, est, cfg["hotPortMaxIdle"]) if e["proto"] == "tcp" else 0
            det = ", ".join(f"{pr}/{p}: {n} ({lv} vivas)" if pr == "tcp" else f"{pr}/{p}: {n}"
                            for (pr, p), (n, lv) in sorted(by.items()))
            verdict = ("BLOQUEA (vieja y nueva)" if w in new and w in old else
                       "BLOQUEA solo la nueva" if w in new else
                       "vieja BLOQUEA, nueva NO" if w in old else "ninguna bloquea")
            sess = "sesión RDP ahora" if any(sg.proves_rdp_session(e) for e in mine) else (
                "sesión RDP en memoria" if w in sessions else "sin sesión RDP")
            print(f"  [watch] {w}: {verdict}; {sess}; {det}")

    print(f"\n## Resumen de muestras: vieja {len(summary['old'])} fuentes, nueva {len(summary['new'])}; "
          f"dejan de bloquearse {len(summary['old'] - summary['new'])} "
          f"(fantasma {len(summary['ghost'] - summary['new'])}, sesión {len(summary['session'] - summary['new'])}, "
          f"puerto bajo {len(summary['port'] - summary['new'])})")

    if not a.journal:
        return 0
    blocks = journal_blocks(a.journal, a.family)
    if not blocks:
        print("\n## Journal: sin bloqueos hot-port")
        return 0
    last_t = samples[-1][1]
    # re-bloqueos: misma fuente bloqueada otra vez al caducar la anterior
    by_src = defaultdict(list)
    for ts, addr, n, p in blocks:
        by_src[addr].append(ts)
    rebl = sum(1 for ts_list in by_src.values() for x, y in zip(ts_list, ts_list[1:])
               if y - x <= cfg["hotPortBlockSeconds"] + 900)
    print(f"\n## Journal: {len(blocks)} bloqueos hot-port de {len(by_src)} fuentes "
          f"({fmt_t(blocks[0][0])} → {fmt_t(blocks[-1][0])}); {rebl} "
          f"({100 * rebl // len(blocks)} %) son re-bloqueos ≤{(cfg['hotPortBlockSeconds'] + 900) // 60} min "
          f"después del anterior de la misma fuente")

    # bloqueos vigentes: ultimo de cada fuente dentro de hotPortBlockSeconds antes de la ULTIMA muestra
    active = {}
    for ts, addr, n, p in blocks:
        if last_t - cfg["hotPortBlockSeconds"] <= ts <= last_t:
            active[addr] = (ts, n, p)
    print(f"\n## Bloqueos hot-port vigentes a {fmt_t(last_t)}: {len(active)}")
    print("   Evidencia reconstruida con la primera muestra posterior al bloqueo: una fuente\n"
          "   bloqueada no crea conexiones nuevas, asi que sus ESTABLISHED de entonces siguen ahi\n"
          "   y su silencio dice si estaban vivas en el momento del bloqueo.")
    kept = dropped = 0
    rows = []
    for addr, (ts, n, p) in sorted(active.items(), key=lambda kv: -kv[1][1]):
        after = [s for s in samples if s[1] >= ts]
        if not after:
            rows.append((addr, p, n, "sin muestra posterior", "?"))
            continue
        _, t, entries = after[0]
        mine = [e for e in entries if e["src"] == addr and e["dport"] == p and e["proto"] == "tcp"]
        stale_at_block = 0
        for e in mine:
            if e["offload"] or e["state"] != "ESTABLISHED" or e["timeout"] is None or est is None:
                continue
            last_pkt = t - (est - e["timeout"])
            if last_pkt < ts - cfg["hotPortMaxIdle"]:
                stale_at_block += 1
        live_lb = n - stale_at_block          # lo que no se explica con conexiones muertas
        session = any(sg.proves_rdp_session(e) for e in entries if e["src"] == addr)
        if session:
            verdict, why = "DEJA", "sesión RDP demostrada"
            dropped += 1
        elif live_lb < cfg["hotPortMinSrc"]:
            verdict, why = "DEJA", f"fantasma: {stale_at_block} de {n} callaban >{cfg['hotPortMaxIdle']}s al bloquear"
            dropped += 1
        else:
            verdict, why = "SIGUE", f"solo {stale_at_block} de {n} callaban al bloquear: la evidencia no es solo fantasma"
            kept += 1
        rows.append((addr, p, n, why, verdict))
    for addr, p, n, why, verdict in rows:
        print(f"  {verdict:5} {addr:18} {n:4} conex. puerto {p} (VM {sg.vm_slot(p)}): {why}")
    print(f"\n   seguirían bloqueados {kept}, dejarían de estarlo {dropped}")

    # Primer bloqueo de cada (fuente, puerto) cuya evidencia sigue en conntrack.
    # Es la comprobacion positiva: las ESTABLISHED congeladas por aquel primer
    # bloqueo guardan cuando fue su ultimo paquete, asi que se puede recontar
    # cuantas estaban VIVAS en ese instante y ver si la regla nueva tambien
    # habria bloqueado. Cotas inferiores: las conexiones que ya caducaron
    # (SYN, TIME_WAIT...) estaban vivas y no se ven.
    _, t, entries = samples[-1]
    first = {}
    for ts, addr, n, p in blocks:
        first.setdefault((addr, p), (ts, n))
    est_entries = [e for e in entries if e["proto"] == "tcp" and e["state"] == "ESTABLISHED"
                   and not e["offload"] and e["timeout"] is not None and est is not None]
    by_port = defaultdict(list)
    for e in est_entries:
        by_port[e["dport"]].append((e["src"], t - (est - e["timeout"])))
    rows = []
    for (addr, p), (ts, n) in first.items():
        pkts = [lp for s, lp in by_port.get(p, []) if s == addr and lp <= ts + 60]
        if len(pkts) < cfg["hotPortMinSrc"]:
            continue                       # evidencia ya caducada: no se puede recontar
        live_src = sum(1 for lp in pkts if lp >= ts - cfg["hotPortMaxIdle"])
        live_port = sum(1 for s, lp in by_port[p] if ts - cfg["hotPortMaxIdle"] <= lp <= ts + 60)
        caught = live_src >= cfg["hotPortMinSrc"] and live_port >= cfg["hotPortMinTotal"]
        rows.append((addr, p, ts, n, len(pkts), live_src, live_port, caught))
    if rows:
        print(f"\n## Primer bloqueo con evidencia aún en conntrack: {len(rows)} (fuente, puerto)")
        for addr, p, ts, n, seen, ls, lpo, caught in sorted(rows, key=lambda r: r[2]):
            print(f"  {'NUEVA TAMBIÉN' if caught else 'nueva no':13} {addr:18} {fmt_t(ts)} puerto {p} "
                  f"(VM {sg.vm_slot(p)}): journal {n}, congeladas {seen}, vivas al bloquear ≥{ls}, "
                  f"puerto vivas ≥{lpo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
