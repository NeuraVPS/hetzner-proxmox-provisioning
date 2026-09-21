import importlib.util
from pathlib import Path
import sys
from contextlib import contextmanager

import pytest


MODULE = Path(__file__).with_name("base_smb_policy.py")
SPEC = importlib.util.spec_from_file_location("base_smb_policy", MODULE)
policy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


def server(server_id, owner, vmid, suffix, **extra):
    data = {
        "userId": owner,
        "proxmoxId": vmid,
        "ipv4": f"10.64.{vmid // 256}.{vmid % 256}",
        "ipv6": f"2a01:4f9:c01f:e::{vmid:x}",
        "firewall": {"sambaEnabled": True},
    }
    data.update(extra)
    return server_id, data


def inputs(mode="audit"):
    servers = dict(
        [
            server("a1", "alice", 100, 100),
            server("a2", "alice", 101, 101),
            server("b1", "bob", 102, 102),
            server("c1", "carol", 103, 103),
        ]
    )
    users = {
        "alice": {"linkedAccountIds": ["bob"]},
        "bob": {"linkedAccountIds": ["alice", "carol"]},
        "carol": {"linkedAccountIds": ["bob"]},
    }
    return servers, users, {"schemaVersion": 1, "version": 7, "mode": mode, "partnerPairs": []}


def pairs(plan):
    return set(plan.allowed_server_pairs)


def test_same_owner_and_direct_link_are_bidirectional_for_both_families():
    servers, users, config = inputs()
    plan = policy.build_policy(servers, users, config)

    assert pairs(plan) == {("a1", "a2"), ("a1", "b1"), ("a2", "b1"), ("b1", "c1")}
    assert ("10.64.0.100", "10.64.0.102") in plan.allowed_v4
    assert ("10.64.0.102", "10.64.0.100") in plan.allowed_v4
    assert ("2a01:4f9:c01f:e::64", "2a01:4f9:c01f:e::66") in plan.allowed_v6
    assert ("2a01:4f9:c01f:e::66", "2a01:4f9:c01f:e::64") in plan.allowed_v6


def test_plan_is_deterministic_for_unordered_firestore_collections():
    servers, users, config = inputs()
    from_mapping = policy.build_policy(servers, users, config)
    server_docs = [{"id": server_id, **data} for server_id, data in reversed(list(servers.items()))]
    user_docs = [{"uid": uid, **data} for uid, data in reversed(list(users.items()))]
    from_reversed_lists = policy.build_policy(server_docs, user_docs, config)
    assert from_reversed_lists == from_mapping


def test_unlink_is_per_edge_and_not_transitive():
    servers, users, config = inputs()
    users["alice"]["linkedAccountIds"] = []
    users["bob"]["linkedAccountIds"] = ["carol"]
    plan = policy.build_policy(servers, users, config)

    assert ("a1", "b1") not in pairs(plan)
    assert ("a2", "b1") not in pairs(plan)
    assert ("a1", "c1") not in pairs(plan)
    assert ("b1", "c1") in pairs(plan)


def test_linked_account_clique_allows_each_direct_edge_and_unlink_only_removes_that_edge():
    servers = dict([
        server("a1", "alice", 100, 100), server("a2", "alice", 101, 101),
        server("b1", "bob", 102, 102), server("b2", "bob", 103, 103),
        server("c1", "carol", 104, 104), server("c2", "carol", 105, 105),
    ])
    users = {
        "alice": {"linkedAccountIds": ["bob", "carol"]},
        "bob": {"linkedAccountIds": ["alice", "carol"]},
        "carol": {"linkedAccountIds": ["alice", "bob"]},
    }
    config = policy.default_config()
    plan = policy.build_policy(servers, users, config)
    # Three owner-pairs × two VMs × two VMs, represented directionally in v4.
    assert len(plan.allowed_v4) == 30
    users["alice"]["linkedAccountIds"] = ["carol"]
    users["bob"]["linkedAccountIds"] = ["carol"]
    after = policy.build_policy(servers, users, config)
    assert len(after.allowed_v4) == 22
    removed = set(plan.allowed_v4) - set(after.allowed_v4)
    assert len(removed) == 8  # A↔B only: 2×2 in both directions.
    assert ("a1", "c1") in set(after.allowed_server_pairs)
    assert ("b1", "c1") in set(after.allowed_server_pairs)


