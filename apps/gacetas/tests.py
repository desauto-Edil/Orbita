"""Pruebas de la Fase 3 del modelo configurable — gacetas y finalización.

Reutilizan el escenario de la Fase 2 (`apps.tickets.tests._EscenarioRecorridosMixin`: Proceso programado con una
colección de ítems «Publicación» con columnas Pieza, Responsable, Equipo y Entrega)."""

import threading
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.aprobaciones.models import Aprobacion
from apps.catalogo.gacetas import configurar_gaceta, configurar_reglas_finalizacion
from apps.catalogo.models import BloqueOperativo, Servicio, ServicioResponsable
from apps.gacetas import operaciones, tasks
from apps.gacetas.models import GacetaTicket, ObservacionGaceta, ParticipacionItemGaceta, RondaRevisionGaceta
from apps.tareas.models import Tarea
from apps.tickets import parrilla as parrilla_ops
from apps.tickets.models import DecisionAlcanceElemento, ResolucionTicket, Ticket
from apps.tickets.operaciones import cancelar_ticket, resolver_ticket
from apps.tickets.tests import CLAVE_PRUEBA, Usuario, _crear_servicio_con_formulario, _EscenarioRecorridosMixin
from apps.workflows.integracion import completar_tarea_workflow
from apps.workflows.models import InstanciaWorkflow, TareaWorkflow

Estado = ParticipacionItemGaceta.Estado


class _EscenarioGacetasMixin(_EscenarioRecorridosMixin):
    def _preparar_gacetas(self):
        self._preparar_recorridos()
        self.observador = Usuario.objects.create_user(username="f3_observador", password=CLAVE_PRUEBA)

    def _proceso_simple(self, *, bloque_gaceta=None):
        """Proceso: Planear → (bloque GACETA opcional) → Cierre, con la colección de la Fase 2. Las gacetas se
        definen con `_gaceta` ANTES de `_activar_flujo` (un bloque GACETA exige una gaceta activa)."""
        servicio, _, _ = _crear_servicio_con_formulario(self.admin, [])
        servicio.tipo = "PROCESO"
        servicio.workflow = self.workflow_e2
        servicio.save(update_fields=["tipo", "workflow", "actualizado_en"])
        responsable = ServicioResponsable.objects.create(servicio=servicio, tipo_responsable="EQUIPO", equipo=self.analitica)
        definicion, columnas = self._definir_parrilla(servicio, self.COLUMNAS_F2)
        return servicio, responsable, definicion, columnas

    def _activar_flujo(self, servicio, *, bloque_gaceta=None):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion

        config = crear_nueva_version_configuracion(servicio, self.admin)
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_recepcion, tipo="ACTIVIDAD", nombre="Planear",
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.analista.pk},
        )
        if bloque_gaceta is not None:
            self.bloque_gaceta = agregar_bloque_operativo(
                config, self.admin, fase=self.f_produccion, tipo="GACETA", nombre="Revisión en gaceta",
                configuracion=bloque_gaceta,
            )
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_entrega, tipo="ACTIVIDAD", nombre="Cierre",
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.analista.pk},
        )
        self._activar(servicio, config)

    def _gaceta(self, servicio, definicion, nombre="Gaceta de piezas", **cfg):
        configuracion = {
            "colecciones": [definicion.clave],
            "aprobadores": [{"tipo": "USUARIO", "usuario_id": self.revisor.pk}],
            "revisores": [{"tipo": "USUARIO", "usuario_id": self.observador.pk}],
            "responsable_correccion_columna": "responsable",
            "plazo_correccion": {"cantidad": 2, "unidad": "DIAS", "habiles": False},
            **cfg,
        }
        return configurar_gaceta(servicio, self.admin, nombre=nombre, configuracion=configuracion)

    def _de(self, ticket, clave):
        return GacetaTicket.objects.get(ticket=ticket, clave=clave)

    def _participacion(self, gaceta, elemento):
        return ParticipacionItemGaceta.objects.get(gaceta=gaceta, elemento=elemento)

    def _aprobacion(self, participacion):
        ronda = RondaRevisionGaceta.objects.get(participacion=participacion, numero=participacion.ronda)
        return ronda.esquema.participaciones.get()

    def _decidir(self, participacion, decision, observacion=""):
        operaciones.resolver_aprobacion_gaceta(
            self._aprobacion(participacion), self.revisor, decision=decision, observacion=observacion
        )
        participacion.refresh_from_db()


class IngresoYMultiplesGacetasTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.def_a = self._gaceta(self.servicio, self.definicion, "Gaceta comercial")
        self.def_b = self._gaceta(self.servicio, self.definicion, "Gaceta legal")
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)

    def test_el_ticket_congela_sus_gacetas_y_se_abren_al_iniciar(self):
        gacetas = list(GacetaTicket.objects.filter(ticket=self.ticket).order_by("pk"))
        self.assertEqual([g.nombre for g in gacetas], ["Gaceta comercial", "Gaceta legal"])
        self.assertTrue(all(g.estado == GacetaTicket.Estado.ABIERTA for g in gacetas))
        # Cambiar la definición después no altera el ticket.
        configurar_gaceta(
            self.servicio, self.admin, nombre="Gaceta comercial renombrada", definicion=self.def_a,
            configuracion={**self.def_a.configuracion, "permite_exclusion": True},
        )
        gaceta = self._de(self.ticket, self.def_a.clave)
        self.assertEqual(gaceta.nombre, "Gaceta comercial")
        self.assertFalse(gaceta.configuracion["permite_exclusion"])
        self.assertEqual(gaceta.version_definicion, 1)

    def test_presentar_es_idempotente_y_registra_version_y_esquema(self):
        gaceta = self._de(self.ticket, self.def_a.clave)
        primera = operaciones.presentar(gaceta, self.banner, self.analista)
        segunda = operaciones.presentar(gaceta, self.banner, self.analista)
        self.assertEqual(primera.pk, segunda.pk)
        self.assertEqual(ParticipacionItemGaceta.objects.filter(gaceta=gaceta).count(), 1)
        ronda = primera.rondas.get()
        self.assertEqual((ronda.numero, ronda.version.numero), (1, 1))
        self.assertEqual(ronda.esquema.participaciones.get().aprobador_usuario, self.revisor)
        self.assertEqual(primera.estado, Estado.EN_REVISION)

    def test_cada_gaceta_decide_por_su_cuenta(self):
        a = operaciones.presentar(self._de(self.ticket, self.def_a.clave), self.banner, self.analista)
        b = operaciones.presentar(self._de(self.ticket, self.def_b.clave), self.banner, self.analista)
        self._decidir(a, "APROBADA")
        b.refresh_from_db()
        self.assertEqual((a.estado, b.estado), (Estado.APROBADA, Estado.EN_REVISION))

    def test_solo_el_coordinador_presenta_y_solo_items_validos(self):
        gaceta = self._de(self.ticket, self.def_a.clave)
        with self.assertRaises(PermissionDenied):
            operaciones.presentar(gaceta, self.banner, self.ajeno)
        parrilla_ops.excluir_elemento(self.banner, self.analista, "No va")
        with self.assertRaises(ValidationError):
            operaciones.presentar(gaceta, self.banner, self.analista)


class IngresoAutomaticoTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()

    def test_ingreso_progresivo_al_cumplir_la_condicion(self):
        servicio, resp, definicion, columnas = self._proceso_simple()
        regla = self._gaceta(
            servicio, definicion, ingreso="AL_CUMPLIR_CONDICION",
            condicion_ingreso={"variable": "item.pieza", "operador": "IGUAL_A", "valor": "Banner"},
        )
        self._activar_flujo(servicio)
        ticket = self._ticket(servicio, resp)
        gaceta = self._de(ticket, regla.clave)
        video = self._item(ticket, columnas, "Video", responsable=self.disenadora)
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta=gaceta).exists())
        parrilla_ops.actualizar_valores(video, self.analista, {columnas["Pieza"].pk: "Banner"})
        self.assertEqual(self._participacion(gaceta, video).origen, ParticipacionItemGaceta.Origen.AUTOMATICO)
        parrilla_ops.actualizar_valores(video, self.analista, {columnas["Entrega"].pk: "2026-12-01"})
        self.assertEqual(ParticipacionItemGaceta.objects.filter(gaceta=gaceta).count(), 1)  # no se duplica

    def test_un_item_que_termina_su_recorrido_ingresa_y_sigue_en_revision(self):
        servicio, resp, definicion, columnas = self._proceso_recorrido()
        regla = self._gaceta(servicio, definicion, ingreso="AL_TERMINAR_RECORRIDO")
        ticket = self._ticket(servicio, resp)
        banner = self._item(ticket, columnas, "Banner", responsable=self.disenadora)
        video = self._item(ticket, columnas, "Video", responsable=self.disenador)
        self._completar_como(ticket, "Planear", self.analista)
        gaceta = self._de(ticket, regla.clave)
        completar_tarea_workflow(self._tarea_de(self._hija(banner)), self.disenadora)
        from apps.workflows.integracion import resolver_aprobacion_workflow

        resolver_aprobacion_workflow(self._aprobacion_de(self._hija(banner)), self.revisor, decision="APROBADA")
        self.assertEqual(self._hija(banner).estado, InstanciaWorkflow.Estado.COMPLETADA)
        participacion = self._participacion(gaceta, banner)
        self.assertEqual(participacion.estado, Estado.EN_REVISION)  # operativamente terminado, en revisión
        self.assertEqual(operaciones.estado_operativo(banner), "Completado")
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta=gaceta, elemento=video).exists())


class ObservacionesYCorreccionesTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.video = self._item(self.ticket, self.columnas, "Video", responsable=self.disenador)
        self.p_banner = operaciones.presentar(self.gaceta, self.banner, self.analista)
        self.p_video = operaciones.presentar(self.gaceta, self.video, self.analista)

    def test_una_observacion_devuelve_el_item_a_su_responsable(self):
        esquema = self.p_banner.rondas.get().esquema
        operaciones.observar(self.p_banner, self.observador, "Falta el logo")
        self.p_banner.refresh_from_db()
        self.assertEqual(self.p_banner.estado, Estado.EN_CORRECCION)
        self.assertIsNotNone(self.p_banner.vence_correccion_en)
        ronda = self.p_banner.rondas.get(numero=1)
        self.assertEqual(ronda.resultado, RondaRevisionGaceta.Resultado.OBSERVADA)
        self.assertEqual(ronda.tarea_correccion.usuario_responsable, self.disenadora)
        self.assertIn("Falta el logo", ronda.tarea_correccion.descripcion)
        self.assertEqual(esquema.participaciones.get().estado, Aprobacion.Estado.NO_REQUERIDA)
        self.p_video.refresh_from_db()
        self.assertEqual(self.p_video.estado, Estado.EN_REVISION)  # los demás no cambian

    def test_corregir_y_reenviar_crea_una_version_nueva_en_la_misma_gaceta(self):
        operaciones.observar(self.p_banner, self.observador, "Falta el logo")
        observacion = ObservacionGaceta.objects.get(participacion=self.p_banner)
        operaciones.responder_observacion(observacion, self.disenadora, "Agregado en la versión 2")
        parrilla_ops.actualizar_valores(self.banner, self.analista, {self.columnas["Pieza"].pk: "Banner con logo"})
        operaciones.reenviar(self.p_banner, self.disenadora, "Listo")
        self.p_banner.refresh_from_db()
        self.assertEqual((self.p_banner.estado, self.p_banner.ronda), (Estado.EN_REVISION, 2))
        rondas = list(self.p_banner.rondas.order_by("numero"))
        self.assertEqual([r.version.numero for r in rondas], [1, 2])  # la versión anterior se conserva
        self.assertEqual(rondas[0].tarea_correccion.estado, Tarea.Estado.COMPLETADA)
        observacion.refresh_from_db()
        self.assertIsNotNone(observacion.atendida_en)
        self.assertEqual(observacion.respuesta, "Agregado en la versión 2")
        self.assertEqual(GacetaTicket.objects.filter(ticket=self.ticket).count(), 1)  # no se crea otra gaceta

    def test_completar_la_tarea_de_correccion_es_reenviar(self):
        operaciones.observar(self.p_banner, self.observador, "Ajustar tamaño")
        tarea = self.p_banner.rondas.get(numero=1).tarea_correccion
        self._entrar(self.disenadora)
        self.client.post(reverse("tareas:completar", args=[tarea.pk]))
        self.p_banner.refresh_from_db()
        self.assertEqual((self.p_banner.estado, self.p_banner.ronda), (Estado.EN_REVISION, 2))

    def test_solo_el_responsable_de_la_correccion_reenvia(self):
        operaciones.observar(self.p_banner, self.observador, "Ajustar tamaño")
        with self.assertRaises(PermissionDenied):
            operaciones.reenviar(self.p_banner, self.disenador)

    def test_devolver_por_aprobacion_registra_la_observacion_del_aprobador(self):
        self._decidir(self.p_banner, "DEVUELTA", "Cambiar el copy")
        self.assertEqual(self.p_banner.estado, Estado.EN_CORRECCION)
        observacion = ObservacionGaceta.objects.get(participacion=self.p_banner)
        self.assertEqual((observacion.origen, observacion.autor, observacion.texto), ("DECISION", self.revisor, "Cambiar el copy"))

    def test_quien_no_participa_no_observa_ni_despues_del_cierre(self):
        with self.assertRaises(PermissionDenied):
            operaciones.observar(self.p_banner, self.ajeno, "Opino")
        operaciones.cerrar_periodo(self.gaceta, self.analista)
        with self.assertRaises(PermissionDenied):
            operaciones.observar(self.p_banner, self.observador, "Tarde")


class DecisionesTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion, permite_exclusion=True)
        self.otra = self._gaceta(self.servicio, self.definicion, "Gaceta legal")
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.p = operaciones.presentar(self.gaceta, self.banner, self.analista)
        self.p_otra = operaciones.presentar(self._de(self.ticket, self.otra.clave), self.banner, self.analista)

    def test_rechazar_saca_el_item_del_alcance_y_cierra_sus_otras_gacetas(self):
        self._decidir(self.p, "RECHAZADA", "No cumple la marca")
        self.assertEqual(self.p.estado, Estado.RECHAZADA)
        decision = parrilla_ops.decision_de_salida(self.banner)
        self.assertEqual((decision.tipo, decision.origen), ("RECHAZADO", DecisionAlcanceElemento.Origen.GACETA))
        self.p_otra.refresh_from_db()
        self.assertEqual(self.p_otra.estado, Estado.RECHAZADA)
        self.assertTrue(self.banner.__class__.objects.filter(pk=self.banner.pk).exists())  # nunca se borra

    def test_excluir_es_distinto_de_rechazar_y_exige_justificacion(self):
        with self.assertRaises(ValidationError):
            self._decidir(self.p, "EXCLUIDA", "")
        self._decidir(self.p, "EXCLUIDA", "Se cayó la pauta")
        self.assertEqual(self.p.estado, Estado.EXCLUIDA)
        self.assertEqual(parrilla_ops.decision_de_salida(self.banner).tipo, "EXCLUIDO")

    def test_una_gaceta_sin_exclusion_no_la_ofrece(self):
        with self.assertRaises(ValidationError):
            operaciones.resolver_aprobacion_gaceta(
                self._aprobacion(self.p_otra), self.revisor, decision="EXCLUIDA", observacion="x"
            )

    def test_el_coordinador_excluye_sin_otra_aprobacion(self):
        with self.assertRaises(PermissionDenied):
            operaciones.excluir_directo(self.p, self.observador, "No va")
        operaciones.excluir_directo(self.p, self.analista, "No va")
        self.p.refresh_from_db()
        self.assertEqual(self.p.estado, Estado.EXCLUIDA)
        self.assertEqual(self._aprobacion(self.p).estado, Aprobacion.Estado.NO_REQUERIDA)

    def test_una_decision_repetida_no_cambia_el_resultado(self):
        self._decidir(self.p, "APROBADA")
        with self.assertRaises(ValidationError):
            self._decidir(self.p, "RECHAZADA", "Tarde")
        self.assertEqual(self.p.estado, Estado.APROBADA)
        self.assertIsNone(parrilla_ops.decision_de_salida(self.banner))

    def test_la_aprobacion_muestra_el_item_y_su_gaceta(self):
        self._entrar(self.revisor)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[self._aprobacion(self.p).pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["item_aprobado"]["gaceta"], self.gaceta)
        self.assertContains(respuesta, "Banner")


class VencimientosYResolucionTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()

    def _escenario(self, **cfg):
        regla = self._gaceta(
            self.servicio, self.definicion, plazo_revision={"cantidad": 1, "unidad": "DIAS", "habiles": False}, **cfg
        )
        self._activar_flujo(self.servicio)
        ticket = self._ticket(self.servicio, self.resp)
        gaceta = self._de(ticket, regla.clave)
        banner = self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        video = self._item(ticket, self.columnas, "Video", responsable=self.disenador)
        p_banner = operaciones.presentar(gaceta, banner, self.analista)
        p_video = operaciones.presentar(gaceta, video, self.analista)
        return ticket, gaceta, p_banner, p_video

    def _vencer(self, gaceta):
        GacetaTicket.objects.filter(pk=gaceta.pk).update(vence_revision_en=timezone.now() - timedelta(minutes=1))

    def test_aceptacion_automatica_solo_sin_observaciones_ni_correcciones(self):
        ticket, gaceta, p_banner, p_video = self._escenario(aceptacion_automatica=True)
        operaciones.observar(p_video, self.observador, "Corregir")
        self._vencer(gaceta)
        self.assertGreater(tasks.reconciliar(), 0)
        gaceta.refresh_from_db()
        p_banner.refresh_from_db()
        p_video.refresh_from_db()
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.PERIODO_CERRADO)
        self.assertEqual((p_banner.estado, p_banner.aceptada_automaticamente), (Estado.APROBADA, True))
        self.assertEqual(p_video.estado, Estado.EN_CORRECCION)  # nunca se acepta un ítem en corrección
        self.assertEqual(tasks.reconciliar(), 0)  # idempotente

    def test_cerrar_el_periodo_no_aprueba_lo_pendiente(self):
        ticket, gaceta, p_banner, p_video = self._escenario()
        operaciones.cerrar_periodo(gaceta, self.analista)
        p_banner.refresh_from_db()
        self.assertEqual(p_banner.estado, Estado.EN_REVISION)
        # Los aprobadores pueden seguir decidiendo con el periodo cerrado.
        self._decidir(p_banner, "APROBADA")
        self.assertEqual(p_banner.estado, Estado.APROBADA)

    def test_resolver_con_pendientes_segun_la_regla(self):
        ticket, gaceta, p_banner, p_video = self._escenario()
        self._decidir(p_banner, "APROBADA")
        with self.assertRaises(ValidationError):
            operaciones.resolver(gaceta, self.analista)  # IMPEDIR por defecto
        GacetaTicket.objects.filter(pk=gaceta.pk).update(
            configuracion={**gaceta.configuracion, "al_resolver_pendientes": "EXCLUIR"}
        )
        operaciones.resolver(gaceta, self.analista, "Fin del ciclo")
        gaceta.refresh_from_db()
        p_video.refresh_from_db()
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.RESUELTA)
        self.assertEqual(p_video.estado, Estado.EXCLUIDA)
        self.assertEqual(parrilla_ops.decision_de_salida(p_video.elemento).origen, "GACETA")
        self.assertEqual((gaceta.resolucion["aprobados"], gaceta.resolucion["excluidos"]), (1, 1))

    def test_resolucion_automatica_al_cerrar_sin_pendientes(self):
        ticket, gaceta, p_banner, p_video = self._escenario(resolucion_automatica=True)
        self._decidir(p_banner, "APROBADA")
        self._decidir(p_video, "RECHAZADA", "No aplica")
        self._vencer(gaceta)
        tasks.reconciliar()
        gaceta.refresh_from_db()
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.RESUELTA)
        self.assertTrue(gaceta.resolucion["automatica"])

    def test_la_correccion_vencida_se_registra(self):
        ticket, gaceta, p_banner, _ = self._escenario()
        operaciones.observar(p_banner, self.observador, "Corregir")
        ParticipacionItemGaceta.objects.filter(pk=p_banner.pk).update(vence_correccion_en=timezone.now() - timedelta(hours=1))
        tasks.reconciliar()
        p_banner.refresh_from_db()
        self.assertIsNotNone(p_banner.correccion_vencida_en)
        self.assertEqual(p_banner.estado, Estado.EN_CORRECCION)


class GacetaEnElFlujoTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()

    def test_el_bloque_abre_presenta_y_espera_la_resolucion(self):
        regla = self._gaceta(self.servicio, self.definicion, apertura="POR_FLUJO")
        self._activar_flujo(self.servicio, bloque_gaceta={"gaceta": regla.clave, "presentar_items": True, "esperar": "RESOLUCION"})
        ticket = self._ticket(self.servicio, self.resp)
        gaceta = self._de(ticket, regla.clave)
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.PENDIENTE)  # se abre por flujo
        banner = self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        self._completar_como(ticket, "Planear", self.analista)
        gaceta.refresh_from_db()
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.ABIERTA)
        principal = self._principal(ticket)
        self.assertEqual(self._bloque_actual(principal), "Revisión en gaceta")
        p = self._participacion(gaceta, banner)
        self.assertEqual(p.origen, ParticipacionItemGaceta.Origen.FLUJO)
        self._decidir(p, "APROBADA")
        self.assertEqual(self._bloque_actual(principal), "Revisión en gaceta")  # aún sin resolver
        operaciones.resolver(gaceta, self.analista)
        self.assertEqual(self._bloque_actual(principal), "Cierre")
        resultado = principal.contexto["resultados_bloques"]["gacetas"][self.bloque_gaceta.clave]
        self.assertEqual((resultado["aprobados"], resultado["resuelta"]), (1, True))

    def test_sin_esperar_la_gaceta_opera_en_paralelo_con_el_proceso(self):
        regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio, bloque_gaceta={"gaceta": regla.clave, "presentar_items": True, "esperar": "NINGUNA"})
        ticket = self._ticket(self.servicio, self.resp)
        self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        self._completar_como(ticket, "Planear", self.analista)
        self.assertEqual(self._bloque_actual(self._principal(ticket)), "Cierre")
        self.assertEqual(self._de(ticket, regla.clave).participaciones.count(), 1)

    def test_publicar_exige_una_gaceta_activa_en_el_bloque(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion
        from apps.catalogo.configuracion_ejecucion import validar_configuracion_ejecucion

        config = crear_nueva_version_configuracion(self.servicio, self.admin)
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_recepcion, tipo="GACETA", nombre="Sin gaceta", configuracion={"gaceta": "x"},
        )
        self.assertIn("gaceta activa", " ".join(validar_configuracion_ejecucion(config)))

    def test_la_etapa_gaceta_legacy_sigue_sin_ser_ejecutable(self):
        from apps.workflows.estrategias import ESTRATEGIAS_POR_TIPO

        self.assertNotIn("GACETA", ESTRATEGIAS_POR_TIPO)
        self.assertIn("GACETA_BLOQUE", ESTRATEGIAS_POR_TIPO)


class FinalizacionTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion)

    def _ticket_terminado(self):
        self._activar_flujo(self.servicio)
        ticket = self._ticket(self.servicio, self.resp)
        banner = self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        video = self._item(ticket, self.columnas, "Video", responsable=self.disenador)
        self._completar_como(ticket, "Planear", self.analista)
        self._completar_como(ticket, "Cierre", self.analista)
        ticket.refresh_from_db()
        return ticket, banner, video

    def test_no_se_resuelve_con_la_gaceta_sin_resolver_y_se_guarda_la_foto_final(self):
        ticket, banner, video = self._ticket_terminado()
        gaceta = self._de(ticket, self.regla.clave)
        p_banner = operaciones.presentar(gaceta, banner, self.analista)
        p_video = operaciones.presentar(gaceta, video, self.analista)
        with self.assertRaises(ValidationError) as contexto:
            resolver_ticket(ticket, self.analista, "Listo")
        self.assertIn("gacetas", " ".join(contexto.exception.messages).lower())
        self._decidir(p_banner, "APROBADA")
        self._decidir(p_video, "RECHAZADA", "No aplica")
        operaciones.resolver(gaceta, self.analista)
        resolucion = resolver_ticket(ticket, self.analista, "Listo")
        self.assertEqual(resolucion.resumen_resultados["items"], {"APROBADO": [1], "RECHAZADO": [2]})
        self.assertEqual(resolucion.resumen_resultados["gacetas"][0]["estado"], "RESUELTA")

    def test_las_reglas_admiten_items_rechazados_y_exigen_resultado(self):
        configurar_reglas_finalizacion(
            self.servicio, self.admin,
            {"items_con_resultado": True, "resultados_admitidos": ["APROBADO", "RECHAZADO", "EXCLUIDO"], "minimo_aprobados": 1},
        )
        ticket, banner, video = self._ticket_terminado()
        gaceta = self._de(ticket, self.regla.clave)
        from apps.tickets import finalizacion

        pendientes = [r.codigo for r in finalizacion.evaluar(ticket) if not r.cumple]
        self.assertIn("items_con_resultado", pendientes)
        self.assertIn("minimo_aprobados", pendientes)
        p_banner = operaciones.presentar(gaceta, banner, self.analista)
        self._decidir(p_banner, "APROBADA")
        parrilla_ops.excluir_elemento(video, self.analista, "Fuera de la campaña")
        operaciones.resolver(gaceta, self.analista)
        self.assertTrue(all(r.cumple for r in finalizacion.evaluar(ticket)))
        resolver_ticket(ticket, self.analista, "Listo")
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RESUELTO)

    def test_las_reglas_se_congelan_en_el_ticket(self):
        ticket, _banner, _video = self._ticket_terminado()
        configurar_reglas_finalizacion(self.servicio, self.admin, {"alcance_cerrado": True})
        ticket.refresh_from_db()
        self.assertFalse(ticket.reglas_finalizacion["alcance_cerrado"])

    def test_un_proceso_sin_gacetas_ni_reglas_se_resuelve_como_siempre(self):
        servicio, resp, _definicion, columnas = self._proceso_simple()
        self._activar_flujo(servicio)
        ticket = self._ticket(servicio, resp)
        self._completar_como(ticket, "Planear", self.analista)
        self._completar_como(ticket, "Cierre", self.analista)
        ticket.refresh_from_db()
        resolver_ticket(ticket, self.analista, "Listo")
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RESUELTO)


