#!/usr/bin/env bash
# Sync Proxmox firewall rules from each node to Firestore (servers.firewall).
# Python is embedded below; nothing is stored on the server.

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Syncing firewall to Firestore =="
  python3 << 'PYEOF'
import subprocess
import json
import os
import sys
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore_v1 import FieldFilter
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    FieldFilter = None

NODE_NAME = subprocess.run(
    ["hostname"], capture_output=True, text=True, check=True
).stdout.strip()

def run_cmd(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip()

def get_vmids():
    out = run_cmd(["qm", "list"])
    if not out:
        return []
    vmids = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 1:
            try:
                vmids.append(int(parts[0]))
            except ValueError:
                pass
    return vmids

def get_firewall_rules(vmid):
    out = run_cmd(
        ["pvesh", "get", "/nodes/{}/qemu/{}/firewall/rules".format(NODE_NAME, vmid)]
    )
    if not out:
        return []
    try:
        parsed = json.loads(out)
        if isinstance(parsed, list):
            return parsed
        return parsed.get("data", [])
    except json.JSONDecodeError:
        return []

def find_rule_by_action(rules, action):
    for r in rules:
        if r.get("action") == action:
            return r
    return None

def parse_firewall_rules(rules):
    ipv6_r = find_rule_by_action(rules, "vm-public-ipv6")
    no_int = find_rule_by_action(rules, "vm-no-internet")
    no_rdp = find_rule_by_action(rules, "vm-no-rdp")
    no_samba = find_rule_by_action(rules, "vm-no-samba")
    ipv6_enabled = ipv6_r.get("enable", 0) == 1 if ipv6_r else False
    internet_enabled = no_int.get("enable", 0) == 0 if no_int else True
    rdp_enabled = no_rdp.get("enable", 0) == 0 if no_rdp else True
    samba_enabled = no_samba.get("enable", 0) == 0 if no_samba else True
    return {
        "ipv6Enabled": ipv6_enabled,
        "internetEnabled": internet_enabled,
        "rdpEnabled": rdp_enabled,
        "sambaEnabled": samba_enabled,
    }

def main():
    if not FIREBASE_AVAILABLE:
        print("Firebase not available", file=sys.stderr)
        sys.exit(0)
    creds_file = os.environ.get(
        "FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json"
    )
    if not Path(creds_file).exists():
        print("No credentials file", file=sys.stderr)
        sys.exit(0)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(creds_file))
    db = firestore.client()

    vmids = get_vmids()
    for vmid in vmids:
        try:
            rules = get_firewall_rules(vmid)
            firewall = parse_firewall_rules(rules)
            q = db.collection("servers")
            if FieldFilter is not None:
                q = q.where(filter=FieldFilter("nodeId", "==", NODE_NAME))
            else:
                q = q.where("nodeId", "==", NODE_NAME)
            servers = q.stream()
            for ref in servers:
                data = ref.to_dict()
                if data.get("proxmoxId") == vmid:
                    ref.reference.update({"firewall": firewall})
                    break
        except Exception as e:
            print("VM {}: {}".format(vmid, e), file=sys.stderr)

main()
PYEOF
  echo "== Finished =="
}
############################################################

FUNC_CONTENT=$(declare -f remote_task)

for ((i=1; i<=85; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP ($i)"

    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done