def test_explicit_partner_is_pinned_to_both_server_and_owner():
    servers, users, config = inputs()
    config["partnerPairs"] = [{
        "serverIdA": "a1", "ownerUidA": "alice", "serverIdB": "c1", "ownerUidB": "carol",
    }]
    plan = policy.build_policy(servers, users, config)
    assert ("a1", "c1") in pairs(plan)

    users["dave"] = {"linkedAccountIds": []}
    servers["c1"]["userId"] = "dave"
    stale = policy.build_policy(servers, users, config)
    assert ("a1", "c1") not in pairs(stale)
    assert stale.warnings == ("partnerPairs[0] skipped: right server/owner binding is stale",)


def test_malformed_partner_config_still_fails_the_whole_reconciliation():
    servers, users, config = inputs()
    config["partnerPairs"] = [{"serverIdA": "a1"}]
    with pytest.raises(policy.ConfigValidationError, match="ownerUidA"):
        policy.build_policy(servers, users, config)


def test_samba_disabled_is_still_a_registered_private_peer_candidate():
    servers, users, config = inputs()
    servers["a1"]["firewall"] = {"sambaEnabled": False}
    plan = policy.build_policy(servers, users, config)
    assert plan.guest_v4 == (policy.GUEST_V4_RANGE,)
    assert plan.guest_v6 == (policy.GUEST_V6_RANGE,)
    assert ("a1", "a2") in pairs(plan)


def test_unknown_owner_is_omitted_without_planning_permissions():
    servers, users, config = inputs()
    servers["a1"]["userId"] = "not-in-users"
    plan = policy.build_policy(servers, users, config)
    assert ("a1", "a2") not in pairs(plan)
    assert "owner is absent" in " ".join(plan.warnings)


def test_vmid_reuse_or_duplicate_guest_ip_fails_instead_of_selecting_one_document():
    servers, users, config = inputs()
    servers["reuse"] = dict(servers["a1"], userId="bob")
    plan = policy.build_policy(servers, users, config)
    assert ("a1", "a2") not in pairs(plan)
    assert "duplicate server documents" in " ".join(plan.warnings)


def test_transit_and_missing_input_never_become_guest_addresses():
    servers, users, config = inputs()
    servers["a1"]["ipv6"] = "2a01:4f9:c01f:e:ffff::1"
    plan = policy.build_policy(servers, users, config)
    assert ("a1", "a2") not in pairs(plan)
    with pytest.raises(policy.InputUnavailable):
        policy.build_policy(None, users, config)


def test_default_mode_is_audit_and_unknown_candidates_are_counted_not_dropped():
    servers, users, config = inputs()
    del config["mode"]
    rendered = policy.render_nft_bootstrap(policy.build_policy(servers, users, config))
    assert "priority -5" in rendered
    assert 'comment "smb-policy unknown candidate"' in rendered
    assert "counter drop" not in rendered
    assert " log " not in rendered


def test_enforce_checks_safe_return_path_without_conntrack_and_keeps_infra_exempt():
    servers, users, config = inputs(mode="enforce")
    rendered = policy.render_nft_bootstrap(policy.build_policy(servers, users, config))
    assert "meta l4proto { tcp, udp } th dport { 135, 137, 138, 139, 445 }" in rendered
    assert "tcp sport { 135, 139, 445 } tcp flags & (syn | ack) != syn" in rendered
    assert "udp sport 137 udp dport 137" in rendered
    assert "udp sport 138 udp dport 138" in rendered
    assert "sport 137 tcp" not in rendered
    assert "ct state new" not in rendered
    assert "counter drop" in rendered
    # Enforcement requires both endpoints to be registered guests.  A BASE
    # SNAT/NAT64 infrastructure source is therefore not caught by this drop.
    assert "ip saddr @smb_guests_v4 ip daddr @smb_guests_v4" in rendered
    assert "ip6 saddr @smb_guests_v6 ip6 daddr @smb_guests_v6" in rendered
    assert "not @smb_guests" not in rendered


