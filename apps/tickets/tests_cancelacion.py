"""Cancelación con aprobación (extiende CU-019): política congelada, solicitud, congelamiento y decisión."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.aprobaciones.decisiones import aplicar as aplicar_decision
from apps.aprobaciones.models import Aprobacion
from apps.catalogo.operaciones import configurar_politica_cancelacion
from apps.catalogo.tests import _otorgar_permiso
from apps.tickets import cancelaciones
from apps.tickets.models import HistorialTicket, SolicitudCancelacion, Ticket
from apps.tickets.operaciones import (
    cancelar_ticket,
    crear_borrador,
    reasignar_ticket,
    resolver_ticket,
    solicitar_informacion,
    tomar_ticket,
)
from apps.tickets.tests import CLAVE_PRUEBA, Usuario, _EscenarioAtencionMixin


class _CancelacionMixin(_EscenarioAtencionMixin):
    def _con_aprobacion(self, aprobador):
        # La política se congela al crear el ticket: para el escenario ya radicado se fija directamente en la BD.
        Ticket.objects.filter(pk=self.ticket.pk).update(
            cancelacion_con_aprobacion=True, cancelacion_aprobador_usuario=aprobador
        )
        self.ticket.refresh_from_db()

    def _aprobacion(self, solicitud):
        return Aprobacion.objects.get(esquema=solicitud.esquema_aprobacion)


class CancelacionSinPoliticaTests(_CancelacionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_sin_politica_cancela_directo_como_siempre(self):
        resultado = cancelaciones.solicitar_cancelacion(self.ticket, self.solicitante, "Ya no lo necesito.")
        self.assertIsInstance(resultado, Ticket)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CANCELADO)
        self.assertFalse(SolicitudCancelacion.objects.exists())

    def test_el_servicio_congela_la_politica_en_los_tickets_nuevos(self):
        admin = Usuario.objects.create_user("admin_cancel@pruebas.orbita")
        _otorgar_permiso(admin, "catalogo.administrar")
        configurar_politica_cancelacion(self.servicio, admin, con_aprobacion=True, aprobador_usuario=self.gestor_area)
        nuevo = crear_borrador(self.solicitante, self.servicio)
        self.assertTrue(nuevo.cancelacion_con_aprobacion)
        self.assertEqual(nuevo.cancelacion_aprobador_usuario, self.gestor_area)
        self.ticket.refresh_from_db()
        self.assertFalse(self.ticket.cancelacion_con_aprobacion)  # el ya creado conserva la suya

    def test_la_politica_exige_un_aprobador_y_permiso(self):
        admin = Usuario.objects.create_user("admin_cancel2@pruebas.orbita")
        _otorgar_permiso(admin, "catalogo.administrar")
        with self.assertRaises(ValidationError):
            configurar_politica_cancelacion(self.servicio, admin, con_aprobacion=True)
        with self.assertRaises(PermissionDenied):
            configurar_politica_cancelacion(self.servicio, self.ajeno, con_aprobacion=False)

    def test_el_studio_guarda_la_politica(self):
        admin = Usuario.objects.create_user("admin_cancel3@pruebas.orbita")
        _otorgar_permiso(admin, "catalogo.administrar")
        self.client.force_login(admin)
        self.client.post(
            reverse("catalogo:studio_cancelacion_guardar", args=[self.servicio.pk]),
            {"politica": "CON_APROBACION", "aprobador_usuario": self.gestor_area.pk},
        )
        self.servicio.refresh_from_db()
        self.assertTrue(self.servicio.cancelacion_con_aprobacion)
        self.assertEqual(self.servicio.cancelacion_aprobador_usuario, self.gestor_area)


class CancelacionConAprobacionTests(_CancelacionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self._con_aprobacion(self.gestor_area)

    def test_cancelar_directo_se_rechaza_si_exige_aprobacion(self):
        with self.assertRaises(ValidationError):
            cancelar_ticket(self.ticket, self.solicitante, "Directo")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_pedirla_crea_una_solicitud_pendiente_y_congela_el_ticket(self):
        solicitud = cancelaciones.solicitar_cancelacion(self.ticket, self.solicitante, "Cambió la prioridad.")
        self.assertEqual(solicitud.estado, SolicitudCancelacion.Estado.PENDIENTE)
        self.assertTrue(cancelaciones.esta_congelado(self.ticket))
        self.assertTrue(
            HistorialTicket.objects.filter(ticket=self.ticket, tipo_evento="CANCELACION_SOLICITADA").exists()
        )
        with self.assertRaises(ValidationError):
            resolver_ticket(self.ticket, self.responsable_directo, "Listo")
        with self.assertRaises(ValidationError):
            reasignar_ticket(self.ticket, self.responsable_directo, usuario=self.miembro_equipo)
        with self.assertRaises(ValidationError):
            solicitar_informacion(self.ticket, self.responsable_directo, "¿Algo más?")
        with self.assertRaises(ValidationError):  # una sola pendiente
            cancelaciones.solicitar_cancelacion(self.ticket, self.responsable_directo, "Otra vez")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_aprobarla_cancela_el_ticket(self):
        solicitud = cancelaciones.solicitar_cancelacion(self.ticket, self.solicitante, "Cambió la prioridad.")
        aplicar_decision(self._aprobacion(solicitud), self.gestor_area, decision=Aprobacion.Estado.APROBADA)
        solicitud.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudCancelacion.Estado.APROBADA)
        self.assertEqual(self.ticket.estado, Ticket.Estado.CANCELADO)
        self.assertFalse(cancelaciones.esta_congelado(self.ticket))

    def test_rechazarla_descongela_sin_cambiar_el_ticket(self):
        solicitud = cancelaciones.solicitar_cancelacion(self.ticket, self.solicitante, "Cambió la prioridad.")
        aplicar_decision(
            self._aprobacion(solicitud), self.gestor_area,
            decision=Aprobacion.Estado.RECHAZADA, observacion="Se necesita igual.",
        )
        solicitud.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudCancelacion.Estado.RECHAZADA)
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertFalse(cancelaciones.esta_congelado(self.ticket))
        resolver_ticket(self.ticket, self.responsable_directo, "Listo")  # el trabajo continúa

    def test_solo_quien_la_pidio_la_retira(self):
        solicitud = cancelaciones.solicitar_cancelacion(self.ticket, self.solicitante, "Cambió la prioridad.")
        with self.assertRaises(PermissionDenied):
            cancelaciones.retirar_cancelacion(solicitud, self.responsable_directo)
        cancelaciones.retirar_cancelacion(solicitud, self.solicitante)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudCancelacion.Estado.RETIRADA)
        self.assertEqual(self._aprobacion(solicitud).estado, Aprobacion.Estado.NO_REQUERIDA)
        self.assertFalse(cancelaciones.esta_congelado(self.ticket))

    def test_la_vista_pide_la_cancelacion_y_el_espacio_avisa(self):
        self.client.force_login(self.solicitante)
        respuesta = self.client.post(reverse("tickets:cancelar", args=[self.ticket.pk]), {"motivo": "Ya no aplica."})
        self.assertRedirects(respuesta, reverse("tickets:espacio", args=[self.ticket.pk]))
        self.assertTrue(cancelaciones.esta_congelado(self.ticket))
        self.client.force_login(self.responsable_directo)
        espacio = self.client.get(reverse("tickets:espacio", args=[self.ticket.pk]))
        self.assertContains(espacio, "Cancelación pendiente de aprobación")
        self.assertEqual(espacio.context["acciones"], [])  # congelado: nada que avanzar
        self.client.force_login(self.gestor_area)
        espacio = self.client.get(reverse("tickets:espacio", args=[self.ticket.pk]))
        self.assertEqual([a["titulo"] for a in espacio.context["acciones"]], ["Cancelación del ticket"])


class AvisosDeCancelacionTests(_CancelacionMixin, TestCase):
    """Pedir la cancelación avisa a quien atiende (el ticket queda detenido) y aprobarla avisa a quien la pidió."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self._con_aprobacion(self.gestor_area)
        for usuario in (self.solicitante, self.responsable_directo, self.gestor_area):
            usuario.email = usuario.email.replace("@pruebas.orbita", "@example.com")
            usuario.save(update_fields=["email"])

    def _avisos(self, usuario, evento):
        from apps.notificaciones.models import Notificacion

        return Notificacion.objects.filter(destinatario=usuario, evento=evento)

    def test_pedirla_avisa_al_responsable_y_aprobarla_al_solicitante(self):
        with self.captureOnCommitCallbacks(execute=True):
            solicitud = cancelaciones.solicitar_cancelacion(self.ticket, self.solicitante, "Cambió la prioridad.")
        self.assertTrue(self._avisos(self.responsable_directo, "CANCELACION_SOLICITADA").exists())
        self.assertFalse(self._avisos(self.solicitante, "CANCELACION_SOLICITADA").exists())  # nunca a quien actúa
        self.assertTrue(self._avisos(self.gestor_area, "APROBACION_TURNO").exists())
        with self.captureOnCommitCallbacks(execute=True):
            aplicar_decision(self._aprobacion(solicitud), self.gestor_area, decision=Aprobacion.Estado.APROBADA)
        self.assertTrue(self._avisos(self.solicitante, "TICKET_CANCELADO").exists())
        self.assertTrue(self._avisos(self.responsable_directo, "TICKET_CANCELADO").exists())


