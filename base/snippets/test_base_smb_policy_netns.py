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
    different registered owner, and d is deliberately unregistered to model a
    BASE/public/NAT path which this account policy must leave to existing rules.
    """
    servers = {
        "a": {"userId": "owner-a", "proxmoxId": 1, "ipv4": "10.200.1.2"},
        "b": {"userId": "owner-a", "proxmoxId": 2, "ipv4": "10.200.2.2"},
        "c": {"userId": "owner-c", "proxmoxId": 3, "ipv4": "10.200.3.2"},
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
          subnet=$2
          unshare -n sh -c 'exec sleep 45' &
          GUEST_PID=$!
          child_pids="$child_pids $GUEST_PID"
          ip link add "tun-$name" type veth peer name "guest-$name"
          ip link set "guest-$name" netns "$GUEST_PID"
          ip addr add "$subnet.1/24" dev "tun-$name"
          ip link set "tun-$name" up
          nsenter -t "$GUEST_PID" -n ip link set lo up
          nsenter -t "$GUEST_PID" -n ip addr add "$subnet.2/24" dev "guest-$name"
          nsenter -t "$GUEST_PID" -n ip link set "guest-$name" up
          nsenter -t "$GUEST_PID" -n ip route add default via "$subnet.1"
        }
        make_guest a 10.200.1; pid_a=$GUEST_PID
        make_guest b 10.200.2; pid_b=$GUEST_PID
        make_guest c 10.200.3; pid_c=$GUEST_PID
        make_guest d 10.200.4; pid_d=$GUEST_PID
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
s = socket.create_connection((sys.argv[1], 445), timeout=1); s.close()
' "$2"
        }
        client_sport_137() {
          nsenter -t "$1" -n python3 -c '
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.settimeout(1); s.bind(("", 137)); s.connect((sys.argv[1], 445)); s.close()
' "$2"
        }
        nft -f "$work/audit.nft"
        client "$pid_a" 10.200.2.2
        client "$pid_c" 10.200.1.2
        client "$pid_d" 10.200.1.2
        client_sport_137 "$pid_c" 10.200.1.2
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
        client "$pid_a" 10.200.2.2
        if client "$pid_c" 10.200.1.2; then
          echo "cross-owner TCP/445 unexpectedly passed enforcement" >&2; exit 1
        fi
        # d is not in smb_guests_v4: this policy does not blanket-drop it.
        client "$pid_d" 10.200.1.2
        if client_sport_137 "$pid_c" 10.200.1.2; then
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
