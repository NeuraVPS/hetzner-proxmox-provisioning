#!/usr/bin/env python3
"""End-to-end test of the per-VM outbound IPv4 pools on a FAKE base.

No root, no production: it re-executes itself inside `unshare -rn` (a user +
network namespace), builds base / Internet / guest namespaces joined by veth
pairs named like the real interfaces (`enp2s0`, `tun-hp238`, `tun-fp238`), loads
a production-shaped nftables.conf, and then drives the REAL code:

  - persist-egress-pools-nft.py (simulacro, --apply, idempotencia, --rollback)
  - sync-base-nat.py reconcile_egress_pools / egress_plan

against real TCP connections to a server that echoes the source address it
sees. What it proves (each is an assert):

  1. structure installed with empty maps = every VM leaves by the base main IP
  2. canary only: the canary VM gets its pool address, the others do not
  3. an ESTABLISHED connection keeps its address when the maps change
     (the zero-cut rollout and the zero-cut rollback)
  4. region = tunnel of entry; cross-pool fallback when this base holds the
     VIP of a region but not its block; nothing owned = main IP
  5. a pair outside its block is ignored; EGRESS_POOLS_FORCE_OFF wins
  6. "reboot": `nft -f nftables.conf` (flush ruleset + includes) restores the
     maps from the include file
  7. --rollback removes only the jump and the file still loads
  8. the real sync entry points (single-VM override, delete, `sync egress`)
     carry the pair through state.json and re-render the maps

Run: python3 base/snippets/test-egress-pools-netns.py      (~15 s)
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN = "198.51.100.2"          # base main IPv4 (TEST-NET-2)
INET_GW = "198.51.100.1"
ECHO = ("192.0.2.80", 9000)     # "the broker"
HEL_BLOCK, FSN_BLOCK = "203.0.113.0/26", "203.0.113.64/26"
VM_A, VM_B = "10.64.4.72", "10.64.4.73"   # vm 1096, vm 1097


def sh(*args, check=True, stdin=None, env=None):
    r = subprocess.run(args, input=stdin, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"{args}: {r.stderr.strip() or r.stdout.strip()}")
    return r


# --- topology ------------------------------------------------------------------

class Topo:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.procs = []
        self.inet = self._ns()
        self.guest = self._ns()

    def _ns(self) -> int:
        p = subprocess.Popen(["unshare", "-n", "sleep", "600"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        time.sleep(0.2)
        return p.pid

    def ns(self, pid, *args, check=True):
        return sh("nsenter", "-t", str(pid), "-n", *args, check=check)

    def build(self):
        sh("ip", "link", "set", "lo", "up")
        for pid in (self.inet, self.guest):
            self.ns(pid, "ip", "link", "set", "lo", "up")
        sh("ip", "link", "add", "enp2s0", "type", "veth", "peer", "name", "up0", "netns", str(self.inet))
        sh("ip", "addr", "add", f"{MAIN}/24", "dev", "enp2s0")
        sh("ip", "link", "set", "enp2s0", "up")
        sh("ip", "link", "add", "veth-host", "type", "dummy")
        sh("ip", "link", "set", "veth-host", "up")
        self.ns(self.inet, "ip", "addr", "add", f"{INET_GW}/24", "dev", "up0")
        self.ns(self.inet, "ip", "link", "set", "up0", "up")
        self.ns(self.inet, "ip", "addr", "add", f"{ECHO[0]}/32", "dev", "lo")
        for block in (HEL_BLOCK, FSN_BLOCK):      # Hetzner routes both blocks here
            self.ns(self.inet, "ip", "route", "add", block, "via", MAIN)
        sh("ip", "route", "add", "default", "via", INET_GW)
        for t in ("hp238", "fp238"):
            sh("ip", "link", "add", f"tun-{t}", "type", "veth", "peer", "name", f"g-{t}", "netns", str(self.guest))
            sh("ip", "link", "set", f"tun-{t}", "up")
            self.ns(self.guest, "ip", "link", "set", f"g-{t}", "up")
            sh("sysctl", "-qw", f"net.ipv4.conf.tun-{t}.proxy_arp=1")
            sh("sysctl", "-qw", f"net.ipv4.conf.tun-{t}.rp_filter=0")
        for vm in (VM_A, VM_B):
            self.ns(self.guest, "ip", "addr", "add", f"{vm}/32", "dev", "g-hp238")
            sh("ip", "route", "add", f"{vm}/32", "dev", "tun-hp238")
        self.ns(self.guest, "ip", "route", "add", "default", "dev", "g-hp238")
        sh("sysctl", "-qw", "net.ipv4.ip_forward=1")
        sh("sysctl", "-qw", "net.ipv4.conf.all.rp_filter=0")

    def region(self, which: str):
        """Guest leaves through the tunnel of `which` (hel|fsn) — what the node
        does when it switches region; the base route follows (migration)."""
        dev = "hp238" if which == "hel" else "fp238"
        self.ns(self.guest, "ip", "route", "replace", "default", "dev", f"g-{dev}")
        for vm in (VM_A, VM_B):
            self.ns(self.guest, "ip", "addr", "replace", f"{vm}/32", "dev", f"g-{dev}", check=False)
            sh("ip", "route", "replace", f"{vm}/32", "dev", f"tun-{dev}")

    def close(self):
        for p in self.procs:
            p.kill()


ECHO_SERVER = r"""
import socket, threading
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("%s", %d)); s.listen(64)
def h(c, a):
    try:
        while True:
            d = c.recv(64)
            if not d: break
            c.sendall((a[0] + "\n").encode())
    finally:
        c.close()