class SoloConsultaYResumenTests(_CancelacionMixin, TestCase):
    """Un ticket cerrado o cancelado queda solo en consulta; la vista «Resumen» reemplaza a «Gestión»."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_el_resumen_responde_con_su_nombre_y_con_el_anterior(self):
        self.client.force_login(self.responsable_directo)
        base = reverse("tickets:espacio", args=[self.ticket.pk])
        for vista in ("resumen", "gestion"):
            respuesta = self.client.get(f"{base}?vista={vista}")
            self.assertContains(respuesta, "Resumen del ticket")
            self.assertContains(respuesta, "rs-ficha")

    def test_un_ticket_cancelado_queda_solo_en_consulta(self):
        cancelar_ticket(self.ticket, self.solicitante, "Ya no aplica.")
        self.client.force_login(self.responsable_directo)
        respuesta = self.client.get(reverse("tickets:espacio", args=[self.ticket.pk]) + "?vista=resumen")
        self.assertContains(respuesta, "solo consulta")
        self.assertEqual(respuesta.context["acciones"], [])
        self.assertFalse(respuesta.context["generales"]["puede_cancelar"])
        self.assertFalse(respuesta.context["generales"]["reasignar_a"])
        self.assertFalse(respuesta.context["puede_resolver"])


class HistoricoTests(_CancelacionMixin, TestCase):
    """La pestaña «Histórico» de Mis tickets y de Mi trabajo, con sus filtros."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        cancelar_ticket(self.ticket, self.solicitante, "Ya no aplica.")

    def test_mis_tickets_historico_lista_lo_terminado_y_filtra(self):
        self.client.force_login(self.solicitante)
        url = reverse("tickets:mis_tickets")
        respuesta = self.client.get(url + "?ver=historico")
        self.assertIn(self.ticket, respuesta.context["historico"]["pagina"].object_list)
        self.assertContains(respuesta, "Histórico")
        respuesta = self.client.get(url + "?ver=historico&tipo=PROCESO")
        self.assertNotIn(self.ticket, respuesta.context["historico"]["pagina"].object_list)
        respuesta = self.client.get(url + "?ver=finalizados")  # nombre anterior del segmento
        self.assertIsNotNone(respuesta.context["historico"])

    def test_mi_trabajo_historico_lista_lo_que_atendio(self):
        self.client.force_login(self.responsable_directo)
        respuesta = self.client.get(reverse("core:mi_trabajo") + "?ver=historico")
        self.assertIn(self.ticket, respuesta.context["historico"]["pagina"].object_list)
        self.client.force_login(self.ajeno)
        respuesta = self.client.get(reverse("core:mi_trabajo") + "?ver=historico")
        self.assertNotIn(self.ticket, respuesta.context["historico"]["pagina"].object_list)