def test_candidate_ranges_cover_deleted_and_unprovisioned_vmids_without_allowing_them():
    servers, users, config = inputs(mode="enforce")
    plan = policy.build_policy(servers, users, config)
    rendered = policy.render_nft_bootstrap(plan)
    assert "flags interval" in rendered
    assert policy.GUEST_V4_RANGE in rendered
    assert policy.GUEST_V6_RANGE in rendered
    # A VMID in the live allocation but absent from Firestore is a candidate,
    # never an implicit same-owner/unknown-IP permission.
    assert policy._is_candidate_address("10.64.4.72", 4)
    assert policy._is_candidate_address("2a01:4f9:c01f:e::448", 6)
    assert not any("10.64.4.72" in pair for pair in plan.allowed_v4)
    assert not policy._is_candidate_address("10.64.255.1", 4)
    assert not policy._is_candidate_address("2a01:4f9:c01f:e:ffff::1", 6)


def test_incomplete_or_reused_server_document_removes_its_old_permission():
    servers, users, config = inputs(mode="enforce")
    before = policy.build_policy(servers, users, config)
    del servers["a1"]["proxmoxId"]  # queued/provisioning write, not a trusted peer
    after = policy.build_policy(servers, users, config)
    assert ("a1", "a2") in pairs(before)
    assert ("a1", "a2") not in pairs(after)
    assert ("10.64.0.100", "10.64.0.101") not in after.allowed_v4
    assert "skipped" in " ".join(after.warnings)


def test_last_good_survives_invalid_fresh_config(tmp_path):
    servers, users, config = inputs()
    last_good = tmp_path / "smb-policy-last-good.json"
    applied = []
    policy.reconcile_fresh(servers, users, config, applied.append, last_good, "audit")
    before = last_good.read_text()

    config["mode"] = "block-everything"
    with pytest.raises(policy.ConfigValidationError):
        policy.reconcile_fresh(servers, users, config, applied.append, last_good, "audit")
    assert last_good.read_text() == before
    assert len(applied) == 1

    with pytest.raises(policy.InputUnavailable):
        policy.reconcile_fresh(None, users, policy.default_config(), applied.append, last_good, "audit")
    assert last_good.read_text() == before
    assert len(applied) == 1


def test_last_good_survives_failed_nft_apply(tmp_path):
    servers, users, config = inputs()
    last_good = tmp_path / "smb-policy-last-good.json"
    policy.write_last_good(last_good, policy.build_policy(servers, users, config))
    before = last_good.read_text()

    def broken_apply(_transaction):
        raise RuntimeError("nft rejected transaction")

    with pytest.raises(RuntimeError, match="nft rejected"):
        policy.reconcile_fresh(servers, users, config, broken_apply, last_good, "audit")
    assert last_good.read_text() == before


def test_set_update_never_flushes_ruleset_or_replaces_chains():
    servers, users, config = inputs()
    update = policy.render_nft_set_update(policy.build_policy(servers, users, config))
    assert "flush ruleset" not in update
    assert "flush set inet nvx_smb_policy" in update
    assert "chain" not in update


def test_mode_change_replaces_only_its_own_chain_in_the_same_transaction():
    servers, users, config = inputs(mode="enforce")
    update = policy.render_nft_update(policy.build_policy(servers, users, config), "audit")
    assert "flush ruleset" not in update
    assert "delete chain inet nvx_smb_policy forward" in update
    assert "add chain inet nvx_smb_policy forward" in update
    assert "counter drop" in update


class Snapshot:
    def __init__(self, identifier, data=None, exists=True):
        self.id = identifier
        self._data = data
        self.exists = exists

    def to_dict(self):
        return self._data


class Collection:
    def __init__(self, snapshots=None, document=None):
        self.snapshots = snapshots or []
        self._document = document
        self.selected = None

    def select(self, fields):
        self.selected = tuple(fields)
        return self

    def stream(self):
        return iter(self.snapshots)

    def document(self, _identifier):
        return Document(self._document)


class Document:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self):
        return self.snapshot


class FakeDb:
    def __init__(self, servers, users, config):
        self.server_collection = Collection(servers)
        self.user_collection = Collection(users)
        self.config_collection = Collection(document=config)

    def collection(self, name):
        return {
            "servers": self.server_collection,
            "users": self.user_collection,
            "config": self.config_collection,
        }[name]


