"""Pure tests of the base side of the per-VM outbound IPv4 pools (no nft, no root).

The end-to-end behaviour with real packets lives in test-egress-pools-netns.py.
"""
import importlib.util
import json
import logging
from pathlib import Path

import pytest

MAIN = "95.216.102.179"
OTHER = "116.202.118.221"


@pytest.fixture
def sbn(tmp_path, monkeypatch):
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: False if str(p) == "/var/log/sync-base-nat.log" else original_exists(p))
    monkeypatch.setattr(logging, "FileHandler", lambda *a, **kw: logging.NullHandler())
    spec = importlib.util.spec_from_file_location("sbn_egress_test", Path(__file__).with_name("sync-base-nat.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE_DIR = tmp_path
    mod.STATE_FILE = tmp_path / "state.json"
    mod.EGRESS_CONFIG_CACHE = tmp_path / "egress-pools.json"
    mod.EGRESS_POOLS_FORCE_OFF = False
    return mod


def cfg(**over):
    c = {"enabled": True, "fleetWide": True, "canaryVmids": [],
         "noticeCompletedAt": "2026-01-01T00:00:00Z", "fleetNotBefore": "2026-01-09T00:00:00Z",
         "pools": {"hel": {"cidrs": ["198.51.100.0/26"], "kind": "failover", "activeServerIp": MAIN},
                   "fsn": {"cidrs": ["203.0.113.64/26"], "kind": "failover", "activeServerIp": OTHER}}}
    c.update(over)
    return c


def desired(sbn, **pairs):
    out = {}
    for vmid, (hel, fsn) in pairs.items():
        v = int(vmid[2:])
        out[v] = sbn._server_entry(f"2a01:4f9:c01f:e::{v:x}", node_id="0000238-AX162-2-LTD",
                                   ipv4=f"10.64.{v // 256}.{v % 256}", egress_hel=hel, egress_fsn=fsn)
    return out


def test_state_roundtrip_keeps_the_pair(sbn):
    d = desired(sbn, vm1096=("198.51.100.5", "203.0.113.69"))
    sbn.write_state(sbn._state_payload(d))
    back = sbn.desired_from_state()
    assert back[1096]["egressHel"] == "198.51.100.5" and back[1096]["egressFsn"] == "203.0.113.69"
    old = json.loads(sbn.STATE_FILE.read_text())
    for e in old.values():
        e.pop("egressHel"), e.pop("egressFsn")
    sbn.STATE_FILE.write_text(json.dumps(old))
    assert sbn.desired_from_state()[1096]["egressHel"] == "", "old state files still load"


def test_egress_pair_parsing(sbn):
    assert sbn._egress_pair({"egressIpv4": {"hel": " 198.51.100.5 ", "fsn": "203.0.113.69"}}) == ("198.51.100.5", "203.0.113.69")
    assert sbn._egress_pair({"egressIpv4": "x"}) == ("", "")
    assert sbn._egress_pair({}) == ("", "")


def test_plan_only_owned_blocks_and_flags(sbn):
    d = desired(sbn, vm1096=("198.51.100.5", "203.0.113.69"), vm1097=("198.51.100.6", "203.0.113.70"))
    plan, _ = sbn.egress_plan(d, cfg(), MAIN)
    assert plan == {"hel": {"10.64.4.72": "198.51.100.5", "10.64.4.73": "198.51.100.6"}, "fsn": {}}
    plan, _ = sbn.egress_plan(d, cfg(fleetWide=False, canaryVmids=[1097]), MAIN)
    assert plan["hel"] == {"10.64.4.73": "198.51.100.6"}
    plan, notes = sbn.egress_plan(d, cfg(), "192.0.2.1")
    assert plan == {"hel": {}, "fsn": {}} and "ninguno" in notes[0]
    plan, _ = sbn.egress_plan(d, cfg(enabled=False), MAIN)
    assert plan == {"hel": {}, "fsn": {}}
    plan, _ = sbn.egress_plan(d, None, MAIN)
    assert plan == {"hel": {}, "fsn": {}}
    plan, notes = sbn.egress_plan(d, cfg(), "not-an-ip")
    assert plan == {"hel": {}, "fsn": {}}


@pytest.mark.parametrize("mutate", [
    lambda c: c["pools"]["hel"].update(cidrs=["10.0.0.0/26"]),
    lambda c: c["pools"]["hel"].update(cidrs=["198.51.100.1/26"]),
    lambda c: c["pools"]["hel"].update(kind="x"),
    lambda c: c["pools"].pop("fsn"),
    lambda c: c["pools"]["fsn"].update(cidrs=["198.51.100.0/27"]),
    lambda c: c.update(canaryVmids=["a"]),
    lambda c: c["pools"]["hel"].update(cidrs=["198.51.0.0/16"]),
])
def test_invalid_config_turns_everything_off(sbn, mutate):
    c = cfg()
    mutate(c)
    d = desired(sbn, vm1096=("198.51.100.5", "203.0.113.69"))
    assert sbn.egress_plan(d, c, MAIN)[0] == {"hel": {}, "fsn": {}}


def test_masked_excluded_foreign_and_non_vm_addresses_are_skipped(sbn):
    c = cfg()
    c["pools"]["hel"]["exclude"] = ["198.51.100.9"]
    d = desired(sbn, vm1096=("198.51.100.0", ""), vm1097=("198.51.100.63", ""),
                vm1098=("198.51.100.9", ""), vm1099=("203.0.113.69", ""), vm1100=("198.51.100.10", ""))
    d[1100]["ipv4"] = "10.0.0.5"          # old-model guest: not a 10.64 address
    plan, notes = sbn.egress_plan(d, c, MAIN)
    assert plan["hel"] == {}
    assert any("fuera de su bloque" in n for n in notes)
    c["pools"]["hel"]["useNetworkAndBroadcast"] = True
    assert set(sbn.egress_plan(d, c, MAIN)[0]["hel"].values()) == {"198.51.100.0", "198.51.100.63"}


def test_duplicate_private_address_keeps_first(sbn):
    d = desired(sbn, vm1096=("198.51.100.5", ""), vm1097=("198.51.100.6", ""))
    d[1097]["ipv4"] = d[1096]["ipv4"]
    plan, notes = sbn.egress_plan(d, cfg(), MAIN)
    assert plan["hel"] == {"10.64.4.72": "198.51.100.5"}
    assert any("repetida" in n for n in notes)


def test_force_off(sbn):
    sbn.EGRESS_POOLS_FORCE_OFF = True
    d = desired(sbn, vm1096=("198.51.100.5", ""))
    assert sbn.egress_plan(d, cfg(), MAIN)[0] == {"hel": {}, "fsn": {}}


def test_fleet_never_bypasses_notice_window_but_canaries_work(sbn):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    d = desired(sbn, vm1096=("198.51.100.5", "203.0.113.69"), vm1097=("198.51.100.6", "203.0.113.70"))
    for patch in ({"noticeCompletedAt": None}, {"fleetNotBefore": "invalid"},
                  {"noticeCompletedAt": now.isoformat(), "fleetNotBefore": now.isoformat()},
                  {"noticeCompletedAt": (now-timedelta(days=8)).isoformat(), "fleetNotBefore": (now+timedelta(days=1)).isoformat()}):
        plan, notes = sbn.egress_plan(d, cfg(canaryVmids=[1096], **patch), MAIN)
        assert plan["hel"] == {"10.64.4.72": "198.51.100.5"}
        assert any("7 dias" in n for n in notes)


def test_immediate_operator_approval_accepts_cached_iso_timestamp(sbn):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 19, 15, 24, 36, tzinfo=timezone.utc)
    approval = {
        "mode": "immediate_operator",
        "approvedAt": (now - timedelta(seconds=1)).isoformat(),
        "reason": "F6 validated; operator authorized immediate rollout.",
    }
    raw = cfg(noticeCompletedAt=None, fleetNotBefore=None, fleetActivationApproval=approval)
    assert sbn._egress_fleet_ready(raw, now=now)
    assert not sbn._egress_fleet_ready(dict(raw, fleetActivationApproval=None), now=now)


@pytest.mark.parametrize("approval", [
    None,
    {},
    {"mode": "immediate_operator", "approvedAt": "2026-09-19T15:24:35", "reason": "approved"},
    {"mode": "immediate_operator", "approvedAt": "2026-09-19T15:24:35Z", "reason": "  "},
    {"mode": "immediate_operator", "approvedAt": "2026-09-19T15:24:37Z", "reason": "approved"},
    {"mode": "noticed", "approvedAt": "2026-09-19T15:24:35Z", "reason": "approved"},
])
def test_invalid_immediate_approval_does_not_bypass_notice_gate(sbn, approval):
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 15, 24, 36, tzinfo=timezone.utc)
    raw = cfg(noticeCompletedAt=None, fleetNotBefore=None, fleetActivationApproval=approval)
    assert not sbn._egress_fleet_ready(raw, now=now)


