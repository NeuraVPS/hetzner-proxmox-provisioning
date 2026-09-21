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
        "ipv4": f"10.64.0.{suffix}",
        "ipv6": f"2a01:4f9:c01f:e::{suffix:x}",
        "firewall": {"sambaEnabled": True},
    }
    data.update(extra)
    return server_id, data


def inputs(mode="audit"):
    servers = dict(
        [
            server("a1", "alice", 1, 1),
            server("a2", "alice", 2, 2),
            server("b1", "bob", 3, 3),
            server("c1", "carol", 4, 4),
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
    assert ("10.64.0.1", "10.64.0.3") in plan.allowed_v4
    assert ("10.64.0.3", "10.64.0.1") in plan.allowed_v4
    assert ("2a01:4f9:c01f:e::1", "2a01:4f9:c01f:e::3") in plan.allowed_v6
    assert ("2a01:4f9:c01f:e::3", "2a01:4f9:c01f:e::1") in plan.allowed_v6


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
    assert "10.64.0.1" in plan.guest_v4
    assert "2a01:4f9:c01f:e::1" in plan.guest_v6
    assert ("a1", "a2") in pairs(plan)


def test_unknown_owner_fails_without_planning_permissions():
    servers, users, config = inputs()
    servers["a1"]["userId"] = "not-in-users"
    with pytest.raises(policy.InventoryValidationError, match="owner"):
        policy.build_policy(servers, users, config)


def test_vmid_reuse_or_duplicate_guest_ip_fails_instead_of_selecting_one_document():
    servers, users, config = inputs()
    servers["reuse"] = dict(servers["a1"], userId="bob", ipv4="10.64.0.99", ipv6="2a01:4f9:c01f:e::99")
    with pytest.raises(policy.InventoryValidationError, match="VMID"):
        policy.build_policy(servers, users, config)


def test_transit_and_missing_input_never_become_guest_addresses():
    servers, users, config = inputs()
    servers["a1"]["ipv6"] = "2a01:4f9:c01f:e:ffff::1"
    with pytest.raises(policy.InventoryValidationError, match="transit"):
        policy.build_policy(servers, users, config)
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
    assert "tcp dport { 135, 139, 445 }" in rendered
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


def test_first_firestore_install_refuses_explicit_enforce_without_an_audit_install(tmp_path):
    servers, users, config = inputs(mode="enforce")
    db = FakeDb(
        [Snapshot(key, value) for key, value in servers.items()],
        [Snapshot(key, value) for key, value in users.items()],
        Snapshot("smbPolicy", config),
    )
    with pytest.raises(policy.PolicyError, match="first SMB policy installation"):
        policy.reconcile_from_firestore(db, lambda _transaction: None, tmp_path / "p.nft", tmp_path / "l.json")
    assert not (tmp_path / "p.nft").exists()


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
    with pytest.raises(policy.PolicyError, match="after enforce"):
        policy.installed_mode_from_runtime(receipt, lambda: False)


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
