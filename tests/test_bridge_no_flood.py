import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'run_remotes/neuravps-bridge-no-flood.py'
spec = importlib.util.spec_from_file_location('no_flood', path)
no_flood = importlib.util.module_from_spec(spec)
spec.loader.exec_module(no_flood)
CONFIG = '''auto vmbr0
iface vmbr0 inet static
    hwaddress 02:83:0a:90:34:f2
    address 10.0.0.1/16
    bridge-ports none
    post-up   ip addr add 10.64.255.1/16 dev vmbr0

iface vmbr0 inet6 static
    address 2001:db8::1/64
'''


class NoFloodConfigTests(unittest.TestCase):
    def test_adds_key_only_to_inet_stanza_and_is_idempotent(self):
        out = no_flood.configured_text(CONFIG)
        self.assertIn('iface vmbr0 inet static\n    bridge-disable-mac-learning 1\n    hwaddress', out)
        self.assertEqual(out.count('bridge-disable-mac-learning'), 1)
        self.assertEqual(no_flood.configured_text(out), out)
        self.assertEqual(out.replace('    bridge-disable-mac-learning 1\n', ''), CONFIG)

    def test_unconfig_restores_original(self):
        self.assertEqual(no_flood.configured_text(no_flood.configured_text(CONFIG), enable=False), CONFIG)

    def test_replaces_other_values(self):
        text = CONFIG.replace('    bridge-ports none\n', '    bridge-ports none\n    bridge-disable-mac-learning 0\n')
        out = no_flood.configured_text(text)
        self.assertEqual(out.count('bridge-disable-mac-learning'), 1)
        self.assertIn('bridge-disable-mac-learning 1', out)

    def test_missing_stanza_is_an_error(self):
        with self.assertRaises(ValueError):
            no_flood.configured_text('auto lo\niface lo inet loopback\n')

    def test_mac_regex(self):
        m = no_flood.MAC_RE.search('virtio=52:54:00:D5:27:F1,bridge=vmbr0,firewall=0')
        self.assertEqual(m.group(1), '52:54:00:D5:27:F1')


if __name__ == '__main__':
    unittest.main()
