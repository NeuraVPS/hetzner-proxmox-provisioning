"""Gracia entre regiones: arrastre de marcas (02/10/2026).

Prueba el parser de `conntrack -L` (lado BASE) y las órdenes nft/ip que genera
el script de nodo, con `nft` e `ip` simulados que registran lo que se les pide."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = (Path(__file__).resolve().parents[1] / 'scripts/migrate_vm.sh').read_text()
PARSER = re.search(r'(?ms)^_grace_flows_from_conntrack\(\) \{.*?^\}', SCRIPT).group()
NODE_SH = re.search(r"(?ms)^read -r -d '' _GRACE_NODE_SH <<'SH' \|\| true\n(.*?)^SH$", SCRIPT).group(1)
VM = '10.64.2.237'

CONNTRACK = textwrap.dedent(f'''\
    tcp      6 431999 ESTABLISHED src={VM} dst=185.97.161.227 sport=50110 dport=1950 src=185.97.161.227 dst={VM} sport=1950 dport=50110 [ASSURED] mark=5133825 use=1
    tcp      6 431999 ESTABLISHED src={VM} dst=185.97.161.228 sport=50111 dport=1952 src=185.97.161.228 dst={VM} sport=1952 dport=50111 [ASSURED] mark=0x4e5602 use=1
    tcp      6 431999 ESTABLISHED src={VM} dst=1.1.1.1 sport=50112 dport=443 src=1.1.1.1 dst={VM} sport=443 dport=50112 [ASSURED] mark=0 use=1
    tcp      6 117 TIME_WAIT src={VM} dst=8.8.8.8 sport=50113 dport=443 src=8.8.8.8 dst={VM} sport=443 dport=50113 [ASSURED] mark=5133825 use=1
    tcp      6 431999 ESTABLISHED src=95.216.102.179 dst={VM} sport=40000 dport=3389 src={VM} dst=95.216.102.179 sport=3389 dport=40000 [ASSURED] mark=5133825 use=1
    udp      17 29 src={VM} dst=9.9.9.9 sport=5353 dport=53 src=9.9.9.9 dst={VM} sport=53 dport=5353 mark=5133825 use=1
    tcp      6 431999 ESTABLISHED src={VM} dst=185.97.161.227 sport=50110 dport=1950 src=185.97.161.227 dst={VM} sport=1950 dport=50110 [ASSURED] mark=5133825 use=1
    conntrack v1.4.8 (conntrack-tools): 7 flow entries have been shown.
''')

FAKE_NFT = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ['STATE']
args = sys.argv[1:]
line = ' '.join(args)
if args[:2] == ['-f', '-']:
    line += ' <<' + sys.stdin.read().replace(chr(10), ' | ')
open(os.path.join(st, 'nft.log'), 'a').write(line + '\n')
if args[:3] == ['list', 'table', 'inet']:
    sys.exit(0 if os.environ.get('TABLE') == '1' else 1)
if args[:3] == ['list', 'set', 'inet'] and args[4] == 'homeflows':
    sys.exit(0 if os.environ.get('HOMESET') == '1' else 1)
if args[:4] == ['-j', 'list', 'set', 'inet']:
    print(json.dumps(json.load(open(os.path.join(st, 'sets.json'))).get(args[5], {"nftables": []})))
sys.exit(0)
'''
FAKE_IP = r'''#!/bin/sh
echo "$*" >> "$STATE/ip.log"
case "$*" in "link show tun-"*) [ -n "$NOTUN" ] && [ "$3" = "$NOTUN" ] && exit 1 ;; esac
# Reglas ya presentes, en el formato real de iproute2 6.15 (el iif va entre fwmark y lookup).
case "$*" in "rule show") [ -f "$STATE/rules" ] && cat "$STATE/rules" ;; esac
case "$*" in "rule add "*) for m in 0x4e5601 0x4e5602; do
    case "$*" in *"fwmark $m"*) grep -q "fwmark $m" "$STATE/rules" 2>/dev/null && { echo "RTNETLINK answers: File exists" >&2; exit 2; } ;; esac
  done ;; esac
exit 0
'''


def run_node(args, *, table='0', homeset='0', sets=None, notun='', rules=''):
    with tempfile.TemporaryDirectory() as t:
        p = Path(t)
        if rules:
            (p / 'rules').write_text(rules)
        for name, body in (('nft', FAKE_NFT), ('ip', FAKE_IP)):
            (p / name).write_text(body); (p / name).chmod(0o755)
        (p / 'sets.json').write_text(json.dumps(sets or {}))
        env = dict(os.environ, PATH=f'{t}:{os.environ["PATH"]}', STATE=t, TABLE=table, HOMESET=homeset, NOTUN=notun)
        r = subprocess.run(['bash', '-s', '--', *args], input=NODE_SH, capture_output=True, text=True, env=env, timeout=20)
        nft = (p / 'nft.log').read_text().splitlines() if (p / 'nft.log').exists() else []
        ip = (p / 'ip.log').read_text().splitlines() if (p / 'ip.log').exists() else []
        return r, nft, ip


def adds(nft):
    return [l for l in nft if l.startswith('add element')]


class ParserTests(unittest.TestCase):
    def parse(self, text, ip=VM):
        r = subprocess.run(['bash', '-c', PARSER + '\n_grace_flows_from_conntrack "$1"', '_', ip],
                           input=text, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_only_marked_outbound_tcp_live_flows_decimal_or_hex(self):
        self.assertEqual(self.parse(CONNTRACK),
                         'hel:185.97.161.227:1950:50110,fsn:185.97.161.228:1952:50111')

    def test_nothing_marked_gives_empty(self):
        self.assertEqual(self.parse(CONNTRACK.replace('mark=5133825', 'mark=0').replace('mark=0x4e5602', 'mark=0')), '')

    def test_other_vm_ip_is_ignored(self):
        self.assertEqual(self.parse(CONNTRACK, ip='10.64.2.238'), '')


class NodeScriptTests(unittest.TestCase):
    FLOWS = 'hel:185.97.161.227:1950:50110,fsn:185.97.161.228:1952:50111'

    def test_x2_fsn_to_hel_fresh_node(self):
        # Vuelta 248 (FSN) → 240 (HEL): lo nacido en HEL va a casa (sin marca),
        # lo nacido en FSN sigue por tun-fsn, y lo demás a mitad → FSN (origen).
        r, nft, ip = run_node(['on', VM, '86400', 'tun-fsn', 'tun-hel', self.FLOWS])
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('GRACE_ON ip=10.64.2.237 generica=fsn home=1 hel=0 fsn=1 sin_tunel=0 destino=hel', r.stdout)
        creates = [l for l in nft if l.startswith('-f -')]
        self.assertEqual(len(creates), 1)
        body = creates[0]
        # orden: home, flujo-hel, flujo-fsn y después las genéricas
        order = [body.index(k) for k in ('nvx-home', 'nvx-flow-hel', 'nvx-flow-fsn', 'nvx-gen-hel', 'nvx-gen-fsn')]
        self.assertEqual(order, sorted(order))
        a = adds(nft)
        self.assertIn('add element inet nvxgrace fsn4 { 10.64.2.237 timeout 86400s }', a)
        self.assertIn('add element inet nvxgrace homeflows { 10.64.2.237 . 185.97.161.227 . 1950 . 50110 timeout 86400s }', a)
        self.assertIn('add element inet nvxgrace fsnflows { 10.64.2.237 . 185.97.161.228 . 1952 . 50111 timeout 86400s }', a)
        self.assertFalse([l for l in a if 'helflows' in l or ' hel4 ' in l])
        self.assertIn('route replace default dev tun-fsn table 112', ip)
        self.assertFalse([l for l in ip if 'table 111' in l])  # HEL es casa: tabla 101

    def test_same_region_hop_carries_flows_without_generic(self):
        # Tras X1, un salto FSN→FSN: los nacidos en HEL siguen por tun-hel.
        r, nft, ip = run_node(['on', VM, '86400', '', 'tun-fsn', 'hel:185.97.161.227:1950:50110'])
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('generica=- home=0 hel=1 fsn=0', r.stdout)
        a = adds(nft)
        self.assertEqual(a, ['add element inet nvxgrace helflows { 10.64.2.237 . 185.97.161.227 . 1950 . 50110 timeout 86400s }'])
        self.assertIn('route replace default dev tun-hel table 111', ip)

    def test_table_from_01_10_is_extended_in_order(self):
        r, nft, ip = run_node(['on', VM, '60', 'tun-hel', 'tun-fsn', ''], table='1', homeset='0')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse([l for l in nft if l.startswith('-f -')])
        self.assertEqual(len([l for l in nft if l.startswith('add set inet nvxgrace')]), 3)
        ins = [l for l in nft if l.startswith('insert rule')]
        self.assertEqual([re.search(r'@(\w+)', l).group(1) for l in ins], ['fsnflows', 'helflows', 'homeflows'])
        self.assertIn('add element inet nvxgrace hel4 { 10.64.2.237 timeout 60s }', adds(nft))

    def test_existing_v2_table_untouched(self):
        r, nft, _ = run_node(['on', VM, '60', 'tun-hel', 'tun-fsn', ''], table='1', homeset='1')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse([l for l in nft if l.startswith(('-f -', 'add set', 'insert rule'))])

    def test_missing_tunnel_skips_those_flows(self):
        r, nft, _ = run_node(['on', VM, '60', '', 'tun-hel', 'fsn:1.2.3.4:443:5000'], notun='tun-fsn')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('fsn=1 sin_tunel=1', r.stdout)
        self.assertFalse([l for l in adds(nft) if 'fsnflows' in l])

    def test_bad_input_is_dropped(self):
        r, nft, _ = run_node(['on', VM, '60', '', 'tun-hel', 'fsn:1.2.3.4;rm:443:5000,fsn:5.6.7.8:44x:1,fsn:5.6.7.8:443:1'])
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual([l for l in adds(nft) if 'fsnflows' in l],
                         ['add element inet nvxgrace fsnflows { 10.64.2.237 . 5.6.7.8 . 443 . 1 timeout 60s }'])

    def test_clear_removes_only_this_vm(self):
        sets = {'helflows': {'nftables': [{'metainfo': {}}, {'set': {'name': 'helflows', 'elem': [
            {'elem': {'val': {'concat': [VM, '1.1.1.1', 443, 5000]}, 'timeout': 86400, 'expires': 100}},
            {'elem': {'val': {'concat': ['10.64.2.238', '1.1.1.1', 443, 5000]}, 'timeout': 86400}},
            {'concat': [VM, '2.2.2.2', 1950, 6000]}]}}]}}
        r, nft, _ = run_node(['clear', VM], table='1', homeset='1', sets=sets)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        dels = [l for l in nft if l.startswith('delete element')]
        self.assertIn('delete element inet nvxgrace hel4 { 10.64.2.237 }', dels)
        self.assertIn('delete element inet nvxgrace helflows { 10.64.2.237 . 1.1.1.1 . 443 . 5000, '
                      '10.64.2.237 . 2.2.2.2 . 1950 . 6000 }', dels)
        self.assertFalse([l for l in dels if '10.64.2.238' in l])

class RuleIdempotencyTests(unittest.TestCase):
    """05/10/2026: la 2.ª VM FSN→HEL hacia el mismo nodo se quedaba sin gracia
    («RTNETLINK answers: File exists») porque la regla ya existía y no se reconocía."""
    RULE = '99:\tfrom all fwmark 0x4e5602 iif vmbr0 lookup 112\n'

    def test_existing_rule_in_real_format_is_not_added_again(self):
        r, nft, ip = run_node(['on', VM, '86400', 'tun-fsn', 'tun-hel', ''], table='1', homeset='1', rules=self.RULE)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('GRACE_ON', r.stdout)
        self.assertFalse([l for l in ip if l.startswith('rule add')], ip)
        self.assertIn(f'add element inet nvxgrace fsn4 {{ {VM} timeout 86400s }}', nft)

    def test_other_region_rule_does_not_hide_a_missing_one(self):
        rules = '99:\tfrom all fwmark 0x4e5601 iif vmbr0 lookup 111\n'
        r, nft, ip = run_node(['on', VM, '86400', 'tun-fsn', 'tun-hel', ''], table='1', homeset='1', rules=rules)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('rule add pref 99 iif vmbr0 fwmark 0x4e5602 lookup 112', ip)

    def test_table_1120_is_not_table_112(self):
        rules = '99:\tfrom all fwmark 0x4e5602 iif vmbr0 lookup 1120\n'
        r, nft, ip = run_node(['on', VM, '86400', 'tun-fsn', 'tun-hel', ''], table='1', homeset='1', rules=rules)
        self.assertIn('rule add pref 99 iif vmbr0 fwmark 0x4e5602 lookup 112', ip)


if __name__ == '__main__':
    unittest.main()