def test_reconcile_from_firestore_projects_only_needed_fields_and_missing_config_bootstraps_audit(tmp_path):
    servers, users, _config = inputs()
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", exists=False),
    )
    applied = []
    plan = policy.reconcile_from_firestore(
        db, applied.append, tmp_path / "policy.nft", tmp_path / "last-good.json"
    )
    assert plan.mode == "audit"
    assert "counter drop" not in applied[0]
    assert db.server_collection.selected == policy.SERVER_PROJECTION
    assert db.user_collection.selected == policy.USER_PROJECTION
    assert "table inet nvx_smb_policy" in (tmp_path / "policy.nft").read_text()
    assert (tmp_path / "last-good.json").exists()


def test_cold_enforce_bootstrap_installs_reviewed_enforcement(monkeypatch, tmp_path):
    servers, users, config = inputs(mode="enforce")
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", config),
    )
    monkeypatch.setattr(policy, "revoke_removed_smb_conntracks", lambda *_a, **_k: None)
    plan = policy.reconcile_from_firestore(db, lambda _transaction: None, tmp_path / "p.nft", tmp_path / "l.json")
    assert plan.mode == "enforce"
    assert 'counter drop' in (tmp_path / "p.nft").read_text()
    assert policy._receipt_plan(tmp_path / "l.json").mode == "enforce"


def test_rejected_nft_restores_previous_boot_include_and_does_not_write_receipt(tmp_path):
    servers, users, config = inputs(mode="enforce")
    include = tmp_path / "p.nft"
    include.write_text("previous include\n")
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", config),
    )
    with pytest.raises(RuntimeError, match="rejected"):
        policy.reconcile_from_firestore(
            db, lambda _transaction: (_ for _ in ()).throw(RuntimeError("rejected")), include, tmp_path / "l.json"
        )
    assert include.read_text() == "previous include\n"
    assert not (tmp_path / "l.json").exists()


def test_installed_mode_readiness_requires_a_consistent_table_and_receipt(tmp_path):
    servers, users, config = inputs()
    receipt = tmp_path / "last-good.json"
    audit = policy.build_policy(servers, users, config)
    policy.write_last_good(receipt, audit)

    # A reboot can lose the table while keeping an audit receipt: bootstrap is
    # allowed, but the mode is never inferred from a caller flag.
    assert policy.installed_mode_from_runtime(receipt, lambda: False) is None
    assert policy.installed_mode_from_runtime(receipt, lambda: True) == "audit"
    with pytest.raises(policy.PolicyError, match="missing"):
        policy.installed_mode_from_runtime(tmp_path / "absent.json", lambda: True)

    enforce = policy.build_policy(servers, users, {**config, "mode": "enforce"})
    policy.write_last_good(receipt, enforce)
    assert policy.installed_mode_from_runtime(receipt, lambda: False) is None


def test_missing_config_after_enforce_retains_policy_instead_of_defaulting_audit(tmp_path):
    servers, users, config = inputs(mode="enforce")
    receipt = tmp_path / "last-good.json"
    policy.write_last_good(receipt, policy.build_policy(servers, users, config))
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", exists=False),
    )
    with pytest.raises(policy.InputUnavailable, match="disappeared after enforcement"):
        policy.reconcile_from_firestore(db, lambda _transaction: None, tmp_path / "p.nft", receipt, "enforce")


def test_required_sync_rejects_missing_config_even_before_first_enforce(tmp_path):
    servers, users, _config = inputs()
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", exists=False),
    )
    with pytest.raises(policy.InputUnavailable, match="disappeared after enforcement"):
        policy.reconcile_from_firestore(
            db, lambda _transaction: None, tmp_path / "p.nft", tmp_path / "receipt.json", required=True,
        )


