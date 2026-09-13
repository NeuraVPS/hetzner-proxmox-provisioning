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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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
        self.assertEqual("lento", eg.veredicto_pila(G_LENTO, P_OK))
        # El pequeño solo YA NO basta: su respuesta de 1 KB cabe en un segmento,
        # nunca manda un paquete a tamaño completo y por tanto no puede probar
        # la ruta del MTU. Lo que mide su `time_total` es sobre todo el saludo
        # (DNS+TCP+TLS), y un SYN perdido cuesta 1+2 s. Ver el 0000045 el
        # 22-08-2026: v6p=3.026s con v6g=0.123s por el mismo camino.
        self.assertEqual("ok", eg.veredicto_pila(G_OK, P_LENTO))

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
        ok, det = self._ana(self._sal(v4g=G_LENTO))
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


class LaMedidaViajaConElVeredicto(unittest.TestCase):
    """Sin el número, el aviso dice "lento" y no cuánto — que es el dato que lo
    justifica y el único con el que se puede calibrar el umbral. Pasó el
    2026-08-20 con la vm773: saltó la alerta, se resolvió sola en una hora, y no
    había forma de saber si habían sido 3,1 segundos o 25."""

    def test_el_detalle_incluye_la_salida_cruda(self):
        sal = (f"v4g=200/{eg.BYTES}/6.49/0 v4p=200/{eg.BYTES_MIN}/0.02/0 "
               f"v6g=200/{eg.BYTES}/0.09/0 v6p=200/{eg.BYTES_MIN}/0.02/0 dns=1.2.3.4")
        _ok, det = eg.analiza(sal)
        self.assertEqual(sal, det["medida"])
        self.assertIn("6.49", det["medida"], "el tiempo que disparó la alerta")

    def test_tambien_cuando_todo_va_bien(self):
        # Una medida sana también sirve: es la línea base contra la que se
        # decide si el umbral está bien puesto.
        sal = (f"v4g=200/{eg.BYTES}/0.09/0 v4p=200/{eg.BYTES_MIN}/0.02/0 "
               f"v6g=200/{eg.BYTES}/0.09/0 v6p=200/{eg.BYTES_MIN}/0.02/0 dns=1.2.3.4")
        ok, det = eg.analiza(sal)
        self.assertTrue(ok)
        self.assertEqual(sal, det["medida"])