while True:
    c, a = s.accept(); threading.Thread(target=h, args=(c, a), daemon=True).start()
"""

CLIENT = r"""
import socket, sys
c = socket.socket(); c.bind((sys.argv[1], 0)); c.settimeout(3); c.connect(("%s", %d))
c.sendall(b"x"); print(c.recv(64).decode().strip())
"""

LONG = r"""
import socket, sys
c = socket.socket(); c.bind((sys.argv[1], 0)); c.settimeout(5); c.connect(("%s", %d))
for line in sys.stdin:
    c.sendall(b"x"); print(c.recv(64).decode().strip(), flush=True)
"""


class Long:
    """A long-lived TCP session from a guest: asks 'what source do you see?'"""

    def __init__(self, topo: Topo, src: str):
        self.p = subprocess.Popen(
            ["nsenter", "-t", str(topo.guest), "-n", "python3", "-c", LONG % ECHO, src],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def ask(self) -> str:
        self.p.stdin.write("?\n")
        self.p.stdin.flush()
        return self.p.stdout.readline().strip()

    def close(self):
        self.p.kill()


def seen(topo: Topo, src: str) -> str:
    r = topo.ns(topo.guest, "python3", "-c", CLIENT % ECHO, src, check=False)
    return r.stdout.strip() or f"FAIL({r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''})"


# --- production-shaped nftables.conf ----------------------------------------------

def base_conf(inc: Path) -> str:
    return f"""#!/usr/sbin/nft -f

flush ruleset

table ip nat {{
    chain prerouting {{
        type nat hook prerouting priority dstnat;
        ip daddr {MAIN} tcp dport 10000-39999 dnat to 10.0.0.3
    }}

    chain postrouting {{
        type nat hook postrouting priority srcnat;
        ct status dnat snat to ct original daddr
        ip saddr 10.0.0.0/24 oifname "enp2s0" snat to {MAIN}
# --- egress-failover (modelo nuevo)
        # Salida IPv4 de los invitados del esquema nuevo y de los nodos que
        # ya no tienen IPv4 publica. Ambos llegan encapsulados por un tunel.
        ip saddr 10.64.0.0/16 iifname "tun-*" oifname "enp2s0" snat to {MAIN}
        ip saddr 10.65.0.0/16 iifname "tun-*" oifname "enp2s0" snat to {MAIN}
    }}
}}