def test_conntrack_revocation_is_tuple_scoped_and_preserves_allowed_pair(monkeypatch):
    servers, users, config = inputs(mode="enforce")
    before = policy.build_policy(servers, users, config)
    users["alice"]["linkedAccountIds"] = []
    users["bob"]["linkedAccountIds"] = ["carol"]
    after = policy.build_policy(servers, users, config)
    listing = "\n".join((
        "ipv4 2 tcp 6 431999 ESTABLISHED src=10.64.0.100 dst=10.64.0.102 sport=50100 dport=445 src=10.64.0.102 dst=10.64.0.100 sport=445 dport=50100 [OFFLOAD]",
        "ipv4 2 tcp 6 431999 ESTABLISHED src=10.64.0.102 dst=10.64.0.103 sport=50101 dport=445 src=10.64.0.103 dst=10.64.0.102 sport=445 dport=50101 [OFFLOAD]",
        "ipv4 2 tcp 6 431999 ESTABLISHED src=10.64.0.100 dst=10.64.4.72 sport=50102 dport=445 src=10.64.4.72 dst=10.64.0.100 sport=445 dport=50102 [OFFLOAD]",
    ))
    from types import SimpleNamespace
    commands = []
    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["conntrack", "-L", "-f"]:
            return SimpleNamespace(returncode=0, stdout=listing if command[3] == "ipv4" else "", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(policy.subprocess, "run", fake_run)
    policy.revoke_removed_smb_conntracks(before, after)
    deletes = [command for command in commands if command[:2] == ["conntrack", "-D"]]
    assert len(deletes) == 2
    assert all("--orig-src" in command and "--orig-port-dst" in command for command in deletes)
    assert not any("10.64.0.103" in command for command in deletes)


def test_conntrack_exit_one_is_only_accepted_for_a_race(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(policy.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="permission denied"))
    with pytest.raises(RuntimeError, match="revocation failed"):
        policy._conntrack_run(["conntrack", "-D"])
    monkeypatch.setattr(policy.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="0 flow entries have been deleted"))
    policy._conntrack_run(["conntrack", "-D"])


def test_cold_enforce_force_sweeps_unknown_offloaded_candidate(monkeypatch, tmp_path):
    servers, users, config = inputs(mode="enforce")
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", config),
    )
    from types import SimpleNamespace
    commands = []
    listing = "ipv4 2 tcp 6 431999 ESTABLISHED src=10.64.4.72 dst=10.64.0.100 sport=50102 dport=445 src=10.64.0.100 dst=10.64.4.72 sport=445 dport=50102 [OFFLOAD]"
    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["conntrack", "-L"]:
            return SimpleNamespace(returncode=0, stdout=listing if command[3] == "ipv4" else "", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(policy.subprocess, "run", fake_run)
    policy.reconcile_from_firestore(db, lambda _transaction: None, tmp_path / "p.nft", tmp_path / "receipt.json")
    delete = next(command for command in commands if command[:2] == ["conntrack", "-D"])
    assert delete == [
        "conntrack", "-D", "-f", "ipv4", "--orig-src", "10.64.4.72", "--orig-dst", "10.64.0.100",
        "-p", "tcp", "--orig-port-src", "50102", "--orig-port-dst", "445",
    ]


def test_compatibility_mode_is_an_assertion_not_an_installed_state():
    assert policy._checked_compatibility_mode("audit", "audit") == "audit"
    with pytest.raises(policy.PolicyError, match="disagrees"):
        policy._checked_compatibility_mode("enforce", "audit")
    with pytest.raises(policy.PolicyError, match="cannot assume"):
        policy._checked_compatibility_mode("audit", None)


def test_cli_holds_shared_lock_and_uses_detected_mode(monkeypatch, tmp_path):
    servers, users, config = inputs()
    expected = policy.build_policy(servers, users, config)
    events = []

    @contextmanager
    def fake_lock(path):
        events.append(("lock", str(path)))
        yield
        events.append(("unlock", str(path)))

    def fake_reconcile(_db, _apply, _include, _receipt, installed_mode):
        events.append(("reconcile", installed_mode))
        return expected

    monkeypatch.setattr(policy, "shared_sync_lock", fake_lock)
    monkeypatch.setattr(policy, "installed_mode_from_runtime", lambda _receipt: "audit")
    monkeypatch.setattr(policy, "_runtime_firestore_db", lambda: object())
    monkeypatch.setattr(policy, "_nft_apply", lambda _transaction: None)
    monkeypatch.setattr(policy, "reconcile_from_firestore", fake_reconcile)

    assert policy.main(["sync-policy", "--lock-path", str(tmp_path / ".sync.lock")]) == 0
    assert events == [
        ("lock", str(tmp_path / ".sync.lock")),
        ("reconcile", "audit"),
        ("unlock", str(tmp_path / ".sync.lock")),
    ]


def test_cli_rejects_a_mismatched_compatibility_flag_before_firestore(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(policy, "installed_mode_from_runtime", lambda _receipt: "audit")
    monkeypatch.setattr(policy, "_runtime_firestore_db", lambda: called.append(True))
    assert policy.main([
        "sync-policy", "--lock-path", str(tmp_path / ".sync.lock"), "--installed-mode", "enforce",
    ]) == 1
    assert called == []
