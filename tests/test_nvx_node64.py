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
    def test_infra_takes_base_and_only_host_addresses_of_nodes(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('[IPSET base]\n\n2001:db8:b::/64 # base\n10.0.0.0/8\n!2001:db8:bad::/64\n'
                    '[IPSET hosts-ipv6]\n2001:db8:c::/64 # nodo\nbasura\n[RULES]\nIN ACCEPT\n')
        mod.CLUSTER_FW = f.name
        try:
            infra = {str(n) for n in mod.infra_nets()}
        finally:
            os.unlink(f.name)
        self.assertIn('2001:db8:b::/64', infra)
        self.assertIn('2001:db8:c::1/128', infra)
        self.assertIn('2001:db8:c::2/128', infra)
        self.assertNotIn('2001:db8:c::/64', infra)
        self.assertIn('2a01:4f8:2b01:124::/64', infra)   # copia fija
        self.assertTrue(all(ipaddress.ip_network(n).version == 6 for n in infra))

    def test_missing_cluster_fw_keeps_bases(self):
        mod.CLUSTER_FW = '/nonexistent/cluster.fw'
        self.assertEqual(len(mod.infra_nets()), len(mod.BASE_FALLBACK))

    def test_ruleset_derives_excluded_addresses_and_modes(self):
        p = [ipaddress.ip_network('2a01:4f9:3081:3cdd::/64')]
        drop = mod.ruleset('drop', p, [ipaddress.ip_network('2001:db8:b::/64')], [520, 1409])
        self.assertIn('2a01:4f9:3081:3cdd::208', drop)
        self.assertIn('2a01:4f9:3081:3cdd::581', drop)
        self.assertIn('counter drop comment "nv-final"', drop)
        self.assertNotIn('log prefix', drop)
        count = mod.ruleset('count', p, [ipaddress.ip_network('2001:db8:b::/64')], [])
        self.assertNotIn(' drop', count)
        self.assertIn('set excl6 { type ipv6_addr;  }', count)
        self.assertNotIn('hook input', drop + count)


if __name__ == '__main__':
    unittest.main()
