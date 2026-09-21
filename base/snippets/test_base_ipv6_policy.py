import importlib.util
import os
from pathlib import Path
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location('ipv6_policy', Path(__file__).with_name('base_ipv6_policy.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def entry(vmid=100, **kw):
    return {'ipv6': f'2a01:4f9:c01f:e::{vmid:x}', **kw}


def test_default_is_protected_and_public_suffix_is_stable():
    p = m.plan({100: entry()}, '2001:db8:1::2')
    assert p['entries'][0]['public'] == '2001:db8:1::64'
    assert not p['entries'][0]['enabled']
    assert m.plan({100: entry(ipv6Enabled='true')}, '2001:db8:1::2')['entries'][0]['enabled'] is False


@pytest.mark.parametrize('vmid,data', [(2,entry(2)), (100,entry(101)), (100,{'ipv6':'2001:db8:1::64'})])
def test_rejects_node_legacy_or_identity_collisions(vmid,data):
    with pytest.raises(ValueError):
        m.plan({vmid:data}, '2001:db8:1::2')


def test_only_revokes_original_inbound_public_tuples():
    before=m.plan({100:entry(ipv6Enabled=True),101:entry(101,ipv6Enabled=True)},'2001:db8:1::2')
    after=m.plan({100:entry(ipv6Enabled=True,samba=False),101:entry(101,ipv6Enabled=True)},'2001:db8:1::2')
    commands=m.revoked(before,after)
    assert len(commands)==5
    assert all(c[:6]==['conntrack','-D','-f','ipv6','--orig-dst','2001:db8:1::64'] for c in commands)
    assert all('--src' not in c and '--dst' not in c for c in commands)
    assert not m.revoked(after,after)


def test_removed_vm_revokes_only_previously_enabled():
    before=m.plan({100:entry(ipv6Enabled=True),101:entry(101)},'2001:db8:1::2')
    assert m.revoked(before, m.plan({},'2001:db8:1::2'))==[['conntrack','-D','-f','ipv6','--orig-dst','2001:db8:1::64']]


def test_corrupt_receipt_fails_before_writes(tmp_path):
    state=tmp_path/'state.json';state.write_text('BROKEN')
    config=tmp_path/'policy.nft';config.write_text('old')
    with patch.dict(os.environ, {'BASE_IPV6_POLICY_ENABLED':'1','MAIN_IPV6':'2001:db8:1::2','BASE_IPV6_POLICY_FILE':str(config),'BASE_IPV6_POLICY_STATE':str(state)}), patch.object(m,'run') as run:
        with pytest.raises(ValueError):m.reconcile({100:entry()})
    run.assert_not_called()
    assert config.read_text()=='old'


def test_failed_atomic_apply_restores_boot_file(tmp_path):
    from types import SimpleNamespace
    state=tmp_path/'state.json'
    config=tmp_path/'policy.nft';config.write_text('old')
    def fake_run(args,**kw):
        if args[:3]==['nft','list','table']:
            return SimpleNamespace(returncode=1)
        if args==['nft','-c','-f','-']:
            return SimpleNamespace(returncode=0)
        raise RuntimeError('reject')
    with patch.dict(os.environ, {'BASE_IPV6_POLICY_ENABLED':'1','MAIN_IPV6':'2001:db8:1::2','BASE_IPV6_POLICY_FILE':str(config),'BASE_IPV6_POLICY_STATE':str(state)}), patch.object(m,'run',side_effect=fake_run):
        with pytest.raises(RuntimeError):m.reconcile({100:entry()})
    assert config.read_text()=='old'
    assert not state.exists()


def test_revoke_never_changes_the_shared_flowtable_or_guest_egress():
    from types import SimpleNamespace
    command=['conntrack','-D','-f','ipv6','--orig-dst','2001:db8:1::64','-p','tcp','--dport','3389']
    with patch.object(m,'run',return_value=SimpleNamespace(returncode=0)) as run:
        m.revoke([command])
    run.assert_called_once_with(command,allowed=(0,1))


def test_revoke_does_not_treat_permission_failure_as_no_matching_flow():
    from types import SimpleNamespace
    with patch.object(m,'run',return_value=SimpleNamespace(returncode=1,stderr='Operation failed: must be root')):
        with pytest.raises(RuntimeError,match='revocation failed'):
            m.revoke([['conntrack','-D']])
    with patch.object(m,'run',return_value=SimpleNamespace(returncode=1,stderr='conntrack: 0 flow entries have been deleted.')):
        m.revoke([['conntrack','-D']])


def test_live_mapping_without_receipt_cannot_skip_existing_session_revocation(tmp_path):
    from types import SimpleNamespace
    config=tmp_path/'policy.nft';config.write_text('previous')
    with patch.dict(os.environ,{'BASE_IPV6_POLICY_ENABLED':'1','MAIN_IPV6':'2001:db8:1::2',
                               'BASE_IPV6_POLICY_FILE':str(config),'BASE_IPV6_POLICY_STATE':str(tmp_path/'missing')}), \
            patch.object(m,'run',return_value=SimpleNamespace(returncode=0)):
        with pytest.raises(RuntimeError,match='without its receipt'):
            m.reconcile({100:entry()})
    assert config.read_text()=='previous'


def test_disable_keeps_an_inert_boot_file_and_removes_receipt(tmp_path):
    from types import SimpleNamespace
    state=tmp_path/'state.json';state.write_text(__import__('json').dumps(m.plan({100:entry(ipv6Enabled=True)},'2001:db8:1::2')))
    policy=tmp_path/'policy.nft';policy.write_text('old')
    calls=[]
    def fake_run(args,**kw):
        calls.append((args,kw.get('data')))
        if args[:3]==['nft','list','table']:
            return SimpleNamespace(returncode=0,stdout='')
        if args[:2]==['conntrack','-L']:
            return SimpleNamespace(returncode=1,stdout='')
        return SimpleNamespace(returncode=0,stdout='')
    with patch.object(m,'run',side_effect=fake_run):
        m.disable_policy(policy,state)
    assert policy.read_text()==m.DISABLED_POLICY
    assert not state.exists()
    assert ['conntrack','-D','-f','ipv6','--orig-dst','2001:db8:1::64'] in [args for args,_ in calls]
    assert any('ct original ip6 daddr @public6' in (data or '') for _,data in calls)