table ip6 nat {{
    map rdp_tcp_map {{
        type inet_service : ipv6_addr . inet_service
    }}
    map rdp_udp_map {{
        type inet_service : ipv6_addr . inet_service
    }}
    map smb_tcp_map {{
        type inet_service : ipv6_addr . inet_service
    }}
    map ssh_tcp_map {{
        type inet_service : ipv6_addr . inet_service
    }}
}}

include "{inc}/base-nat-elements.nft"

table inet filter {{
    set sin_internet6 {{ type ipv6_addr; }}
    set sin_internet4 {{ type ipv4_addr; }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
        iifname "tun-*" oifname "enp2s0" ct direction original ip saddr @sin_internet4 drop
        ct state established,related accept
        iifname "tun-*" oifname "enp2s0" accept
    }}
}}
"""


# --- the test ----------------------------------------------------------------------

def load_sync_module(tmp: Path, inc: Path):
    os.environ.update({
        "MAIN_IPV4": MAIN, "STATE_FILE": str(tmp / "state.json"),
        "NFT_EGRESS_FILE": str(inc / "base-nat-egress-pools.nft"),
        "EGRESS_CONFIG_CACHE": str(tmp / "egress-pools.json"),
        "FIREBASE_CREDENTIALS_FILE": str(tmp / "missing.json"),
    })
    real_exists = Path.exists
    Path.exists = lambda p: False if str(p) == "/var/log/sync-base-nat.log" else real_exists(p)
    logging.FileHandler = lambda *a, **k: logging.NullHandler()
    spec = importlib.util.spec_from_file_location("sbn", HERE / "sync-base-nat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    Path.exists = real_exists
    mod.STATE_DIR = tmp
    mod.LOCK_FILE = tmp / ".sync.lock"
    mod.ensure_firebase = lambda: False
    return mod


def config(enabled=True, fleet=False, canary=(1096,), hel_owner=MAIN, fsn_owner="192.0.2.250"):
    return {"enabled": enabled, "fleetWide": fleet, "canaryVmids": list(canary),
            "noticeCompletedAt": "2026-01-01T00:00:00Z", "fleetNotBefore": "2026-01-09T00:00:00Z",
            "pools": {"hel": {"cidrs": [HEL_BLOCK], "kind": "failover", "activeServerIp": hel_owner},
                      "fsn": {"cidrs": [FSN_BLOCK], "kind": "failover", "activeServerIp": fsn_owner}}}


def desired(mod, pair_a=("203.0.113.5", "203.0.113.69"), pair_b=("203.0.113.6", "203.0.113.70")):
    return {
        1096: mod._server_entry("2a01:4f9:c01f:e::448", node_id="0000238-AX162-2-LTD", ipv4=VM_A,
                                egress_hel=pair_a[0], egress_fsn=pair_a[1]),
        1097: mod._server_entry("2a01:4f9:c01f:e::449", node_id="0000238-AX162-2-LTD", ipv4=VM_B,
                                egress_hel=pair_b[0], egress_fsn=pair_b[1]),
    }


def check(label, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {label}: {got}" + ("" if ok else f" (want {want})"))
    if not ok:
        raise AssertionError(label)


def run_inside():
    tmp = Path(tempfile.mkdtemp(prefix="egress-netns-"))
    inc = tmp / "nftables.d"
    inc.mkdir()
    (inc / "base-nat-elements.nft").write_text("# empty\n")
    conf = tmp / "nftables.conf"
    conf.write_text(base_conf(inc))
    defaults = tmp / "base-nat"
    defaults.write_text(f"MAIN_IPV4={MAIN}\n")
    topo = Topo(tmp)
    server = None
    try:
        topo.build()
        sh("nft", "-f", str(conf))
        server = subprocess.Popen(["nsenter", "-t", str(topo.inet), "-n", "python3", "-c", ECHO_SERVER % ECHO],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        env = dict(os.environ, NFT_CONF=str(conf), BASE_NAT_DEFAULTS=str(defaults), NFT_INCLUDE_DIR=str(inc))
        persist = [sys.executable, str(HERE / "persist-egress-pools-nft.py")]

        print("0) today, before anything")
        check("vm1096 leaves by", seen(topo, VM_A), MAIN)

        print("1) persist script: dry run changes nothing, --apply installs inert structure")
        before = sh("nft", "list", "ruleset").stdout
        sh(*persist, env=env)
        check("dry run left ruleset untouched", sh("nft", "list", "ruleset").stdout == before, True)
        check("dry run left file untouched", conf.read_text() == base_conf(inc), True)
        long_main = Long(topo, VM_A)
        check("long session (opened before) sees", long_main.ask(), MAIN)
        out = sh(*persist, "--apply", env=env).stdout
        post = sh("nft", "-a", "list", "chain", "ip", "nat", "postrouting").stdout
        jump_pos = post.index("jump egress_pools")
        check("jump sits before the general 10.64 rule",
              jump_pos < post.index(f'ip saddr 10.64.0.0/16 iifname "tun-*" oifname "enp2s0" snat to {MAIN}'), True)
        check("structure with empty maps: vm1096", seen(topo, VM_A), MAIN)
        check("second --apply is a no-op", "ya aplicado" in sh(*persist, "--apply", env=env).stdout, True)
        check("persisted file validates", sh("nft", "-c", "-f", str(conf), check=False).returncode, 0)

        mod = load_sync_module(tmp, inc)
        d = desired(mod)

        print("2) canary only (vm1096), this base owns the HEL block")
        mod.reconcile_egress_pools(d, config())
        check("vm1096 (canary)", seen(topo, VM_A), "203.0.113.5")
        check("vm1097 (not canary)", seen(topo, VM_B), MAIN)
        check("established session kept its address", long_main.ask(), MAIN)
        long_pool = Long(topo, VM_A)
        check("new long session", long_pool.ask(), "203.0.113.5")

        print("3) fleet-wide")
        mod.reconcile_egress_pools(d, config(fleet=True))
        check("vm1097", seen(topo, VM_B), "203.0.113.6")

        print("4) ownership and region")
        mod.reconcile_egress_pools(d, config(fleet=True, hel_owner="192.0.2.250", fsn_owner=MAIN))
        check("HEL tunnel, only FSN block here -> other IP of the pair", seen(topo, VM_A), "203.0.113.69")
        topo.region("fsn")
        check("FSN tunnel, FSN block here", seen(topo, VM_A), "203.0.113.69")
        mod.reconcile_egress_pools(d, config(fleet=True, hel_owner=MAIN, fsn_owner="192.0.2.250"))
        check("FSN tunnel, only HEL block here -> other IP of the pair", seen(topo, VM_A), "203.0.113.5")
        mod.reconcile_egress_pools(d, config(fleet=True, hel_owner="192.0.2.250", fsn_owner="192.0.2.251"))
        check("no block owned -> main IP", seen(topo, VM_A), MAIN)
        mod.reconcile_egress_pools(d, config(fleet=True, hel_owner=MAIN, fsn_owner=MAIN))
        check("both blocks here, FSN tunnel", seen(topo, VM_A), "203.0.113.69")
        topo.region("hel")
        check("both blocks here, HEL tunnel", seen(topo, VM_A), "203.0.113.5")

        print("5) guards")
        bad = desired(mod, pair_a=("198.18.0.5", "203.0.113.200"))
        plan, notes = mod.egress_plan(bad, config(fleet=True), MAIN)
        check("pair outside its block ignored", plan["hel"].get(VM_A), None)
        mod.reconcile_egress_pools(bad, config(fleet=True))
        check("vm1096 with a bad pair", seen(topo, VM_A), MAIN)
        masked = desired(mod, pair_a=("203.0.113.0", "203.0.113.64"))
        check("network address never used",
              mod.egress_plan(masked, config(fleet=True), MAIN)[0]["hel"].get(VM_A), None)
        invalid = config(fleet=True)
        invalid["pools"]["hel"]["cidrs"] = ["10.9.0.0/26"]
        check("private block = feature off", mod.egress_plan(d, invalid, MAIN)[0], {"hel": {}, "fsn": {}})
        mod.EGRESS_POOLS_FORCE_OFF = True
        check("FORCE_OFF", mod.egress_plan(d, config(fleet=True), MAIN)[0], {"hel": {}, "fsn": {}})
        mod.EGRESS_POOLS_FORCE_OFF = False

        print("6) reboot: flush ruleset + includes restores the maps")
        mod.reconcile_egress_pools(d, config(fleet=True))
        check("before reboot", seen(topo, VM_B), "203.0.113.6")
        sh("nft", "-f", str(conf))
        check("after `nft -f nftables.conf`", seen(topo, VM_B), "203.0.113.6")

        print("8) real sync entry points")
        mod.NFT_INCLUDE_FILE = inc / "base-nat-elements.nft"
        mod.NFT_SIN_INET_FILE = inc / "base-nat-sin-internet.nft"
        (tmp / "egress-pools.json").write_text(json.dumps({"config": config(fleet=True)}))
        mod.write_state(mod._state_payload(desired(mod)))
        mod.sync_single_vmid(1096, mod._server_entry("2a01:4f9:c01f:e::448", rdp=True, samba=True),
                             delete_only=False, ipv6_override=True)
        st = json.loads((tmp / "state.json").read_text())
        check("override sync kept the pair in state", st["1096"]["egressHel"], "203.0.113.5")
        check("override sync: vm1096", seen(topo, VM_A), "203.0.113.5")
        mod.sync_single_vmid(1097, None, delete_only=True)
        check("deleted vm1097 leaves by main IP", seen(topo, VM_B), MAIN)

        class Snap:
            def __init__(self, data, doc_id="x"):
                self._d, self.exists, self.id = data, data is not None, doc_id

            def to_dict(self):
                return self._d

        docs = [Snap({"proxmoxId": 1096, "ipv4": VM_A,
                      "egressIpv4": {"hel": "203.0.113.9", "fsn": "203.0.113.73"}})]

        class FakeFS:
            def client(self):
                return self

            def collection(self, name):
                self.name = name
                return self

            def document(self, _):
                return self

            def get(self):
                return Snap(config(fleet=True))

            def select(self, _):
                return self

            def stream(self):
                return docs

        mod.firestore = FakeFS()
        mod.ensure_firebase = lambda: True
        mod.sync_egress()
        check("`sync egress` picked the new pair from Firestore", seen(topo, VM_A), "203.0.113.9")
        check("and wrote it to state", json.loads((tmp / "state.json").read_text())["1096"]["egressHel"], "203.0.113.9")
        mod.ensure_firebase = lambda: False
        mod.write_state(mod._state_payload(d))
        mod.reconcile_egress_pools(d, config(fleet=True))

        print("7) kill switch keeps established sessions; rollback removes only the jump")
        long_pool2 = Long(topo, VM_B)
        check("session on pool address", long_pool2.ask(), "203.0.113.6")
        mod.reconcile_egress_pools(d, config(enabled=False))
        check("enabled=false: new connection", seen(topo, VM_B), MAIN)
        check("enabled=false: established session unchanged", long_pool2.ask(), "203.0.113.6")
        mod.reconcile_egress_pools(d, config(fleet=True))
        sh(*persist, "--rollback", "--apply", env=env)
        check("after rollback", seen(topo, VM_B), MAIN)
        check("rolled-back file validates", sh("nft", "-c", "-f", str(conf), check=False).returncode, 0)
        check("jump gone from file", "jump egress_pools" in conf.read_text(), False)
        for s in (long_main, long_pool, long_pool2):
            s.close()
        print("ALL OK")
    finally:
        if server:
            server.kill()
        topo.close()


def main():
    if os.environ.get("_EGRESS_NETNS_INSIDE") != "1":
        env = dict(os.environ, _EGRESS_NETNS_INSIDE="1")
        r = subprocess.run(["unshare", "-rn", sys.executable, __file__], env=env, timeout=120)
        sys.exit(r.returncode)
    run_inside()


if __name__ == "__main__":
    main()