class CancelacionTests(_EscenarioGacetasMixin, TestCase):
    def test_cancelar_el_ticket_cancela_sus_gacetas_sin_borrar_lo_decidido(self):
        self._preparar_gacetas()
        servicio, resp, definicion, columnas = self._proceso_simple()
        regla = self._gaceta(servicio, definicion)
        self._activar_flujo(servicio)
        ticket = self._ticket(servicio, resp)
        gaceta = self._de(ticket, regla.clave)
        banner = self._item(ticket, columnas, "Banner", responsable=self.disenadora)
        video = self._item(ticket, columnas, "Video", responsable=self.disenador)
        p_banner = operaciones.presentar(gaceta, banner, self.analista)
        p_video = operaciones.presentar(gaceta, video, self.analista)
        self._decidir(p_banner, "APROBADA")
        operaciones.observar(p_video, self.observador, "Corregir")
        tarea = p_video.rondas.get(numero=1).tarea_correccion
        cancelar_ticket(ticket, self.analista, "Suspendido")
        gaceta.refresh_from_db()
        tarea.refresh_from_db()
        p_banner.refresh_from_db()
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.CANCELADA)
        self.assertEqual(tarea.estado, Tarea.Estado.CANCELADA)
        self.assertEqual(p_banner.estado, Estado.APROBADA)


class VistasYStudioTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.p = operaciones.presentar(self.gaceta, self.banner, self.analista)

    def test_el_revisor_ve_la_gaceta_sin_acceder_al_ticket_y_un_ajeno_no(self):
        self._entrar(self.observador)
        respuesta = self.client.get(reverse("gacetas:detalle", args=[self.gaceta.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Banner")
        self.assertTrue(respuesta.context["filas"][0]["puede_observar"])
        self.assertIn(self.gaceta, self.client.get(reverse("gacetas:lista")).context["gacetas"])
        self.assertEqual(self.client.get(reverse("tickets:detalle", args=[self.ticket.pk])).status_code, 403)
        self._entrar(self.ajeno)
        self.assertEqual(self.client.get(reverse("gacetas:detalle", args=[self.gaceta.pk])).status_code, 403)

    def test_observar_y_reenviar_desde_la_pagina(self):
        self._entrar(self.observador)
        self.client.post(reverse("gacetas:observar", args=[self.p.pk]), {"texto": "Corregir el título"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.estado, Estado.EN_CORRECCION)
        self._entrar(self.disenadora)
        self.client.post(reverse("gacetas:reenviar", args=[self.p.pk]), {"comentario": "Hecho"})
        self.p.refresh_from_db()
        self.assertEqual((self.p.estado, self.p.ronda), (Estado.EN_REVISION, 2))

    def test_el_ticket_muestra_sus_gacetas_y_las_condiciones_de_cierre(self):
        self._entrar(self.analista)
        respuesta = self.client.get(reverse("tickets:trabajo", args=[self.ticket.pk]))
        self.assertContains(respuesta, "Gacetas")
        self.assertContains(respuesta, "Condiciones de cierre")
        self.assertFalse(next(r for r in respuesta.context["condiciones_cierre"] if r.codigo == "gacetas_resueltas").cumple)

    def test_studio_configura_gacetas_y_condiciones_y_ofrece_el_bloque(self):
        self._entrar(self.admin)
        respuesta = self.client.post(
            reverse("catalogo:studio_gaceta_crear", args=[self.servicio.pk]),
            {
                "gaceta-nueva-nombre": "Gaceta de cierre", "gaceta-nueva-colecciones": [self.definicion.clave],
                "gaceta-nueva-ingreso": "MANUAL", "gaceta-nueva-apertura": "AL_INICIAR",
                "gaceta-nueva-aprobadores_usuarios": [self.revisor.pk], "gaceta-nueva-politica": "TODOS",
                "gaceta-nueva-al_resolver_pendientes": "IMPEDIR", "gaceta-nueva-plazo_revision_unidad": "DIAS",
                "gaceta-nueva-plazo_correccion_unidad": "DIAS",
            },
        )
        self.assertEqual(respuesta.status_code, 302)
        nueva = self.servicio.definiciones_gaceta.get(nombre="Gaceta de cierre")
        self.assertEqual(nueva.configuracion["aprobadores"], [{"tipo": "USUARIO", "usuario_id": self.revisor.pk}])
        self.client.post(
            reverse("catalogo:studio_reglas_finalizacion", args=[self.servicio.pk]),
            {"gacetas_resueltas": "on", "items_con_resultado": "on", "resultados_admitidos": ["APROBADO", "EXCLUIDO"],
             "minimo_aprobados": "0"},
        )
        self.assertEqual(
            self.servicio.regla_finalizacion.configuracion["resultados_admitidos"], ["APROBADO", "EXCLUIDO"]
        )
        pagina = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "ejecucion"})
        self.assertIn("GACETA", [codigo for codigo, _e, _d in pagina.context["bloques_disponibles"]])
        salida = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]), {"tab": "salida"})
        self.assertContains(salida, "Gaceta de cierre")
        self.assertContains(salida, "Condiciones de cierre")

    def test_un_servicio_no_tiene_gacetas(self):
        servicio, _, _ = _crear_servicio_con_formulario(self.admin, [])
        with self.assertRaises(ValidationError):
            configurar_gaceta(
                servicio, self.admin, nombre="X",
                configuracion={"colecciones": ["x"], "aprobadores": [{"tipo": "USUARIO", "usuario_id": self.revisor.pk}]},
            )
        self.assertEqual(Servicio.objects.get(pk=servicio.pk).tipo, "SERVICIO")
        self.assertFalse(BloqueOperativo.objects.filter(tipo="GACETA").exists())
        self.assertFalse(TareaWorkflow.objects.filter(tarea__titulo__startswith="Corregir").exists())
        self.assertFalse(ResolucionTicket.objects.exists())