def test_notice_gate_still_requires_seven_full_days(sbn):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 19, 15, 24, 36, tzinfo=timezone.utc)
    assert sbn._egress_fleet_ready(cfg(noticeCompletedAt=(now - timedelta(days=7)).isoformat(),
                                       fleetNotBefore=now.isoformat()), now=now)
    assert not sbn._egress_fleet_ready(cfg(noticeCompletedAt=(now - timedelta(days=7) + timedelta(seconds=1)).isoformat(),
                                           fleetNotBefore=now.isoformat()), now=now)


def test_config_cache_used_when_firestore_is_down(sbn):
    sbn.ensure_firebase = lambda: False
    assert sbn.load_egress_config() is None
    sbn.EGRESS_CONFIG_CACHE.write_text(json.dumps({"config": cfg()}))
    assert sbn.load_egress_config()["enabled"] is True


def test_reconcile_skips_without_structure(sbn, monkeypatch):
    calls = []
    monkeypatch.setattr(sbn, "_egress_maps_exist", lambda: False)
    monkeypatch.setattr(sbn, "_apply_nft_transaction", calls.append)
    sbn.reconcile_egress_pools(desired(sbn, vm1096=("198.51.100.5", "")), cfg())
    assert calls == []


def test_reconcile_is_one_transaction_and_persists(sbn, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sbn, "_egress_maps_exist", lambda: True)
    monkeypatch.setattr(sbn, "_apply_nft_transaction", calls.append)
    sbn.NFT_EGRESS_FILE = tmp_path / "base-nat-egress-pools.nft"
    sbn.MAIN_IPV4 = MAIN
    sbn.reconcile_egress_pools(desired(sbn, vm1096=("198.51.100.5", "203.0.113.69")), cfg())
    assert len(calls) == 1
    tx = calls[0]
    assert tx[:2] == ["flush map ip nat egress_hel4", "flush map ip nat egress_fsn4"]
    assert tx[2:] == ["add element ip nat egress_hel4 { 10.64.4.72 : 198.51.100.5 }"]
    assert sbn.NFT_EGRESS_FILE.read_text().splitlines()[1:] == tx[2:]
