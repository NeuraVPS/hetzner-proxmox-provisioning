"""nvx-aliviar-nodo.py: guarda de CPU del destino (25/11/2026), igual que cpu_ok del defrag."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("aliviar", ROOT / "run_remotes/nvx-aliviar-nodo.py")
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)
NOW = 1_800_000_000.0


class CpuOk(unittest.TestCase):
    def test_sin_medida_falla_abierto(self):
        self.assertTrue(al.cpu_ok({"nodeType": "MT5"}, NOW))
        self.assertTrue(al.cpu_ok({"nodeType": "SQX"}, NOW))

    def test_contencion_reciente_alta_bloquea(self):
        n = {"nodeType": "MT5", "cpuContentionMsS": 67.0, "cpuContentionAt": NOW - 600}
        self.assertFalse(al.cpu_ok(n, NOW))
        n["cpuContentionMsS"] = 50.0
        self.assertTrue(al.cpu_ok(n, NOW))

    def test_contencion_rancia_no_decide(self):
        n = {"nodeType": "MT5", "cpuContentionMsS": 300.0, "cpuContentionAt": NOW - 7 * 3600}
        self.assertTrue(al.cpu_ok(n, NOW))

    def test_p95_laborable_mt(self):
        n = {"nodeType": "MT5", "cpuWeekdayP95Pct": 85.0, "cpuProfileAt": NOW - 3600}
        self.assertFalse(al.cpu_ok(n, NOW))
        n["cpuWeekdayP95Pct"] = 84.9
        self.assertTrue(al.cpu_ok(n, NOW))
        # perfil de mas de 48 h: no decide
        self.assertTrue(al.cpu_ok({"nodeType": "MT5", "cpuWeekdayP95Pct": 95.0,
                                   "cpuProfileAt": NOW - 49 * 3600}, NOW))

    def test_p95_no_aplica_a_sqx(self):
        n = {"nodeType": "SQX", "cpuWeekdayP95Pct": 99.0, "cpuProfileAt": NOW - 60}
        self.assertTrue(al.cpu_ok(n, NOW))

    def test_basura_no_rompe(self):
        self.assertTrue(al.cpu_ok({"nodeType": "MT5", "cpuContentionMsS": "x"}, NOW))


if __name__ == "__main__":
    unittest.main()
