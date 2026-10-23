"""Pruebas de la Fase 4.A en gacetas y dependencias del Proceso.

- Coordinador propio por gaceta (persona o equipo): autoriza a quien está configurado y solo a él; sin coordinador
  se conserva la política de siempre (quien atiende el ticket). Las gacetas ya congeladas no cambian.
- Gaceta obligatoria/opcional para finalizar: una obligatoria que nunca se abrió impide resolver; una opcional no.
- Dependencias entre secciones: gaceta abierta por un bloque inexistente, ingreso al terminar un recorrido que no
  existe y tareas por elemento sin su paso BLOQUEAN activar/publicar; una revisión sin paso solo ADVIERTE.
- Errores de gacetas agrupados en Salida; coherencia de las opciones «Generar un resultado» y «Revisar elementos».

Reutilizan el escenario de la Fase 3 (`apps.gacetas.tests._EscenarioGacetasMixin`). Se entregan SIN ejecutar."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.catalogo import dependencias, intenciones
from apps.catalogo.entregables import configurar_definicion_entregable
from apps.catalogo.experiencia_proceso import errores_por_seccion
from apps.catalogo.gacetas import configurar_gaceta, errores_de_gacetas
from apps.catalogo.tareas_elemento import configurar_tareas_elemento
from apps.core.models import Equipo, MiembroEquipo
from apps.gacetas import autorizacion, consultas, operaciones
from apps.gacetas.models import GacetaTicket
from apps.gacetas.tests import _EscenarioGacetasMixin
from apps.tickets import finalizacion
from apps.tickets.tests import CLAVE_PRUEBA, Usuario


class CoordinadorDeGacetaTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.coordinadora = Usuario.objects.create_user(username="f4a_coordinadora", password=CLAVE_PRUEBA)
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()

    def _con_coordinador(self, coordinador):
        regla = self._gaceta(self.servicio, self.definicion, coordinador=coordinador)
        self._activar_flujo(self.servicio)
        ticket = self._ticket(self.servicio, self.resp)
        return regla, ticket, self._de(ticket, regla.clave)

    def test_un_coordinador_persona_coordina_y_quien_atiende_ya_no(self):
        _regla, ticket, gaceta = self._con_coordinador({"tipo": "USUARIO", "usuario_id": self.coordinadora.pk})
        banner = self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.assertTrue(autorizacion.puede_coordinar(self.coordinadora, gaceta))
        self.assertFalse(autorizacion.puede_coordinar(self.analista, gaceta))
        with self.assertRaises(PermissionDenied):
            operaciones.presentar(gaceta, banner, self.analista)
        participacion = operaciones.presentar(gaceta, banner, self.coordinadora)
        self.assertEqual(participacion.elemento_id, banner.pk)
        # Ve la gaceta (sin acceso al resto del ticket) y la encuentra en su lista.
        self.assertTrue(autorizacion.puede_consultar_gaceta(self.coordinadora, gaceta))
        self.assertIn(gaceta, consultas.gacetas_visibles_para(self.coordinadora))

    def test_un_coordinador_equipo_coordina_por_sus_miembros_activos(self):
        equipo = Equipo.objects.create(nombre="Comité editorial 4A")
        MiembroEquipo.objects.create(equipo=equipo, usuario=self.coordinadora, activo=True)
        _regla, _ticket, gaceta = self._con_coordinador({"tipo": "EQUIPO", "equipo_id": equipo.pk})
        self.assertTrue(autorizacion.puede_coordinar(self.coordinadora, gaceta))
        self.assertFalse(autorizacion.puede_coordinar(self.ajeno, gaceta))
        MiembroEquipo.objects.filter(equipo=equipo).update(activo=False)
        self.assertFalse(autorizacion.puede_coordinar(self.coordinadora, gaceta))

    def test_sin_coordinador_se_conserva_la_politica_de_siempre(self):
        _regla, _ticket, gaceta = self._con_coordinador(None)
        self.assertIsNone(gaceta.configuracion["coordinador"])
        self.assertTrue(autorizacion.puede_coordinar(self.analista, gaceta))
        self.assertFalse(autorizacion.puede_coordinar(self.coordinadora, gaceta))

    def test_una_gaceta_congelada_antes_de_4a_sigue_igual(self):
        _regla, _ticket, gaceta = self._con_coordinador(None)
        configuracion = dict(gaceta.configuracion)
        configuracion.pop("coordinador", None)
        configuracion.pop("obligatoria", None)
        GacetaTicket.objects.filter(pk=gaceta.pk).update(configuracion=configuracion)
        gaceta.refresh_from_db()
        self.assertTrue(autorizacion.puede_coordinar(self.analista, gaceta))

    def test_cambiar_el_coordinador_en_studio_no_altera_tickets_existentes(self):
        regla, ticket, gaceta = self._con_coordinador(None)
        configurar_gaceta(
            self.servicio, self.admin, nombre=regla.nombre, definicion=regla,
            configuracion={**regla.configuracion, "coordinador": {"tipo": "USUARIO", "usuario_id": self.coordinadora.pk}},
        )
        gaceta.refresh_from_db()
        self.assertIsNone(gaceta.configuracion["coordinador"])
        self.assertFalse(autorizacion.puede_coordinar(self.coordinadora, gaceta))

    def test_un_coordinador_inactivo_se_rechaza(self):
        self.coordinadora.is_active = False
        self.coordinadora.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self._gaceta(self.servicio, self.definicion, coordinador={"tipo": "USUARIO", "usuario_id": self.coordinadora.pk})


class GacetaObligatoriaTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()

    def _regla_gacetas(self, ticket):
        return next(r for r in finalizacion.evaluar(ticket) if r.codigo == "gacetas_resueltas")

    def _ticket_con_gaceta_sin_abrir(self, obligatoria):
        regla = self._gaceta(self.servicio, self.definicion, apertura="POR_FLUJO", obligatoria=obligatoria)
        # El bloque que la abriría está en una fase que el ticket aún no alcanza.
        self._activar_flujo(self.servicio, bloque_gaceta={"gaceta": regla.clave, "presentar_items": True, "esperar": "NINGUNA"})
        ticket = self._ticket(self.servicio, self.resp)
        self.assertEqual(self._de(ticket, regla.clave).estado, GacetaTicket.Estado.PENDIENTE)
        return ticket

    def test_una_gaceta_obligatoria_que_nunca_se_abrio_impide_resolver(self):
        ticket = self._ticket_con_gaceta_sin_abrir(True)
        regla = self._regla_gacetas(ticket)
        self.assertFalse(regla.cumple)
        self.assertIn("Gaceta de piezas", regla.detalle)

    def test_una_gaceta_opcional_no_bloquea_solo_por_existir(self):
        ticket = self._ticket_con_gaceta_sin_abrir(False)
        self.assertTrue(self._regla_gacetas(ticket).cumple)

    def test_una_configuracion_congelada_sin_la_clave_es_opcional(self):
        ticket = self._ticket_con_gaceta_sin_abrir(True)
        gaceta = GacetaTicket.objects.get(ticket=ticket)
        configuracion = dict(gaceta.configuracion)
        configuracion.pop("obligatoria")
        GacetaTicket.objects.filter(pk=gaceta.pk).update(configuracion=configuracion)
        self.assertTrue(self._regla_gacetas(ticket).cumple)


class DependenciasDelProcesoTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()

    def test_gaceta_abierta_por_un_bloque_inexistente_bloquea_activar(self):
        regla = self._gaceta(self.servicio, self.definicion, apertura="POR_FLUJO")
        with self.assertRaises(ValidationError) as error:
            self._activar_flujo(self.servicio)
        self.assertIn(regla.nombre, " ".join(error.exception.messages))
        self._activar_flujo(self.servicio, bloque_gaceta={"gaceta": regla.clave, "presentar_items": True, "esperar": "RESOLUCION"})
        self.servicio.refresh_from_db()
        # (El primer intento dejó un borrador sin el bloque: se evalúa la configuración que quedó activa.)
        self.assertEqual(dependencias.bloqueantes(self.servicio, self.servicio.configuracion_ejecucion_activa), [])

    def test_ingreso_al_terminar_un_recorrido_inexistente_bloquea(self):
        self._gaceta(self.servicio, self.definicion, ingreso="AL_TERMINAR_RECORRIDO")
        with self.assertRaises(ValidationError):
            self._activar_flujo(self.servicio)

    def test_tareas_por_elemento_sin_su_paso_bloquean_y_se_ofrece_agregarlo(self):
        configurar_tareas_elemento(
            self.servicio, self.admin, clave_titulo=self.columnas["Pieza"].clave,
            clave_responsable=self.columnas["Responsable"].clave, clave_fecha_limite=self.columnas["Entrega"].clave,
        )
        detectadas = dependencias.detectar(self.servicio)
        tareas = next(d for d in detectadas if d.codigo == "tareas_sin_paso")
        self.assertTrue(tareas.bloquea)
        self.assertEqual(tareas.agregar_opcion, "tareas_elementos")
        with self.assertRaises(ValidationError):
            self._activar_flujo(self.servicio)

    def test_una_revision_sin_paso_solo_advierte(self):
        configurar_definicion_entregable(
            self.servicio, self.admin, nombre="Piezas aprobadas", tipo="ELEMENTOS", obligatorio=True,
            columnas_revisadas=[self.columnas["Pieza"].clave], revisores_usuarios=[self.revisor],
        )
        self._activar_flujo(self.servicio)  # no bloquea
        revision = next(d for d in dependencias.detectar(self.servicio) if d.codigo.startswith("revision_sin_paso"))
        self.assertFalse(revision.bloquea)
        self.assertEqual(revision.agregar_opcion, "revision_conjunto")

    def test_errores_de_gacetas_se_corrigen_en_salida(self):
        self._gaceta(self.servicio, self.definicion)
        self.revisor.is_active = False
        self.revisor.save(update_fields=["is_active"])
        mensajes = errores_de_gacetas(self.servicio)
        self.assertTrue(mensajes)
        grupos = errores_por_seccion(self.servicio, mensajes)
        self.assertEqual([g["seccion"] for g in grupos], ["salida"])

    def test_resultado_y_revision_son_opciones_coherentes(self):
        definicion = configurar_definicion_entregable(
            self.servicio, self.admin, nombre="Piezas", tipo="ELEMENTOS", obligatorio=False,
            columnas_revisadas=[self.columnas["Pieza"].clave], revisores_usuarios=[self.revisor],
        )
        with self.assertRaises(ValidationError):
            intenciones.exigir_definicion_coherente("resultado", definicion)
        intenciones.exigir_definicion_coherente("revision_conjunto", definicion)
        grupo = next(g for g in intenciones.opciones_para(self.servicio) if g["intencion"].codigo == "coleccion")
        disponibles = {o["opcion"].codigo: o["disponible"] for o in grupo["opciones"]}
        self.assertTrue(disponibles["revision_conjunto"])
        self.assertTrue(disponibles["recorrido"])
        self.assertFalse(disponibles["tareas_elementos"])  # aún no hay tareas por elemento configuradas

    def test_la_opcion_gaceta_requiere_una_gaceta_activa(self):
        grupo = next(g for g in intenciones.opciones_para(self.servicio) if g["intencion"].codigo == "colaborativa")
        self.assertFalse(grupo["opciones"][0]["disponible"])
        self._gaceta(self.servicio, self.definicion)
        grupo = next(g for g in intenciones.opciones_para(self.servicio) if g["intencion"].codigo == "colaborativa")
        self.assertTrue(grupo["opciones"][0]["disponible"])
