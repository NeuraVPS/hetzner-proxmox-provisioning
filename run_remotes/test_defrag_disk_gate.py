"""El gate de disco del rebalanceador.

`pick_dest` filtraba por RAM y cores y NO miraba el disco, así que podía mandar
una VM a un nodo que la venta ya rechazaba. Eso es incoherente: no tiene sentido
que `auto_provision` se niegue a colocar ahí y el defrag se la mande igual media
hora después.

Se usa el MISMO umbral y la MISMA fuente que la venta
(`auto_provision._DISK_REAL_MAX_PCT` sobre `proxmox_nodes.zpoolCapPct`), y ese
acoplamiento es intencionado: si algún día se mueve uno, tiene que moverse el
otro.

⚠️ Lo que más se vigila aquí es el FAIL-OPEN. Un nodo cuya ocupación aún no ha
sincronizado tiene `zpoolCapPct` a None, y si eso bloqueara, un fallo de sincronía
dejaría a media flota sin destinos y el rebalanceador se pararía justo cuando
más falta hace. Misma regla que ya usa la base de RAM.

    python3 run_remotes/test_defrag_disk_gate.py
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "neuravps_defrag", Path(__file__).with_name("neuravps-defrag.py"))
df = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(df)


def nodo(pct):
    return {"zpool_pct": pct}


class ElGate(unittest.TestCase):
    def test_un_nodo_holgado_sirve(self):
        for pct in (0.0, 34.0, 60.0, 79.9):
            self.assertTrue(df.disk_ok(nodo(pct)), pct)

    def test_un_nodo_lleno_no_sirve(self):
        # 82% es el 0000199 medido el 2026-08-21: 71% de fragmentación y
        # presión de E/S 12-25x la de sus iguales.
        for pct in (80.0, 82.0, 95.0, 100.0):
            self.assertFalse(df.disk_ok(nodo(pct)), pct)

    def test_el_umbral_es_EXACTAMENTE_el_de_la_venta(self):
        # Acoplado a propósito con auto_provision._DISK_REAL_MAX_PCT. Si alguien
        # cambia uno sin el otro, la venta y el rebalanceador discrepan sobre el
        # mismo nodo — que es el fallo que este gate viene a cerrar.
        self.assertEqual(80.0, df.DISK_REAL_MAX_PCT)


class FailOpen(unittest.TestCase):
    """La mitad que importa: no bloquear por falta de dato."""

    def test_sin_dato_sincronizado_SI_sirve(self):
        self.assertTrue(df.disk_ok(nodo(None)))

    def test_un_nodo_sin_la_clave_siquiera_SI_sirve(self):
        # Un doc viejo, o un nodo recién dado de alta que node_health aún no ha
        # visitado. Bloquearlo lo dejaría fuera del rebalanceo en silencio.
        self.assertTrue(df.disk_ok({}))


class NoEsUnGateDeVenta(unittest.TestCase):
    def test_no_toca_los_factores_de_capacidad(self):
        # Este cambio NO reduce cuántas VMs caben en un nodo: solo decide dónde
        # aterriza una que ya se estaba moviendo. Los factores de venta y de
        # colocación siguen donde estaban.
        self.assertEqual((1.5, 0.90, 0.80),
                         (df.COMMIT, df.FLOOR, df.FLOOR_SALES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
