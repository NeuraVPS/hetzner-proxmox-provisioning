import importlib.util
import ipaddress
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SNIP = ROOT / 'base/snippets'
spec = importlib.util.spec_from_file_location('nvx_node64', SNIP / 'nvx-node64.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
N = ipaddress.ip_network


def heredoc(text, path):
    start = text.index(f"cat > /mnt{path} <<'NVXEOF'\n")
    start = text.index('\n', start) + 1
    return text[start:text.index('NVXEOF\n', start)]


class InstallCopyTests(unittest.TestCase):
    def test_install_sh_embeds_current_snippets(self):
        text = (ROOT / 'install.sh').read_text()
        for name, path in [('nvx-node64.py', '/usr/local/sbin/nvx-node64.py'),
                           ('nvx-node64.service', '/etc/systemd/system/nvx-node64.service'),
                           ('nvx-node64.timer', '/etc/systemd/system/nvx-node64.timer')]:
            self.assertEqual(heredoc(text, path), (SNIP / name).read_text(), name)
        self.assertIn('systemctl enable nvx-node64.service nvx-node64.timer', text)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def fw(self, text):
        p = os.path.join(self.tmp, 'cluster.fw')
        open(p, 'w').write(text)
        mod.CLUSTER_FW = p

    def test_infra_only_host_addresses_and_transit_by_tunnel(self):
        self.fw('[IPSET base]\n\n2001:db8:b::/64 # base\n2001:db8:e:ffff::/112\n10.0.0.0/8\n'
                '!2001:db8:bad::/64\n[IPSET hosts-ipv6]\n2001:db8:c::/64 # nodo\nbasura\n'
                '2001:db8:d::9/128\n[RULES]\nIN ACCEPT\n')
        hosts, tun = mod.infra_nets(mod.ipsets())
        hosts = {str(n) for n in hosts}
        self.assertEqual(hosts, {'2001:db8:b::1/128', '2001:db8:b::2/128', '2001:db8:c::1/128',
                                 '2001:db8:c::2/128', '2001:db8:d::9/128'})
        self.assertEqual([str(n) for n in tun], ['2001:db8:e:ffff::/112'])

    def test_unreadable_or_baseless_cluster_fw_is_an_error(self):
        mod.CLUSTER_FW = '/nonexistent/cluster.fw'
        self.assertRaises(mod.Fallo, mod.ipsets)
        self.fw('[IPSET base]\n10.0.0.0/8\n')
        self.assertRaises(mod.Fallo, mod.ipsets)

    def test_exclusions_only_for_resident_vms_with_their_ports(self):
        qd = os.path.join(self.tmp, 'q')
        os.mkdir(qd)
        open(os.path.join(qd, '520.conf'), 'w').write(
            'net0: virtio=x,bridge=vmbr0\nnet1: virtio=y,bridge=vmbr0,firewall=1\n[snap]\nnet7: z\n')
        allow = os.path.join(self.tmp, 'allow')
        open(allow, 'w').write('520\n1306\n12\nx\n')
        mod.QEMU_DIR, mod.EXCL_FILE = qd, allow
        self.assertEqual(mod.excl_vms(), {520: ['tap520i0', 'fwpr520p0', 'tap520i1', 'fwpr520p1']})
        mod.EXCL_FILE = os.path.join(self.tmp, 'missing')
        self.assertEqual(mod.excl_vms(), {})

    def test_ruleset_modes_and_bindings(self):
        p = [N('2a01:4f9:3081:3cdd::/64')]
        drop = mod.ruleset('drop', p, [N('2001:db8:b::2/128')], [N('2001:db8:e:ffff::/112')],
                           {520: ['tap520i0', 'fwpr520p0']})
        self.assertIn('2a01:4f9:3081:3cdd::208 . "tap520i0"', drop)
        self.assertIn('counter drop comment "nv-final"', drop)
        self.assertIn('ct direction reply ct state established', drop)
        self.assertNotIn('log prefix', drop)
        count = mod.ruleset('count', p, [], [], {})
        self.assertNotIn('counter drop comment "nv-final"', count)
        self.assertNotIn('hook input', drop + count)


if __name__ == '__main__':
    unittest.main()