class AvisoDeColaAlRadicarTests(_CancelacionMixin, TestCase):
    """Bug del 2026-11-12: un ticket normal nace sin equipo puesto (sus responsables están en el Servicio) y el aviso
    de Cola no le llegaba a nadie. Ahora llega, por correo, a todos los que pueden tomarlo; nunca al solicitante."""

    def setUp(self):
        self._preparar_escenario()
        for usuario in (self.solicitante, self.responsable_directo, self.miembro_equipo):
            usuario.email = usuario.email.replace("@pruebas.orbita", "@example.com")
            usuario.save(update_fields=["email"])

    def test_radicar_avisa_por_correo_a_quienes_pueden_tomarlo(self):
        from apps.notificaciones.models import EnvioCorreo, Notificacion
        from apps.tickets.operaciones import radicar_ticket
        from apps.tickets.tests import _completar_texto

        ticket = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(ticket, self.solicitante, self.campos)
        with self.captureOnCommitCallbacks(execute=True):
            radicar_ticket(ticket, self.solicitante)
        en_cola = Notificacion.objects.filter(ticket=ticket, evento="TICKET_EN_COLA")
        self.assertEqual(
            set(en_cola.values_list("destinatario_id", flat=True)),
            {self.responsable_directo.pk, self.miembro_equipo.pk},
        )
        self.assertEqual(EnvioCorreo.objects.filter(notificacion__in=en_cola).count(), 2)
        self.assertTrue(all(n.enlace for n in en_cola))
