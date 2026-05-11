#!/usr/bin/env bash
# veth-host-setup.sh — create the Jool netns + host↔netns veth pair
# *before* nftables.service loads /etc/nftables.conf.
#
# Why this exists as its own unit (vs. inside jool-nat46-setup.sh):
# the flowtable in `table inet filter` (see netns-jool-nat46-nat66-guide.md
# §7) declares `devices = { enp*, veth-host }`. nftables.service ships
# with `Before=network-pre.target` so it loads very early in boot — much
# earlier than jool-nat46.service, which runs after `network-online.target`.
# If nftables loads before veth-host exists, nft errors with
#   "Could not process rule: No such file or directory"
# and rejects the entire ruleset, leaving the host with no firewall/DNAT.
#
# Trying to reorder nftables to run AFTER jool-nat46 creates a systemd
# ordering cycle (nftables Before=network-pre.target ↔ networking-Before
# graph), which systemd silently breaks at every boot by deleting one of
# the edges — sometimes the very edge we needed.
#
# The clean fix is to do nothing more than what nftables needs (the veth
# pair existing) in a sibling oneshot that runs in the same early-boot
# slot as nftables. Address/route/Jool configuration on this veth pair
# happens later, in jool-nat46.service.
#
# All commands are idempotent so the unit is safe to re-run.

set -euo pipefail

# 1) Jool netns must exist before we move veth-ns into it.
ip netns add jool 2>/dev/null || true

# 2) Veth pair host↔netns. `2>/dev/null || true` makes re-runs a no-op.
ip link add veth-host type veth peer name veth-ns 2>/dev/null || true

# 3) Move veth-ns into the jool netns. No-op if it is already there.
ip link set veth-ns netns jool 2>/dev/null || true

# 4) Bring veth-host up so nftables's flowtable hook can attach at ingress.
ip link set veth-host up
