"""Test del predicado de resolución del prober conncheck (lógica pura).

Carga el módulo por importlib (el fichero lleva guiones) y ejerce
`resolution_for` sin tocar red ni Firestore. Correr con:
    python3 run_remotes/test_conncheck_resolution.py
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "neuravps_conncheck", Path(__file__).with_name("neuravps-conncheck.py"))
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


class ResolutionTests(unittest.TestCase):
    def test_still_confirmed_stays_open(self):
        # sigue inalcanzable este ciclo -> no se cierra
        self.assertIsNone(cc.resolution_for(
            719, confirmed_vmids={719}, probed_vmids={719}, running_vmids={719}))

    def test_probed_and_reachable_is_recovered(self):
        # la sondeamos y NO está en confirmed -> respondió -> recovered
        self.assertEqual("recovered", cc.resolution_for(
            719, confirmed_vmids=set(), probed_vmids={719}, running_vmids={719}))

    def test_not_running_is_no_longer_applicable(self):
        # ya no está entre las running/entregadas (parada/borrada/mantenimiento)
        self.assertEqual("no_longer_applicable", cc.resolution_for(
            719, confirmed_vmids=set(), probed_vmids=set(), running_vmids=set()))

    def test_running_but_not_probed_stays_open_conservative(self):
        # running pero sin sonda este ciclo (p.ej. servicio deshabilitado):
        # no afirmamos que se recuperó
        self.assertIsNone(cc.resolution_for(
            719, confirmed_vmids=set(), probed_vmids=set(), running_vmids={719}))

    def test_confirmed_wins_even_if_running_and_probed(self):
        # confirmed tiene prioridad: aunque esté running y sondeada, sigue caída
        self.assertIsNone(cc.resolution_for(
            5, confirmed_vmids={5}, probed_vmids={5}, running_vmids={5}))


if __name__ == "__main__":
    unittest.main()
