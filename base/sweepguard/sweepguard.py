#!/usr/bin/env python3
"""neuravps-sweepguard — auto-block RDP port SWEEPERS at a BASE.

Why this exists
---------------
The rdpguard rate limits cannot stop a SLOW sweep. `bf_src` limits a source to
60 new RDP conns/min; the botnet that reached a customer's VM on 2026-07-25 ran
at ~3.5/min spread over 675 different customer ports — three orders of magnitude
under the limit, and under the per-(source,port) ip6 limit too. No rate
threshold separates it from a real client, because it is not fast.

What DOES separate them is PORT DIVERSITY:
  * a real client opens its own VM's port — the busiest customer owns 7 servers;
  * a sweeper touched 20-686 distinct ports in one conntrack sample.

So this counts DISTINCT DESTINATION PORTS per source over a rolling window
(nftables set `bf_seen`, populated by a no-verdict rule) and drops sources over
the threshold into `bf_auto`, a set with a TIMEOUT so every automatic block
expires by itself.

Safety (this runs at the edge; a mistake blocks paying customers)
---------------------------------------------------------------
  * `bf_allow` always wins — it is checked first in the chain AND here.
  * `bf_auto` entries EXPIRE (default 24h): a wrong block heals itself.
  * `maxAddsPerRun` caps the blast radius of a bug in this script.
  * `dryRun` (default ON) only logs what it WOULD block.
  * `enabled: false` is a kill switch; a missing/corrupt config disables it.
  * The v4 family is authoritative for v4 clients: on the ip6 side they all
    arrive SNATed to the BASE's own VIP, so ip6 only judges NATIVE v6 sources
    and explicitly skips the 64:ff9b:1::/96 translation range.
"""
import ipaddress
import json
import subprocess
import sys
from collections import defaultdict

CONFIG = "/etc/neuravps-sweepguard.json"
DEFAULTS = {
    "enabled": True,
    "dryRun": True,          # empieza observando; armar solo tras validar
    "minPorts": 20,          # 3x el cliente con mas servidores (7)
    "maxAddsPerRun": 50,
    "blockSeconds": 86400,   # 24h
}
NAT64 = ipaddress.ip_network("64:ff9b:1::/96")


def log(msg):
    print(f"sweepguard: {msg}", flush=True)


def nft_json(args):
    """Run `nft -j <args>` and return the parsed object list (empty on error)."""
    try:
        out = subprocess.run(["nft", "-j"] + args, capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            return []
        return json.loads(out.stdout).get("nftables", [])
    except Exception:
        return []


def set_elements(family, name):
    """Elements of a set. Returns the raw element list (shape varies by type)."""
    for obj in nft_json(["list", "set", family, "rdpguard", name]):
        s = obj.get("set")
        if s and s.get("name") == name:
            return s.get("elem", []) or []
    return []


def plain_addrs(family, name):
    """Set of plain addresses in a simple addr set (ignores prefixes/ranges —
    those only ever appear in the hand-maintained lists and a sweeper matching
    one is already dropped by the chain before us)."""
    out = set()
    for e in set_elements(family, name):
        if isinstance(e, str):
            out.add(e)
        elif isinstance(e, dict) and "elem" in e:
            v = e["elem"].get("val")
            if isinstance(v, str):
                out.add(v)
    return out


def covered_by_lists(family, addr, allow_elems):
    """True if addr falls inside any allow entry (plain, prefix or range)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True   # unparseable -> never touch it
    for e in allow_elems:
        try:
            if isinstance(e, str):
                if ip == ipaddress.ip_address(e):
                    return True
            elif isinstance(e, dict):
                if "prefix" in e:
                    p = e["prefix"]
                    if ip in ipaddress.ip_network(f"{p['addr']}/{p['len']}", strict=False):
                        return True
                elif "range" in e:
                    lo, hi = e["range"]
                    if ipaddress.ip_address(lo) <= ip <= ipaddress.ip_address(hi):
                        return True
                elif "elem" in e:
                    v = e["elem"].get("val")
                    if isinstance(v, str) and ip == ipaddress.ip_address(v):
                        return True
        except Exception:
            continue
    return False


def sweepers(family, cfg):
    """(addr -> distinct port count) for sources over the threshold."""
    per_src = defaultdict(set)
    for e in set_elements(family, "bf_seen"):
        val = e.get("elem", {}).get("val") if isinstance(e, dict) else None
        if not isinstance(val, dict):
            continue
        cat = val.get("concat")
        if not cat or len(cat) < 2:
            continue
        addr, port = cat[0], cat[1]
        if not isinstance(addr, str):
            continue
        if family == "ip6":
            # v4 clients reach us SNATed to our own VIP; judging them here would
            # mean judging every customer as one source. v4 family owns them.
            try:
                if ipaddress.ip_address(addr) in NAT64:
                    continue
            except ValueError:
                continue
        per_src[addr].add(port)
    return {a: len(p) for a, p in per_src.items() if len(p) >= cfg["minPorts"]}


def run_family(family, cfg):
    cand = sweepers(family, cfg)
    if not cand:
        return 0
    allow = set_elements(family, "bf_allow")
    static = set_elements(family, "bf_static")
    already = plain_addrs(family, "bf_auto")

    picked = []
    for addr, nports in sorted(cand.items(), key=lambda kv: -kv[1]):
        if addr in already:
            continue
        if covered_by_lists(family, addr, allow):
            log(f"{family}: SKIP {addr} ({nports} ports) — in bf_allow")
            continue
        if covered_by_lists(family, addr, static):
            continue          # ya bloqueada a mano
        picked.append((addr, nports))

    if not picked:
        return 0
    if len(picked) > cfg["maxAddsPerRun"]:
        log(f"{family}: {len(picked)} candidates exceeds maxAddsPerRun "
            f"{cfg['maxAddsPerRun']} — blocking the worst ones only")
        picked = picked[:cfg["maxAddsPerRun"]]

    for addr, nports in picked:
        if cfg["dryRun"]:
            log(f"{family}: DRY-RUN would block {addr} ({nports} distinct ports)")
            continue
        r = subprocess.run(
            ["nft", "add", "element", family, "rdpguard", "bf_auto",
             "{ %s timeout %ds }" % (addr, cfg["blockSeconds"])],
            capture_output=True, text=True)
        if r.returncode == 0:
            log(f"{family}: BLOCKED {addr} ({nports} distinct ports) "
                f"for {cfg['blockSeconds']}s")
        else:
            log(f"{family}: FAILED to block {addr}: {r.stderr.strip()[:120]}")
    return len(picked)


def main():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        log(f"no {CONFIG} — using defaults (dryRun={cfg['dryRun']})")
    except Exception as exc:
        log(f"config unreadable ({exc}) — refusing to act")
        return 0
    if not cfg.get("enabled"):
        log("disabled by config — nothing to do")
        return 0

    total = 0
    for family in ("ip", "ip6"):
        try:
            total += run_family(family, cfg)
        except Exception as exc:            # nunca romper el timer
            log(f"{family}: ERROR {exc}")
    if total:
        log(f"done: {total} sweeper(s) {'identified' if cfg['dryRun'] else 'blocked'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
