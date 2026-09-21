"""Real TCP forwarding checks for the BASE SMB policy in an isolated netns."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


MODULE = Path(__file__).with_name("base_smb_policy.py")
SPEC = importlib.util.spec_from_file_location("base_smb_policy_netns", MODULE)
policy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


@pytest.mark.skipif(
    not shutil.which("unshare") or not shutil.which("nft") or not shutil.which("nsenter"),
    reason="requires unshare, nft and nsenter",
)
def test_real_tcp_audit_and_enforcement_with_guest_and_infrastructure_paths(tmp_path):
    """Exercise forward traffic, not just rendered rule strings.

    The outer namespace is BASE. a/b are same-owner registered guests, c is a
    different registered owner, and d is deliberately unregistered but uses a
    valid VMID-shaped guest address.  Infrastructure is outside that range;
    d must be blocked in enforcement without granting unregistered guests an
    implicit exception.
    """
    servers = {
        "a": {"userId": "owner-a", "proxmoxId": 100, "ipv4": "10.64.0.100", "ipv6": "2a01:4f9:c01f:e::64"},
        "b": {"userId": "owner-a", "proxmoxId": 101, "ipv4": "10.64.0.101", "ipv6": "2a01:4f9:c01f:e::65"},
        "c": {"userId": "owner-c", "proxmoxId": 102, "ipv4": "10.64.0.102", "ipv6": "2a01:4f9:c01f:e::66"},
    }
    users = {"owner-a": {}, "owner-c": {}}
    audit = policy.build_policy(servers, users, policy.default_config())
    enforce = policy.build_policy(servers, users, {**policy.default_config(), "mode": "enforce"})
    (tmp_path / "audit.nft").write_text(policy.render_nft_bootstrap(audit))
    (tmp_path / "enforce.nft").write_text(policy.render_nft_update(enforce, "audit"))

    harness = tmp_path / "harness.sh"
    harness.write_text(textwrap.dedent("""\
        #!/bin/sh
        set -eu
        work=$1
        child_pids=""
        cleanup() {
          for pid in $child_pids; do kill "$pid" 2>/dev/null || true; done
        }
            trap cleanup EXIT INT TERM
            make_guest() {
              name=$1
              address=$2
              link=$3
          unshare -n sh -c 'exec sleep 45' &
          GUEST_PID=$!
          child_pids="$child_pids $GUEST_PID"
          ip link add "tun-$name" type veth peer name "guest-$name"
              ip link set "guest-$name" netns "$GUEST_PID"
              ip addr add "169.254.$link.1/30" dev "tun-$name"
              ip link set "tun-$name" up
              ip route add "$address/32" via "169.254.$link.2" dev "tun-$name"
              nsenter -t "$GUEST_PID" -n ip link set lo up
              nsenter -t "$GUEST_PID" -n ip addr add "169.254.$link.2/30" dev "guest-$name"
              nsenter -t "$GUEST_PID" -n ip addr add "$address/32" dev lo
              nsenter -t "$GUEST_PID" -n ip link set "guest-$name" up
              nsenter -t "$GUEST_PID" -n ip route add default via "169.254.$link.1"
            }
            make_guest a 10.64.0.100 0; pid_a=$GUEST_PID
            make_guest b 10.64.0.101 4; pid_b=$GUEST_PID
            make_guest c 10.64.0.102 8; pid_c=$GUEST_PID
            make_guest d 10.64.4.72 12; pid_d=$GUEST_PID
        sysctl -qw net.ipv4.ip_forward=1
        serve() {
          nsenter -t "$1" -n python3 -c '
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 445)); s.listen()
while True:
    c, _ = s.accept(); c.close()
' >/dev/null 2>&1 &
          child_pids="$child_pids $!"
        }
        serve "$pid_a"
        serve "$pid_b"
        sleep 0.1
        client() {
          nsenter -t "$1" -n python3 -c '
import socket, sys
s = socket.socket(); s.settimeout(1); s.bind((sys.argv[2], 0)); s.connect((sys.argv[1], 445)); s.close()
' "$2" "$3"
        }
        client_sport_137() {
          nsenter -t "$1" -n python3 -c '
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.settimeout(1); s.bind((sys.argv[2], 137)); s.connect((sys.argv[1], 445)); s.close()
' "$2" "$3"
        }
        nft -f "$work/audit.nft"
        client "$pid_a" 10.64.0.101 10.64.0.100
        client "$pid_c" 10.64.0.100 10.64.0.102
        client "$pid_d" 10.64.0.100 10.64.4.72
        client_sport_137 "$pid_c" 10.64.0.100 10.64.0.102
        nft -j list chain inet nvx_smb_policy forward > "$work/audit-counters.json"
        python3 - "$work/audit-counters.json" <<'PY'
import json, sys
rules = [x["rule"] for x in json.load(open(sys.argv[1]))["nftables"] if "rule" in x]
def packets(comment):
    total = 0
    for rule in rules:
        if rule.get("comment") != comment:
            continue
        for expression in rule.get("expr", []):
            total += expression.get("counter", {}).get("packets", 0)
    return total
assert packets("smb-policy known pair") > 0
assert packets("smb-policy unknown candidate") > 0
PY
        nft -f "$work/enforce.nft"
        client "$pid_a" 10.64.0.101 10.64.0.100
        if client "$pid_c" 10.64.0.100 10.64.0.102; then
          echo "cross-owner TCP/445 unexpectedly passed enforcement" >&2; exit 1
        fi
        # d has a valid VMID-shaped address but no document: never implicitly
        # allow it merely because it is not in the current Firestore snapshot.
        if client "$pid_d" 10.64.0.100 10.64.4.72; then
          echo "unknown VMID-range TCP/445 unexpectedly passed enforcement" >&2; exit 1
        fi
        if client_sport_137 "$pid_c" 10.64.0.100 10.64.0.102; then
          echo "TCP source-port 137 bypassed the dport 445 policy" >&2; exit 1
        fi
    """))
    result = subprocess.run(
        ["unshare", "-Urn", "sh", str(harness), str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