class UnInvitadoOCUPADONoEsUnaREDLENTA(unittest.TestCase):
    """La sonda corre `curl` DENTRO del invitado. Un invitado con la CPU al tope
    hace que se planifique tarde y el reloj marque segundos que no son de red.

    Pasó el 2026-08-21 con la vm587: saltó "salida a Internet rota" en el nodo
    0000223 y lo que había era StrategyQuant llevando 2.303 HORAS de CPU al
    100% — el cliente usando el servidor exactamente para lo que lo compró. Las
    otras 13 VMs del mismo nodo dieron 222 medidas sin una sola lenta.

    Confundir las dos cosas convierte a cada cliente que exprime su VPS en una
    falsa alarma, y tres falsas alarmas seguidas enseñan a ignorar el aviso.
    """

    def _sal(self, tg, cpu):
        # Se varia el GRANDE, no el pequeño: desde el 22-08-2026 un pequeño
        # lento con el grande rapido ya no es `lento` (el grande lo desmiente),
        # asi que montarlo sobre el pequeño probaria otra cosa.
        return (f"v4g=200/{eg.BYTES}/{tg}/0 v4p={P_OK} "
                f"v6g={G_OK} v6p={P_OK} dns=1.2.3.4 cpu={cpu}")

    def test_lento_con_la_CPU_al_tope_NO_es_fallo(self):
        ok, det = eg.analiza(self._sal("6.50", 100))
        self.assertTrue(ok, "un invitado saturado no puede acusar a la red")
        self.assertEqual("no_concluyente", det["kind"])
        self.assertEqual(100, det["cpuInvitado"])

    def test_lento_con_la_CPU_TRANQUILA_si_es_fallo(self):
        ok, det = eg.analiza(self._sal("6.50", 12))
        self.assertFalse(ok)
        self.assertEqual("lento", det["kind"])

    def test_el_caso_REAL_de_la_vm587(self):
        # La medida literal que disparó el aviso, más su CPU real.
        sal = ("v4g=200/32768/0.215979/0 v4p=200/1024/3.605303/0 "
               "v6g=200/32768/0.162943/0 v6p=200/1024/0.111383/0 "
               "dns=172.66.0.218 cpu=100")
        ok, det = eg.analiza(sal)
        self.assertTrue(ok, "no debería haber avisado")
        # Antes se salvaba por la CPU al 100%. Desde el 22-08-2026 se descarta
        # un paso ANTES: v4g=0.216s desmiente a v4p=3.605s sin necesidad de
        # mirar la CPU. Mejor asi — deja de depender de que el invitado
        # estuviera ocupado, que fue una coincidencia de aquel caso.
        self.assertEqual("ok", det["kind"])

    def test_MTU_sigue_siendo_fallo_por_saturada_que_este(self):
        # LO IMPORTANTE. Ninguna carga de CPU puede hacer que 1 KB pase y 32 KB
        # no: esa firma es infalsificable y tiene que seguir alertando aunque el
        # invitado esté al 100%. Si esto se rompiera, la excusa de la CPU
        # taparía la única avería que sabemos diagnosticar con certeza.
        sal = (f"v4g={TRUNCADO} v4p={P_OK} v6g={G_OK} v6p={P_OK} "
               f"dns=1.2.3.4 cpu=100")
        ok, det = eg.analiza(sal)
        self.assertFalse(ok)
        self.assertEqual("mtu", det["kind"])

    def test_sin_dato_de_CPU_se_comporta_como_antes(self):
        # Invitados con la sonda vieja, o donde el WMI falle: -1 no debe
        # silenciar nada.
        ok, det = eg.analiza(self._sal("6.50", -1))
        self.assertFalse(ok)
        self.assertEqual("lento", det["kind"])

    def test_el_umbral_deja_margen_a_un_invitado_que_oscila(self):
        self.assertGreaterEqual(eg.CPU_INVITADO_SATURADO, 85)
        self.assertLess(eg.CPU_INVITADO_SATURADO, 100)


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



class UnGrandeRAPIDODesmienteAUnPequenoLENTO(unittest.TestCase):
    """32 KB por el mismo camino y en la misma pasada son prueba positiva.

    El 22-08-2026 el 0000045-AX102 alerto por `lento` con v6p=3.026s y
    v6g=0.123s. Si el camino retransmitiera, el que lleva 32 veces mas
    paquetes tardaria MAS, no 25 veces menos. Esos 3,026 s son un SYN perdido
    (1 s + 2 s de reintento TCP), o sea establecimiento de conexion. Seis
    re-sondeos posteriores: todos limpios, el peor 0,73 s.
    """

    def test_el_caso_REAL_del_0000045(self):
        salida = ("v4g=200/32768/0.069428/0 v4p=200/1024/0.072745/0 "
                  "v6g=200/32768/0.122970/0 v6p=200/1024/3.026448/0 "
                  "dns=162.159.140.220 cpu=8")
        ok, det = eg.analiza(salida)
        self.assertTrue(ok, "un grande que vuela desmiente al pequeño")
        self.assertEqual(det["kind"], "ok")

    def test_si_el_GRANDE_va_lento_sigue_alertando(self):
        # La firma del clamp de MSS: el grande es el que sufre. Intocada.
        v = eg.veredicto_pila("200/32768/6.500000/0", "200/1024/0.070000/0")
        self.assertEqual(v, "lento")

    def test_los_DOS_lentos_siguen_alertando(self):
        v = eg.veredicto_pila("200/32768/5.000000/0", "200/1024/4.000000/0")
        self.assertEqual(v, "lento")

    def test_pequeno_lento_con_grande_TAMBIEN_tocado_alerta(self):
        # 1,5 s en 32 KB no es volar: el pequeño conserva su valor de señal.
        v = eg.veredicto_pila("200/32768/1.500000/0", "200/1024/3.500000/0")
        self.assertEqual(v, "lento")

    def test_el_umbral_del_grande_sano_es_exclusivo_por_abajo(self):
        # Justo por debajo de 1 s -> se descarta; justo por encima -> alerta.
        self.assertEqual(eg.veredicto_pila("200/32768/0.999000/0",
                                           "200/1024/3.500000/0"), "ok")
        self.assertEqual(eg.veredicto_pila("200/32768/1.000000/0",
                                           "200/1024/3.500000/0"), "lento")

    def test_MTU_no_se_ve_afectado(self):
        # El grande NO entrega: sigue siendo mtu, pase lo que pase con tiempos.
        v = eg.veredicto_pila("000/0/0.100000/28", "200/1024/0.050000/0")
        self.assertEqual(v, "mtu")