class ConcurrenciaGacetaTests(_EscenarioGacetasMixin, TransactionTestCase):
    """Hilos reales contra PostgreSQL (`TransactionTestCase`): el orden de locks instancia → ticket → gaceta →
    participación serializa las operaciones; ni la presentación ni la decisión se duplican."""

    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.segundo = Usuario.objects.create_user(username="f3_segundo", password=CLAVE_PRUEBA)
        self.regla = self._gaceta(
            self.servicio, self.definicion, politica="CUALQUIERA",
            aprobadores=[{"tipo": "USUARIO", "usuario_id": self.revisor.pk}, {"tipo": "USUARIO", "usuario_id": self.segundo.pk}],
        )
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)

    def _en_paralelo(self, *funciones):
        resultados = []
        barrera = threading.Barrier(len(funciones))

        def correr(funcion):
            barrera.wait()
            try:
                funcion()
                resultados.append("ok")
            except (ValidationError, PermissionDenied):
                resultados.append("rechazado")
            finally:
                connection.close()

        hilos = [threading.Thread(target=correr, args=(f,)) for f in funciones]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        return resultados

    def test_dos_presentaciones_simultaneas_crean_una_sola_participacion(self):
        self._en_paralelo(
            lambda: operaciones.presentar(GacetaTicket.objects.get(pk=self.gaceta.pk), self.banner, self.analista),
            lambda: operaciones.presentar(GacetaTicket.objects.get(pk=self.gaceta.pk), self.banner, self.analista),
        )
        self.assertEqual(ParticipacionItemGaceta.objects.filter(gaceta=self.gaceta, elemento=self.banner).count(), 1)
        self.assertEqual(RondaRevisionGaceta.objects.filter(participacion__gaceta=self.gaceta).count(), 1)

    def test_dos_decisiones_simultaneas_cierran_una_sola_vez(self):
        participacion = operaciones.presentar(self.gaceta, self.banner, self.analista)
        esquema = participacion.rondas.get().esquema
        a = esquema.participaciones.get(aprobador_usuario=self.revisor)
        b = esquema.participaciones.get(aprobador_usuario=self.segundo)
        resultados = self._en_paralelo(
            lambda: operaciones.resolver_aprobacion_gaceta(a, self.revisor, decision="APROBADA"),
            lambda: operaciones.resolver_aprobacion_gaceta(b, self.segundo, decision="RECHAZADA", observacion="No"),
        )
        participacion.refresh_from_db()
        self.assertIn(participacion.estado, (Estado.APROBADA, Estado.RECHAZADA))
        self.assertEqual(resultados.count("ok") + resultados.count("rechazado"), 2)
        salidas = DecisionAlcanceElemento.objects.filter(elemento=self.banner, tipo__in=("EXCLUIDO", "RECHAZADO")).count()
        self.assertEqual(salidas, 1 if participacion.estado == Estado.RECHAZADA else 0)
