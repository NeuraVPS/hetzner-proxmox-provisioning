#!/usr/bin/env python3
"""Real IPv6 TCP probes in private user/network namespaces; no production I/O."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent

def sh(*cmd,data=None,check=True):
    p=subprocess.run(cmd,input=data,capture_output=True,text=True)
    if check and p.returncode:raise RuntimeError(f'{cmd}: {p.stderr} {p.stdout}')
    return p

def main():
    spec=importlib.util.spec_from_file_location('base_ipv6_policy', HERE/'base_ipv6_policy.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    procs=[]
    def ns():
        p=subprocess.Popen(['unshare','-n','sleep','300']);procs.append(p);time.sleep(.15);return str(p.pid)
    internet,guest=ns(),ns()
    def n(pid,*cmd,**kw):return sh('nsenter','-t',pid,'-n',*cmd,**kw)
    try:
        sh('ip','link','set','lo','up')
        for pid in [internet,guest]:n(pid,'ip','link','set','lo','up')
        for interface,peer,pid in [('enp2s0','wan',internet),('tun-fp38','eth0',guest)]:
            sh('ip','link','add',interface,'type','veth','peer','name',peer,'netns',pid)
            sh('ip','link','set',interface,'up');n(pid,'ip','link','set',peer,'up')
        sh('ip','-6','addr','add','2001:db8:1::2/64','dev','enp2s0','nodad')
        n(internet,'ip','-6','addr','add','2001:db8:1::1/64','dev','wan','nodad')
        n(internet,'ip','-6','route','add','2001:db8:1::64/128','via','2001:db8:1::2')
        n(internet,'ip','-6','route','add','2001:db8:1::65/128','via','2001:db8:1::2')
        sh('ip','-6','addr','add','fd00:1::1/64','dev','tun-fp38','nodad')
        n(guest,'ip','-6','addr','add','fd00:1::2/64','dev','eth0','nodad')
        for vm in (100,101):
            n(guest,'ip','-6','addr','add',f'2a01:4f9:c01f:e::{vm:x}/128','dev','eth0','nodad')
            sh('ip','-6','route','add',f'2a01:4f9:c01f:e::{vm:x}/128','via','fd00:1::2','dev','tun-fp38')
        n(guest,'ip','-6','route','add','default','via','fd00:1::1')
        sh('sysctl','-qw','net.ipv6.conf.all.forwarding=1')
        base='''table ip6 nat {
 chain pre { type nat hook prerouting priority -100; policy accept;
  iifname "enp2s0" tcp dport 20101 dnat to [2a01:4f9:c01f:e::65]:3389
 }
 chain post { type nat hook postrouting priority 100; policy accept;
  ip6 saddr 2a01:4f9:c01f:e::/64 oifname "enp2s0" snat prefix to 2001:db8:1::/64
 }
}
table inet filter {
 flowtable ft { hook ingress priority 0; devices = { enp2s0, tun-fp38 }; }
 chain forward { type filter hook forward priority filter; policy drop;
  ct state established,related flow add @ft
  ct state established,related accept
  iifname "enp2s0" ct status dnat accept
  iifname "tun-*" oifname "enp2s0" accept
 }
}
'''
        sh('nft','-f','-',data=base)
        server=r'''import socket,threading
s=socket.socket(socket.AF_INET6);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("::",0));s.close()
def serve(c,addr,port):
 if addr.endswith("::64") and port==3389:
  c.sendall((addr+":"+str(port)).encode()+bytes([10]))
  try:
   while c.recv(100):c.sendall(b"PONG"+bytes([10]))
  finally:c.close()
 else:c.sendall((addr+":"+str(port)).encode());c.close()
def listen(addr,port):
 s=socket.socket(socket.AF_INET6);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((addr,port));s.listen()
 while True:
  c,a=s.accept();threading.Thread(target=serve,args=(c,addr,port),daemon=True).start()
for a,p in [("2a01:4f9:c01f:e::64",3389),("2a01:4f9:c01f:e::64",20101),("2a01:4f9:c01f:e::65",3389)]:threading.Thread(target=listen,args=(a,p),daemon=True).start()
threading.Event().wait()
'''
        p=subprocess.Popen(['nsenter','-t',guest,'-n','python3','-c',server]);procs.append(p);time.sleep(.2)
        egress_server=r'''import socket
s=socket.socket(socket.AF_INET6);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("2001:db8:1::1",4444));s.listen()
while True:
 c,a=s.accept();c.sendall(b"OPEN"+bytes([10]))
 try:
  while c.recv(100):c.sendall(b"PONG"+bytes([10]))
 finally:c.close()
'''
        p=subprocess.Popen(['nsenter','-t',internet,'-n','python3','-c',egress_server]);procs.append(p);time.sleep(.1)
        probe='''import socket,sys
s=socket.socket(socket.AF_INET6);s.settimeout(3)
try:s.connect((sys.argv[1],int(sys.argv[2])));print(s.recv(100).decode())
except (OSError,TimeoutError):print("CLOSED")
'''
        hold='''import socket,sys
s=socket.socket(socket.AF_INET6);s.settimeout(3)
try:
 s.connect((sys.argv[1],int(sys.argv[2])));print(s.recv(100).decode().strip(),flush=True)
 for line in sys.stdin:
  try:s.sendall(line.encode());print(s.recv(100).decode().strip(),flush=True)
  except (OSError,TimeoutError):print("CLOSED",flush=True);break
except (OSError,TimeoutError):print("CLOSED",flush=True)
'''
        hold_out='''import socket,sys
s=socket.socket(socket.AF_INET6);s.settimeout(3);s.bind((sys.argv[1],0))
try:
 s.connect((sys.argv[2],int(sys.argv[3])));print(s.recv(100).decode().strip(),flush=True)
 for line in sys.stdin:
  try:s.sendall(line.encode());print(s.recv(100).decode().strip(),flush=True)
  except (OSError,TimeoutError):print("CLOSED",flush=True);break
except (OSError,TimeoutError):print("CLOSED",flush=True)
'''
        def check(addr,port,expected):
            actual=n(internet,'python3','-c',probe,addr,str(port)).stdout.strip()
            if actual != expected:
                print(sh('nft','list','ruleset').stdout)
                print(n(guest,'ip','-6','route').stdout)
                print(n(guest,'ss','-lnt').stdout)
                print(sh('ip','-6','route').stdout)
            assert actual==expected,(addr,port,actual,expected)
        with tempfile.TemporaryDirectory() as td:
            conf=Path(td)/'policy.nft';receipt=Path(td)/'state.json'
            os.environ.update(BASE_IPV6_POLICY_ENABLED='1',MAIN_IPV6='2001:db8:1::2',BASE_IPV6_POLICY_FILE=str(conf),BASE_IPV6_POLICY_STATE=str(receipt))
            desired={100:{'ipv6':'2a01:4f9:c01f:e::64'},101:{'ipv6':'2a01:4f9:c01f:e::65'}}
            m.reconcile(desired)
            check('2001:db8:1::64',3389,'CLOSED')
            check('2001:db8:1::2',20101,'2a01:4f9:c01f:e::65:3389')
            desired[100]['ipv6Enabled']=True;m.reconcile(desired)
            check('2001:db8:1::64',3389,'2a01:4f9:c01f:e::64:3389')
            check('2001:db8:1::64',20101,'2a01:4f9:c01f:e::64:20101')
            # This connection is eligible for the test flowtable. Revoking
            # its exact original public tuple must stop it, not just new SYNs.
            persistent=subprocess.Popen(['nsenter','-t',internet,'-n','python3','-c',hold,'2001:db8:1::64','3389'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            procs.append(persistent)
            assert persistent.stdout.readline().strip()=='2a01:4f9:c01f:e::64:3389'
            persistent.stdin.write('ping\n');persistent.stdin.flush()
            assert persistent.stdout.readline().strip()=='PONG'
            # VM 101's established guest egress must survive VM 100's direct
            # withdrawal; the revoke guard is limited to original inbound
            # tuples and must not match a NAT66 reply.
            egress=subprocess.Popen(['nsenter','-t',guest,'-n','python3','-c',hold_out,'2a01:4f9:c01f:e::65','2001:db8:1::1','4444'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            procs.append(egress)
            assert egress.stdout.readline().strip()=='OPEN'
            egress.stdin.write('ping\n');egress.stdin.flush()
            assert egress.stdout.readline().strip()=='PONG'
            desired[100]['ipv6Enabled']=False;m.reconcile(desired)
            persistent.stdin.write('ping\n');persistent.stdin.flush()
            actual=persistent.stdout.readline().strip()
            assert actual=='CLOSED',actual
            egress.stdin.write('ping\n');egress.stdin.flush()
            assert egress.stdout.readline().strip()=='PONG'
            check('2001:db8:1::64',3389,'CLOSED')
            check('2001:db8:1::2',20101,'2a01:4f9:c01f:e::65:3389')
            desired[100]['ipv6Enabled']=True;desired[100]['rdp']=False;m.reconcile(desired)
            check('2001:db8:1::64',3389,'CLOSED')
            check('2001:db8:1::64',20101,'2a01:4f9:c01f:e::64:20101')
            # Disable publishes an inert file before it removes the live
            # table. A later cold nftables load cannot resurrect the old
            # receipt; only a fresh sync below can enable it again.
            m.disable_policy(conf,receipt)
            assert conf.read_text()==m.DISABLED_POLICY
            assert not receipt.exists()
            sh('nft','-f','-',data='flush ruleset\n'+base+conf.read_text())
            check('2001:db8:1::64',3389,'CLOSED')
            check('2001:db8:1::64',20101,'CLOSED')
            check('2001:db8:1::2',20101,'2a01:4f9:c01f:e::65:3389')
            desired[100]['rdp']=True;m.reconcile(desired)
            check('2001:db8:1::64',3389,'2a01:4f9:c01f:e::64:3389')
            check('2001:db8:1::64',20101,'2a01:4f9:c01f:e::64:20101')
        print('ALL OK: protected/enabled IPv6, service flags, high-port isolation, VIP preserved, cold disable/re-enable, revocation')
    finally:
        for p in reversed(procs):
            p.kill();p.wait()

if __name__=='__main__':
    if '--inside' not in sys.argv:
        os.execvp('unshare',['unshare','-Urn',sys.executable,__file__,'--inside'])
    main()