class ConfirmaLentoDentroDeLaPasada(unittest.TestCase):
    """Un 'lento' de UNA medida no basta (ver ANTI-RUIDO, 13-09-2026): tres
    pasadas de b1 seguidas marcaron "certeza alta" a partir de una sola
    descarga de 2-4 s mientras el uplink estaba al 0,16% de uso, y las tres se
    resolvieron solas en la pasada siguiente. `_confirma_lento` exige mayoria
    (2 de 3) antes de aceptar el veredicto."""

    def _det(self, kind):
        return {"kind": kind, "v4": kind if kind != "ok" else "ok", "v6": "ok",
                "dns": "ok", "medida": "x", "cpuInvitado": -1}

    def test_un_solo_hipo_no_confirma(self):
        # Las dos re-medidas salen limpias: era un SYN perdido, no una averia.
        salidas = iter([self._det("ok"), self._det("ok")])
        probe_fn = lambda ip, vmid: (True, "cualquier-cosa")
        with mock.patch.object(eg, "analiza", side_effect=lambda s: (True, next(salidas))):
            ok, det = eg._confirma_lento("10.0.0.1", 100, self._det("lento"), probe_fn=probe_fn, sleep_fn=lambda *_: None)
        self.assertTrue(ok, "un hipo de un momento no debe confirmarse como lento")

    def test_lento_persistente_si_confirma(self):
        # Las dos re-medidas TAMBIEN salen lentas: mayoria 3 de 3.
        salidas = iter([self._det("lento"), self._det("lento")])
        probe_fn = lambda ip, vmid: (True, "cualquier-cosa")
        with mock.patch.object(eg, "analiza", side_effect=lambda s: (False, next(salidas))):
            ok, det = eg._confirma_lento("10.0.0.1", 100, self._det("lento"), probe_fn=probe_fn, sleep_fn=lambda *_: None)
        self.assertFalse(ok)
        self.assertEqual("lento", det["kind"])

    def test_mayoria_2_de_3_tambien_confirma(self):
        salidas = iter([self._det("lento"), self._det("ok")])
        probe_fn = lambda ip, vmid: (True, "cualquier-cosa")
        with mock.patch.object(eg, "analiza", side_effect=lambda s: (True, next(salidas))):
            ok, det = eg._confirma_lento("10.0.0.1", 100, self._det("lento"), probe_fn=probe_fn, sleep_fn=lambda *_: None)
        self.assertFalse(ok, "2 de 3 diciendo lento ya es mayoria")

    def test_un_mtu_en_el_reintento_gana_sin_esperar_a_la_mayoria(self):
        # Es la firma infalsificable: no necesita mayoria ni una segunda vuelta.
        salidas = iter([self._det("mtu")])
        probe_fn = lambda ip, vmid: (True, "cualquier-cosa")
        with mock.patch.object(eg, "analiza", side_effect=lambda s: (False, next(salidas))):
            ok, det = eg._confirma_lento("10.0.0.1", 100, self._det("lento"), probe_fn=probe_fn, sleep_fn=lambda *_: None)
        self.assertFalse(ok)
        self.assertEqual("mtu", det["kind"])

    def test_agente_mudo_en_el_reintento_no_cuenta_ni_a_favor_ni_en_contra(self):
        # Si el canal no contesta esa vuelta, ni confirma ni descarta: se
        # descuenta la muestra, no se convierte en "confirmado por defecto".
        probe_fn = lambda ip, vmid: (False, "sin canal")
        ok, det = eg._confirma_lento("10.0.0.1", 100, self._det("lento"), probe_fn=probe_fn, sleep_fn=lambda *_: None)
        self.assertTrue(ok, "sin mas medidas que la primera, no hay mayoria")

    def test_no_duerme_mas_de_lo_necesario_si_ya_gano_mtu(self):
        sleeps = []
        salidas = iter([self._det("mtu")])
        probe_fn = lambda ip, vmid: (True, "cualquier-cosa")
        with mock.patch.object(eg, "analiza", side_effect=lambda s: (False, next(salidas))):
            eg._confirma_lento("10.0.0.1", 100, self._det("lento"),
                               probe_fn=probe_fn, sleep_fn=sleeps.append)
        self.assertEqual(1, len(sleeps), "un mtu corta el bucle a la primera vuelta")


