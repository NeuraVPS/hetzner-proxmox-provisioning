"""Egress probe: outbound-IP verdicts for the per-VM IPv4 pools (pure parts)."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("egresscheck_probe", ROOT / "run_remotes/neuravps-egresscheck.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)

PAR = {"hel": "198.51.100.5", "fsn": "203.0.113.69"}


class IpDeSalida(unittest.TestCase):
    def test_the_guest_script_reports_the_address(self):
        self.assertIn("cdn-cgi/trace", ec._PS)
        self.assertIn("' ip4='+$ip4", ec._PS)

    def test_parse_seen_address(self):
        salida = "v4g=200/32768/0.1/0 v4p=200/1024/0.05/0 v6g=200/32768/0.1/0 v6p=200/1024/0.1/0 dns=1.1.1.1 cpu=3 ip4=198.51.100.5"
        self.assertEqual(ec.ip_vista(salida), "198.51.100.5")
        for bad in ("ip4=NA", "ip4=999.1.1.1", "ip4=", "", None, "ip4=1.2.3"):
            self.assertIsNone(ec.ip_vista(bad))
        # the extra field must not disturb the existing verdicts
        ok, det = ec.analiza(salida)
        self.assertTrue(ok, det)

    def test_pools_gate(self):
        self.assertIsNone(ec.pools_activos(None))
        self.assertIsNone(ec.pools_activos({"enabled": False}))
        self.assertIsNone(ec.pools_activos({"enabled": True, "canaryVmids": ["x"]}))
        p = ec.pools_activos({"enabled": True, "canaryVmids": [201],
                              "pools": {"hel": {"activeServerIp": "95.216.102.179"}, "fsn": {}}})
        self.assertEqual((p["fleet"], p["canary"], p["concedidos"]), (False, {201}, False))

    def test_verdicts(self):
        pools = {"fleet": True, "canary": set(), "concedidos": True}
        self.assertEqual(ec.veredicto_ip("198.51.100.5", PAR, "helsinki", pools), "propia")
        self.assertEqual(ec.veredicto_ip("203.0.113.69", PAR, "helsinki", pools), "del_par")
        self.assertEqual(ec.veredicto_ip("203.0.113.69", PAR, "falkenstein", pools), "propia")
        self.assertEqual(ec.veredicto_ip("95.216.102.179", PAR, "helsinki", pools), "general")
        self.assertEqual(ec.veredicto_ip("8.8.8.8", PAR, "helsinki", pools), "ajena")
        self.assertEqual(ec.veredicto_ip(None, PAR, "helsinki", pools), "sin_dato")


if __name__ == "__main__":
    unittest.main()
