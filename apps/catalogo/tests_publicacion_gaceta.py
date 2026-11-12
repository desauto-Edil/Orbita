"""Publicación a interesados decidida por el aprobador, fecha límite por día del periodo y contratos de las demos
nuevas (requerimiento del 2026-11-11)."""

from datetime import date

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import timezone

from apps.catalogo import actividades
from apps.catalogo.demo import contrato
from apps.catalogo.gacetas import eleccion_de_publicacion, publicacion_de


class EleccionDePublicacionTests(SimpleTestCase):
    CFG = {"publicacion": {"gaceta": "publicacion_a_interesados", "dias_maximos": 2}}

    def test_sin_opcion_en_el_bloque_no_hay_eleccion(self):
        self.assertIsNone(publicacion_de({}))
        self.assertIsNone(eleccion_de_publicacion({}, publicar=True, dias=1))

    def test_publicar_exige_dias_dentro_del_maximo(self):
        self.assertEqual(eleccion_de_publicacion(self.CFG, publicar=True, dias="2"), {"publicar": True, "dias": 2})
        self.assertEqual(eleccion_de_publicacion(self.CFG, publicar=False, dias=None), {"publicar": False, "dias": None})
        for dias in (0, 3, "", None):
            with self.assertRaises(ValidationError):
                eleccion_de_publicacion(self.CFG, publicar=True, dias=dias)


class PublicarEsAprobarTests(SimpleTestCase):
    """2026-11-12: con «Publicar a los interesados» marcado solo se puede aprobar, y a una gaceta por ítem solo entra
    lo que llega por su recorrido."""

    def test_publicar_con_otra_decision_se_rechaza(self):
        from apps.workflows.integracion import _eleccion_de_publicacion

        for decision in ("DEVUELTA", "RECHAZADA", "EXCLUIDA"):
            with self.assertRaisesMessage(ValidationError, "solo se puede al aprobar"):
                _eleccion_de_publicacion(None, decision, {"publicar": True, "dias": 1})
            self.assertIsNone(_eleccion_de_publicacion(None, decision, {"publicar": False}))

    def test_una_gaceta_por_item_no_ofrece_presentar_a_mano(self):
        from types import SimpleNamespace
        from unittest import mock

        from apps.gacetas import consultas
        from apps.gacetas.models import GacetaTicket

        gaceta = SimpleNamespace(estado=GacetaTicket.Estado.ABIERTA)
        with mock.patch("apps.gacetas.integracion.llegadas_pendientes", return_value=SimpleNamespace(usada_por_item=True)):
            self.assertTrue(consultas.se_llena_por_el_flujo(gaceta))
            self.assertEqual(consultas.presentables(gaceta), [])


class FechaLimiteDelPeriodoTests(SimpleTestCase):
    def test_el_dia_del_periodo_fija_el_final_de_ese_dia(self):
        cfg = {"fecha_limite_dia_del_periodo": 20}
        _prevista, limite = actividades.fechas_desde(cfg, timezone.now(), periodo_inicio=date(2026, 11, 1))
        local = timezone.localtime(limite)
        self.assertEqual((local.year, local.month, local.day, local.hour), (2026, 11, 20, 23))

    def test_sin_periodo_no_inventa_fecha_y_el_dia_31_es_el_ultimo_del_mes(self):
        cfg = {"fecha_limite_dia_del_periodo": 31}
        self.assertIsNone(actividades.fechas_desde(cfg, timezone.now())[1])
        limite = actividades.fechas_desde(cfg, timezone.now(), periodo_inicio=date(2026, 11, 1))[1]
        self.assertEqual(timezone.localtime(limite).day, 30)

    def test_no_se_combina_con_un_plazo(self):
        _datos, errores = actividades.normalizar(
            {"fecha_limite_dia_del_periodo": 20, "plazo_limite": {"cantidad": 2, "unidad": "DIAS"}}
        )
        self.assertTrue(errores)


class ContratosDemosNuevasTests(SimpleTestCase):
    def test_solar_andina_y_diseno_estan_en_el_contrato(self):
        solar = contrato.CONTRATOS[contrato.SOLAR_ANDINA]
        self.assertEqual(solar.calendario, (2, "MES_ACTUAL"))
        columnas = {c.clave: c for c in solar.columnas}
        self.assertEqual(columnas[contrato.MEDIOS_PUBLICACION].tipo, "MULTILISTA")
        self.assertEqual([v for v, _ in columnas[contrato.TIPO_PIEZA].opciones], ["blog", "reel", "post", "carrusel"])
        diseno = contrato.CONTRATOS[contrato.DISENO]
        self.assertEqual(diseno.nombre, "Diseño de nuevas soluciones")
        self.assertEqual([g.nombre for g in diseno.gacetas_extra], [contrato.PUBLICACION_INTERESADOS])
        nombres = [b.nombre for b in diseno.bloques]
        i = nombres.index(contrato.APROBACION_RESULTADO)
        self.assertEqual(nombres[i + 1:i + 3], [contrato.DECISION_PUBLICAR, contrato.PUBLICACION_INTERESADOS])
        self.assertEqual(contrato.ALIAS["solar"], contrato.SOLAR_ANDINA)
        self.assertEqual(contrato.ALIAS["diseno"], contrato.DISENO)
