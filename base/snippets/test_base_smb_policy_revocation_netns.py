"""Persistent IPv4/IPv6 SMB offload revocation in an unprivileged netns lab."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time

import pytest


HERE = Path(__file__).resolve().parent
MODULE = HERE / "base_smb_policy.py"


def _load_policy():
    spec = importlib.util.spec_from_file_location("smb_policy_revocation_netns", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*command, data=None, check=True):
    result = subprocess.run(command, input=data, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{command}: {result.stderr} {result.stdout}")
    return result


def _lab():
    policy = _load_policy()
    children = []
    guests = {}
    try:
        _run("ip", "link", "set", "lo", "up")
        for index, (name, vmid) in enumerate((("a", 100), ("b", 101), ("c", 102), ("d", 103))):
            guest = subprocess.Popen(["unshare", "-n", "sleep", "60"])
            children.append(guest)
            time.sleep(.05)
            guests[name] = guest.pid
            host_if, guest_if = f"tun-{name}", f"eth-{name}"
            _run("ip", "link", "add", host_if, "type", "veth", "peer", "name", guest_if, "netns", str(guest.pid))
            _run("ip", "addr", "add", f"169.254.{index * 4}.1/30", "dev", host_if)
            _run("ip", "-6", "addr", "add", f"fd00:{index + 1}::1/64", "dev", host_if, "nodad")
            _run("ip", "link", "set", host_if, "up")
            for command in (("ip", "link", "set", "lo", "up"),
                            ("ip", "addr", "add", f"169.254.{index * 4}.2/30", "dev", guest_if),
                            ("ip", "-6", "addr", "add", f"fd00:{index + 1}::2/64", "dev", guest_if, "nodad"),
                            ("ip", "link", "set", guest_if, "up"),
                            ("ip", "route", "add", "default", "via", f"169.254.{index * 4}.1"),
                            ("ip", "-6", "route", "add", "default", "via", f"fd00:{index + 1}::1")):
                _run("nsenter", "-t", str(guest.pid), "-n", *command)
            v4, v6 = policy._canonical_guest_addresses(vmid)
            _run("nsenter", "-t", str(guest.pid), "-n", "ip", "addr", "add", f"{v4}/32", "dev", guest_if)
            _run("nsenter", "-t", str(guest.pid), "-n", "ip", "-6", "addr", "add", f"{v6}/128", "dev", guest_if, "nodad")
            _run("ip", "route", "add", f"{v4}/32", "via", f"169.254.{index * 4}.2", "dev", host_if)
            _run("ip", "-6", "route", "add", f"{v6}/128", "via", f"fd00:{index + 1}::2", "dev", host_if)
        _run("sysctl", "-qw", "net.ipv4.ip_forward=1")
        _run("sysctl", "-qw", "net.ipv4.conf.all.rp_filter=0")
        _run("sysctl", "-qw", "net.ipv6.conf.all.forwarding=1")
        _run("nft", "-f", "-", data=textwrap.dedent("""\
            table inet smb_lab_flow {
              flowtable ft { hook ingress priority 0; devices = { tun-a, tun-b, tun-c, tun-d }; }
              chain forward { type filter hook forward priority filter; policy accept;
                ct state established,related flow add @ft
                ct state established,related accept
              }
            }
        """))
        servers = {
            name: {"userId": "alice" if name == "a" else "bob" if name == "b" else "carol", "proxmoxId": vmid,
                   "ipv4": policy._canonical_guest_addresses(vmid)[0], "ipv6": policy._canonical_guest_addresses(vmid)[1]}
            for name, vmid in (("a", 100), ("b", 101), ("c", 102), ("d", 103))
        }
        users = {"alice": {"linkedAccountIds": ["bob"]}, "bob": {"linkedAccountIds": ["alice"]}, "carol": {}}
        before = policy.build_policy(servers, users, {**policy.default_config(), "mode": "enforce"})
        _run("nft", "-f", "-", data=policy.render_nft_bootstrap(before))
        server = r'''
import socket, sys, threading
for family, address in ((socket.AF_INET, sys.argv[1]), (socket.AF_INET6, sys.argv[2])):
  s=socket.socket(family); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((address,445)); s.listen()
  def accept(sock):
    while True:
      c,_=sock.accept(); c.sendall(b'OPEN\n')
      try:
        while c.recv(100): c.sendall(b'PONG\n')
      finally: c.close()
  threading.Thread(target=accept,args=(s,),daemon=True).start()
threading.Event().wait()
'''
        for name in ("b", "d"):
            v4, v6 = policy._canonical_guest_addresses(101 if name == "b" else 103)
            process = subprocess.Popen(["nsenter", "-t", str(guests[name]), "-n", "python3", "-c", server, v4, v6])
            children.append(process)
        time.sleep(.2)
        hold = r'''
import socket, sys
family=socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET
s=socket.socket(family); s.settimeout(5); s.bind((sys.argv[2],0)); s.connect((sys.argv[1],445))
print(s.recv(100).decode().strip(),flush=True)
for line in sys.stdin:
 try: s.sendall(line.encode()); print(s.recv(100).decode().strip(),flush=True)
 except (OSError,TimeoutError): print('CLOSED',flush=True); break
'''
        sessions = []
        for source, target, source_vm, target_vm in (("a", "b", 100, 101), ("c", "d", 102, 103)):
            source_v4, source_v6 = policy._canonical_guest_addresses(source_vm)
            target_v4, target_v6 = policy._canonical_guest_addresses(target_vm)
            for target, source_addr in ((target_v4, source_v4), (target_v6, source_v6)):
                process = subprocess.Popen(["nsenter", "-t", str(guests[source]), "-n", "python3", "-c", hold, target, source_addr], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                children.append(process); sessions.append((source, process))
                actual = process.stdout.readline().strip()
                if actual != "OPEN":
                    raise AssertionError((source, target, source_addr, actual, _run("nft", "list", "ruleset").stdout, _run("ip", "-6", "route").stdout))
                process.stdin.write("ping\n"); process.stdin.flush()
                assert process.stdout.readline().strip() == "PONG"
        time.sleep(.2)
        a4, a6 = policy._canonical_guest_addresses(100)
        b4, b6 = policy._canonical_guest_addresses(101)
        for family, source, destination in (("ipv4", a4, b4), ("ipv6", a6, b6)):
            tracked = _run("conntrack", "-L", "-f", family, "--orig-src", source, "--orig-dst", destination).stdout
            assert "[OFFLOAD]" in tracked, tracked
        # Direct unlink removes a<->b; c<->d is unrelated, allowed traffic
        # which must survive both family-specific conntrack revocations.
        users["alice"]["linkedAccountIds"] = []
        users["bob"]["linkedAccountIds"] = []
        after = policy.build_policy(servers, users, {**policy.default_config(), "mode": "enforce"})
        _run("nft", "-f", "-", data=policy.render_nft_update(after, "enforce"))
        v4_listing = _run("conntrack", "-L", "-f", "ipv4", "-o", "extended").stdout
        v6_listing = _run("conntrack", "-L", "-f", "ipv6", "-o", "extended").stdout
        assert any(item[1:3] == ("10.64.0.100", "10.64.0.101") for item in policy._smb_conntrack_tuples(v4_listing, 4)), v4_listing
        assert any(item[1:3] == ("2a01:4f9:c01f:e::64", "2a01:4f9:c01f:e::65") for item in policy._smb_conntrack_tuples(v6_listing, 6)), v6_listing
        policy.revoke_removed_smb_conntracks(before, after)
        for source, process in sessions:
            process.stdin.write("ping\n"); process.stdin.flush()
            actual = process.stdout.readline().strip()
            assert actual == ("CLOSED" if source == "a" else "PONG"), (source, actual)
    finally:
        for process in reversed(children):
            process.kill()
            process.wait()


@pytest.mark.skipif(
    not shutil.which("unshare") or not shutil.which("nft") or not shutil.which("nsenter") or not shutil.which("conntrack"),
    reason="requires unshare, nft, nsenter and conntrack on PATH",
)
def test_persistent_offloaded_sessions_are_revoked_by_ownership_reuse_only(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text(f"import runpy\nrunpy.run_path({str(__file__)!r})['_lab']()\n")
    result = subprocess.run(["unshare", "-Urn", sys.executable, str(runner)], text=True, capture_output=True, timeout=45, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
