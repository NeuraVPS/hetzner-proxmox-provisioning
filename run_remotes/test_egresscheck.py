"""Test de la clasificación del prober de salida (lógica pura, sin red).

Lo que se fija aquí es el discriminador que hace útil a toda la sonda: se bajan
DOS tamaños, 1 KB (cabe en un segmento) y 32 KB (no). Que el pequeño pase y el
grande no es una firma que un cliente NO PUEDE fabricar —un firewall bloquea
los dos o ninguno—, así que señala inequívocamente a nuestro lado.

Sin esa distinción la alerta tendría que exigir varias VMs del mismo nodo
fallando, y eso dejaba fuera a los 115 nodos de una sola VM (los VPS-E, el plan
más caro), que nunca habrían podido disparar una alerta. De ahí que estos casos
estén clavados aquí: si alguien simplifica la sonda a una sola descarga,
vuelven los 115 nodos ciegos y nada más se rompe visiblemente.

    python3 run_remotes/test_egresscheck.py
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "neuravps_egresscheck", Path(__file__).with_name("neuravps-egresscheck.py"))
eg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eg)

G_OK = f"200/{eg.BYTES}/0.09/0"           # 32 KB enteros y rapido
P_OK = f"200/{eg.BYTES_MIN}/0.02/0"       # 1 KB entero y rapido
# Lo medido el 18-08-2026 con el clamp quitado: entrega, pero con el backoff
# de retransmision 1+2+4. Este es el caso que daba VERDE antes.
G_LENTO = f"200/{eg.BYTES}/6.49/0"
P_LENTO = f"200/{eg.BYTES_MIN}/6.55/0"
TRUNCADO = "200/1448/25.0/28"             # conecta y se atraganta
TIMEOUT = "000/0/25.0/28"
BLOQUEADO = "000/0/0.01/7"                # firewall: ni conecta


class Descarga(unittest.TestCase):
    def test_entera_es_ok(self):
        self.assertTrue(eg._descarga(G_OK, eg.BYTES)[0])

    def test_de_mas_sigue_siendo_ok(self):
        self.assertTrue(eg._descarga(f"200/{eg.BYTES+500}/0.09/0", eg.BYTES)[0])

    def test_devuelve_el_tiempo(self):
        self.assertAlmostEqual(6.49, eg._descarga(G_LENTO, eg.BYTES)[2])

    def test_a_medias_no_es_ok(self):
        ok, motivo, _t = eg._descarga(TRUNCADO, eg.BYTES)
        self.assertFalse(ok)
        self.assertEqual("truncado", motivo)

    def test_ilegible_no_revienta(self):
        for basura in ("", "nada", "200/x/y/z", "200/32768"):
            self.assertEqual((False, "ilegible", 0.0), eg._descarga(basura, eg.BYTES))


class Discriminador(unittest.TestCase):
    def test_las_dos_bien_es_ok(self):
        self.assertEqual("ok", eg.veredicto_pila(G_OK, P_OK))

    def test_pequeno_pasa_y_grande_no_es_NUESTRO(self):
        # LA FIRMA. Handshake y 1 KB pasan, 32 KB se atraganta = MTU/MSS.
        # Un firewall de cliente no puede producir esto.
        self.assertEqual("mtu", eg.veredicto_pila(TRUNCADO, P_OK))

    def test_grande_que_expira_con_pequeno_bueno_tambien_es_mtu(self):
        self.assertEqual("mtu", eg.veredicto_pila(TIMEOUT, P_OK))

    def test_entrega_lenta_NO_es_ok(self):
        # EL CASO QUE SE ESCAPO. Código 200 y los bytes enteros, pero 6,5 s
        # donde se tarda 0,06: el camino está retransmitiendo. Con el clamp
        # quitado de verdad, la sonda daba verde sin esta comprobación.
        self.assertEqual("lento", eg.veredicto_pila(G_OK, P_LENTO))
        self.assertEqual("lento", eg.veredicto_pila(G_LENTO, P_OK))

    def test_rapido_de_verdad_sigue_siendo_ok(self):
        # El umbral no puede ser tan fino que un hipo normal alerte.
        self.assertEqual("ok", eg.veredicto_pila(
            f"200/{eg.BYTES}/1.20/0", f"200/{eg.BYTES_MIN}/0.90/0"))

    def test_los_dos_bloqueados_es_ambiguo(self):
        # Esto SÍ lo puede hacer el cliente cerrándose el firewall.
        self.assertEqual("cortado", eg.veredicto_pila(BLOQUEADO, BLOQUEADO))

    def test_grande_bien_y_pequeno_mal_es_ruido(self):
        # Físicamente raro; no se alerta por ello.
        self.assertEqual("raro", eg.veredicto_pila(G_OK, BLOQUEADO))


class Analiza(unittest.TestCase):
    def _sal(self, v4g=G_OK, v4p=P_OK, v6g=G_OK, v6p=P_OK, dns="1.2.3.4"):
        return f"v4g={v4g} v4p={v4p} v6g={v6g} v6p={v6p} dns={dns}"

    def test_todo_bien(self):
        ok, det = self._ana(self._sal())
        self.assertTrue(ok)
        self.assertEqual("ok", det["kind"])

    def _ana(self, s):
        return eg.analiza(s)

    def test_mtu_en_una_sola_pila_ya_marca_el_veredicto(self):
        # Basta una pila: los invitados son IPv6 nativos y su IPv4 pasa por el
        # NAT de la base — son dos caminos y se rompen por separado.
        ok, det = self._ana(self._sal(v4g=TRUNCADO))
        self.assertFalse(ok)
        self.assertEqual("mtu", det["kind"])
        self.assertEqual("mtu", det["v4"])
        self.assertEqual("ok", det["v6"])

    def test_bloqueo_total_es_cortado_no_mtu(self):
        # Esta es la diferencia que decide si alertamos con UNA sola VM.
        ok, det = self._ana(self._sal(v4g=BLOQUEADO, v4p=BLOQUEADO,
                                      v6g=BLOQUEADO, v6p=BLOQUEADO))
        self.assertFalse(ok)
        self.assertEqual("cortado", det["kind"])

    def test_lento_cuenta_como_nuestro_no_como_ambiguo(self):
        # Decide si alertamos con UNA sola VM: `lento` va con `mtu`, no con
        # `cortado`. En los 115 nodos de una VM es la diferencia entre verlo
        # y no verlo nunca.
        ok, det = self._ana(self._sal(v4p=P_LENTO))
        self.assertFalse(ok)
        self.assertEqual("lento", det["kind"])

    def test_mtu_manda_sobre_lento(self):
        # Si conviven, el veredicto más específico es el que ayuda a arreglar.
        ok, det = self._ana(self._sal(v4g=TRUNCADO, v6p=P_LENTO))
        self.assertFalse(ok)
        self.assertEqual("mtu", det["kind"])

    def test_dns_roto_es_fallo_aunque_bajen_los_bytes(self):
        # Se puede tener camino y no tener nombres: al cliente le da igual el
        # matiz, su SQX no valida licencia.
        ok, det = self._ana(self._sal(dns="FALLO"))
        self.assertFalse(ok)
        self.assertEqual("cortado", det["kind"])

    def test_salida_vacia_del_agente_no_pasa_por_buena(self):
        # Sería justo el falso verde que esta sonda existe para evitar.
        self.assertFalse(self._ana("")[0])


class ParametrosQueNoDebenBajar(unittest.TestCase):
    def test_el_grande_llena_varios_segmentos(self):
        # Con menos de ~16 KB la descarga cabe en pocos segmentos y un camino
        # con la MTU mal podría colarse entera. El número no es decorativo.
        self.assertGreaterEqual(eg.BYTES, 16384)

    def test_el_pequeno_cabe_en_un_segmento(self):
        # Si creciera por encima de la MTU dejaría de ser control y la firma
        # `mtu` desaparecería sin que fallara ningún test.
        self.assertLessEqual(eg.BYTES_MIN, 1400)

    def test_ambas_urls_piden_bytes_explicitos(self):
        self.assertIn(f"bytes={eg.BYTES}", eg.URL)
        self.assertIn(f"bytes={eg.BYTES_MIN}", eg.URL_MIN)

    def test_el_umbral_de_lentitud_separa_bien_los_dos_mundos(self):
        # Sano medido: 0,02–0,10 s. Roto medido: 6,45–6,55 s. El umbral tiene
        # que caer holgadamente entre medias, sin rozar ninguno de los dos.
        self.assertGreater(eg.UMBRAL_LENTO, 1.0)
        self.assertLess(eg.UMBRAL_LENTO, 6.0)

    def test_el_umbral_global_evita_227_correos(self):
        self.assertTrue(0 < eg.GLOBAL_PCT < 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
