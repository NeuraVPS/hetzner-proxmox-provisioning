#!/usr/bin/env python3
"""Persist BASE IPv6 policy wiring, without reloading nftables or touching VMs.

Install base_ipv6_policy.py beside sync-base-nat.py first. --apply only wires
an initially inert include and the sync flag. Then run sync-base-nat.py sync
and verify both BASES before deploying the Cloud Function firewall trigger.
"""
import argparse
import fcntl
from pathlib import Path
import subprocess
from base_ipv6_policy import atomic_write, disable_policy


def apply(args):
    conf=Path(args.config);env=Path(args.env)
    src=conf.read_text();environment=env.read_text()
    policy=Path(args.policy);state=Path(args.state)
    include=f'include "{policy}"'
    count=src.splitlines().count(include)
    if count>1:raise SystemExit('Duplicate managed include; aborting')
    target='\n'.join(line for line in environment.splitlines() if not line.startswith('BASE_IPV6_POLICY_ENABLED='))+'\n'
    target+='BASE_IPV6_POLICY_ENABLED='+('0' if args.disable else '1')+'\n'
    updated=src.replace(include+'\n','') if args.disable else (src if count else src.rstrip()+'\n\n'+include+'\n')
    print(('Disable' if args.disable else 'Enable')+' direct IPv6 BASE policy; no global reload, no VM changes')
    if not args.apply:return
    if not args.disable and not policy.exists():atomic_write(policy,'# Empty until first successful sync\n')
    if args.disable:
        # Validate the candidate config before changing live policy.  The
        # helper first replaces the include file with an inert one, making a
        # crash at any later step safe for a cold nftables load.
        checked=subprocess.run(['nft','-c','-f','-'],input=updated,capture_output=True,text=True)
        if checked.returncode:raise SystemExit(checked.stderr)
        disable_policy(policy,state)
    for path,body in [(conf,updated),(env,target)]:
        backup=path.with_name(path.name+'.before-ipv6-policy')
        if not backup.exists():atomic_write(backup,path.read_text())
        atomic_write(path,body)
    if not args.disable:
        checked=subprocess.run(['nft','-c','-f',str(conf)],capture_output=True,text=True)
        if checked.returncode:
            atomic_write(conf,src);atomic_write(env,environment)
            raise SystemExit(checked.stderr)
        print('Run sync-base-nat.py sync; verify direct IPv6, VIP access, egress and persisted maps.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--disable',action='store_true',help='Remove only the direct IPv6 table/include; restores pre-feature behavior')
    parser.add_argument('--config',default='/etc/nftables.conf')
    parser.add_argument('--env',default='/etc/default/base-nat')
    parser.add_argument('--policy',default='/etc/nftables.d/base-ipv6-policy.nft')
    parser.add_argument('--state',default='/var/lib/base-nat/ipv6-policy.json')
    parser.add_argument('--lock',default='/var/lib/base-nat/.sync.lock')
    args=parser.parse_args()
    lock=Path(args.lock)
    lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        apply(args)

if __name__=='__main__':main()
