import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'run_remotes/neuravps-pin-bridge-mac.py'
spec = importlib.util.spec_from_file_location('bridge_mac', path)
bridge_mac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mac)
MAC = '02:00:00:11:22:33'
CONFIG = '''source /etc/network/interfaces.d/*
auto vmbr0
iface vmbr0 inet static
    address 10.0.0.1/16
    bridge-ports none
    post-up ip -6 addr add fe80::1/64 dev vmbr0 || true

iface vmbr0 inet6 static
    address 2001:db8::1/64

auto eth0
iface eth0 inet manual
    hwaddress 02:00:00:00:00:01
'''


class BridgeMacTests(unittest.TestCase):
    def test_live_address_is_preserved(self):
        self.assertEqual(bridge_mac.select_mac(MAC, ['tap42i0'], 'node'), MAC)

    def test_empty_zero_address_gets_stable_unicast_address(self):
        mac = bridge_mac.select_mac('00:00:00:00:00:00', [], 'node-one')
        self.assertTrue(bridge_mac.valid_mac(mac))
        self.assertTrue(mac.startswith('02:'))
        self.assertEqual(mac, bridge_mac.select_mac('00:00:00:00:00:00', [], 'node-one'))
        self.assertNotEqual(mac, bridge_mac.select_mac('00:00:00:00:00:00', [], 'node-two'))

    def test_zero_address_with_guest_port_is_rejected(self):
        with self.assertRaises(ValueError):
            bridge_mac.select_mac('00:00:00:00:00:00', ['tap42i0'], 'node')

    def test_invalid_nonzero_address_is_not_replaced(self):
        with self.assertRaises(ValueError):
            bridge_mac.select_mac('ff:ff:ff:ff:ff:ff', [], 'node')

    def test_only_one_line_is_added(self):
        result = bridge_mac.configured_text(CONFIG, MAC)
        self.assertEqual(result.replace(f'    hwaddress {MAC}\n', ''), CONFIG)
        self.assertIn(f'iface vmbr0 inet static\n    hwaddress {MAC}\n', result)

    def test_idempotent(self):
        result = bridge_mac.configured_text(CONFIG, MAC)
        self.assertEqual(bridge_mac.configured_text(result, MAC), result)

    def test_existing_ipv6_attribute_is_respected(self):
        text = CONFIG.replace('iface vmbr0 inet6 static\n',
                              f'iface vmbr0 inet6 static\n    hwaddress ether {MAC.upper()}\n')
        self.assertEqual(bridge_mac.configured_text(text, MAC), text)

    def test_conflict_is_rejected(self):
        text = CONFIG.replace('    bridge-ports none', '    hwaddress 02:00:00:00:00:02\n    bridge-ports none')
        with self.assertRaises(ValueError):
            bridge_mac.configured_text(text, MAC)

    def test_missing_or_duplicate_stanza_is_rejected(self):
        for text in (CONFIG.replace('vmbr0', 'vmbr1'), CONFIG + '\niface vmbr0 inet manual\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                bridge_mac.configured_text(text, MAC)

    def test_invalid_or_multicast_mac_is_rejected(self):
        for mac in ('00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff', '01:00:5e:00:00:01', 'garbage'):
            with self.subTest(mac=mac), self.assertRaises(ValueError):
                bridge_mac.configured_text(CONFIG, mac)

    def test_commented_attribute_does_not_count(self):
        text = CONFIG.replace('    bridge-ports none', '    # hwaddress 02:00:00:00:00:02\n    bridge-ports none')
        self.assertIn(f'    hwaddress {MAC}\n', bridge_mac.configured_text(text, MAC))


if __name__ == '__main__':
    unittest.main()