class ClasificaCulpables(unittest.TestCase):
    """Quien alerta a la primera y quien queda pendiente de confirmar entre
    pasadas. Pura: no toca Firestore."""

    def _vm(self, vmid, kind):
        return {"vmid": vmid, "detalle": {"kind": kind}}

    def test_mtu_una_sola_vm_alerta_ya(self):
        culpables, pendiente = eg.clasifica_culpables(
            {"n1": [self._vm(1, "mtu")]}, {"n1": [False]})
        self.assertIn("n1", culpables)
        self.assertEqual("mtu", culpables["n1"]["regla"])
        self.assertEqual({}, pendiente)

    def test_lento_una_sola_vm_queda_pendiente_no_alerta_ya(self):
        # ESTE es el caso que producia ruido el 13-09: una sola VM 'lento'.
        culpables, pendiente = eg.clasifica_culpables(
            {"n1": [self._vm(1, "lento")]}, {"n1": [False]})
        self.assertEqual({}, culpables, "una sola VM lenta no debe alertar sola")
        self.assertIn("n1", pendiente)

    def test_lento_dos_vms_del_mismo_nodo_alerta_ya(self):
        # Dos VMs distintas coincidiendo en 'lento' se corroboran solas.
        culpables, pendiente = eg.clasifica_culpables(
            {"n1": [self._vm(1, "lento"), self._vm(2, "lento")]}, {"n1": [False, False]})
        self.assertIn("n1", culpables)
        self.assertEqual("lento", culpables["n1"]["regla"])
        self.assertEqual({}, pendiente)

    def test_mtu_manda_sobre_lento_si_conviven_en_el_nodo(self):
        culpables, pendiente = eg.clasifica_culpables(
            {"n1": [self._vm(1, "lento"), self._vm(2, "mtu")]}, {"n1": [False, False]})
        self.assertEqual("mtu", culpables["n1"]["regla"])
        self.assertEqual({}, pendiente)

    def test_cortado_sigue_exigiendo_dos_vms_sin_pasar_por_pendiente(self):
        culpables, pendiente = eg.clasifica_culpables(
            {"n1": [self._vm(1, "cortado")]}, {"n1": [False]})
        self.assertEqual({}, culpables, "una sola VM cortada sigue sin bastar")
        self.assertEqual({}, pendiente, "'cortado' no pasa por el buzon de 'lento'")
        culpables, pendiente = eg.clasifica_culpables(
            {"n1": [self._vm(1, "cortado"), self._vm(2, "cortado")]}, {"n1": [False, False]})
        self.assertEqual("varias_vms", culpables["n1"]["regla"])


class LentoConfirmadoEntrePasadas(unittest.TestCase):
    AHORA = datetime(2026, 9, 13, 13, 0, tzinfo=timezone.utc)

    def test_sin_watch_previo_no_confirma(self):
        self.assertFalse(eg.lento_confirmado(None, self.AHORA))

    def test_watch_de_la_pasada_anterior_confirma(self):
        # Vista hace 1h (pasadas horarias): dentro del TTL, confirma.
        wd = {"createdAt": self.AHORA - timedelta(hours=1)}
        self.assertTrue(eg.lento_confirmado(wd, self.AHORA))

    def test_watch_viejo_fuera_de_ttl_no_confirma(self):
        # Un hipo de hace dias no debe resucitar como confirmado.
        wd = {"createdAt": self.AHORA - timedelta(hours=eg.LENTO_WATCH_TTL_H + 1)}
        self.assertFalse(eg.lento_confirmado(wd, self.AHORA))

    def test_ttl_cubre_una_pasada_perdida(self):
        # Cada base pasa una vez por hora: el TTL tiene que ser mayor que
        # una pasada, para no perder la confirmacion si una se retrasa.
        self.assertGreater(eg.LENTO_WATCH_TTL_H, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
