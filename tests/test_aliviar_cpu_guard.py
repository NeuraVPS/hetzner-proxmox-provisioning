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


GENOA = ("AuthenticAMD", "25", "17", "1", "1", "1")
TURIN = ("AuthenticAMD", "26", "2", "1", "1", "1")
RAPHAEL = ("AuthenticAMD", "25", "97", "2", "1", "1")
INTEL = ("GenuineIntel", "6", "183", "1", "0", "1")


class CpuPermite(unittest.TestCase):
    """26/11/2026: misma regla que el defrag y migrate_vm.sh."""

    def test_generica_entre_genoa_y_turin(self):
        for m in ("x86-64-v4", "x86-64-v3", "x86-64-v2-AES"):
            self.assertTrue(al.cpu_permite(m, GENOA, TURIN), m)
            self.assertTrue(al.cpu_permite(m, TURIN, GENOA), m)

    def test_generica_no_cruza_a_raphael_ni_a_intel(self):
        # antes bastaba el mismo fabricante: planeaba lo que migrate_vm.sh rechaza
        self.assertFalse(al.cpu_permite("x86-64-v4", GENOA, RAPHAEL))
        self.assertFalse(al.cpu_permite("x86-64-v3", RAPHAEL, TURIN))
        self.assertFalse(al.cpu_permite("x86-64-v3", GENOA, INTEL))

    def test_condiciones_del_cruce(self):
        self.assertFalse(al.cpu_permite("x86-64-v4", GENOA, TURIN[:5] + ("0",)))   # sin tsc_scale
        self.assertFalse(al.cpu_permite("x86-64-v4", TURIN, GENOA[:4] + ("0", "1")))  # sin AVX-512
        self.assertTrue(al.cpu_permite("x86-64-v3", TURIN, GENOA[:4] + ("0", "1")))
        self.assertFalse(al.cpu_permite("x86-64-v4", GENOA, TURIN[:4]))            # firma corta

    def test_opciones_no_cruzan(self):
        self.assertFalse(al.cpu_permite("x86-64-v4,phys-bits=host", GENOA, TURIN))
        self.assertTrue(al.cpu_permite("x86-64-v4,phys-bits=host", GENOA, GENOA))

    def test_kademar_destino_solo_con_interruptor(self):
        src = (ROOT / "run_remotes/nvx-aliviar-nodo.py").read_text()
        self.assertIn('.get("cappedDestEnabled") is True', src)
        self.assertIn("if tope is not None and not capped_ok:", src)

    def test_host_solo_mismo_silicio(self):
        self.assertFalse(al.cpu_permite("host", GENOA, TURIN))
        self.assertFalse(al.cpu_permite("kvm64", TURIN, GENOA))
        self.assertTrue(al.cpu_permite("host", GENOA, GENOA[:4] + ("0", "0")))
        self.assertFalse(al.cpu_permite("host", GENOA, GENOA[:3] + ("2", "1", "1")))

    def test_sin_dato_no_se_mueve(self):
        self.assertFalse(al.cpu_permite(None, GENOA, TURIN))
        self.assertFalse(al.cpu_permite("x86-64-v4", None, TURIN))
        self.assertFalse(al.cpu_permite("x86-64-v4", GENOA, None))


class HolguraTope(unittest.TestCase):
    def test_sin_tope(self):
        self.assertIsNone(al.holgura_tope({"nodeType": "SQX"}, "n", {}, {}, 4))
        self.assertIsNone(al.holgura_tope({"nodeType": "MT5", "max_vm": 48}, "n", {}, {}, 4))

    def test_margenes(self):
        n = {"nodeType": "SQX", "max_vm": 26, "max_vcpu": 500}
        self.assertTrue(al.holgura_tope(n, "k", {"k": 24}, {"k": 400}, 20))
        self.assertFalse(al.holgura_tope(n, "k", {"k": 25}, {"k": 400}, 20))
        self.assertTrue(al.holgura_tope(n, "k", {"k": 1}, {"k": 460}, 20))
        self.assertFalse(al.holgura_tope(n, "k", {"k": 1}, {"k": 461}, 20))


if __name__ == "__main__":
    unittest.main()
