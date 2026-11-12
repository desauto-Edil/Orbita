"""Pruebas de notificaciones y alertas — Sprint 7 (adelanto): CU-033 parcial, CU-034 parcial, CU-035, CU-036, CU-037.

Cubren: eventos de dominio entregados al confirmar (y nunca si la operación se deshace), destinatarios por relación
(nunca el actor, nunca un solicitante inexistente), agrupación y supresión, enlaces solo para quien puede abrir,
reglas de correo (catálogo, ticket congelado, preferencias, lo que exige acción), envío por Gmail con el logo
incrustado y sus estados (omitido, enviado, incierto, redirigido), centro de notificaciones, preferencias,
configuración general, Studio › Notificaciones (validaciones, foto congelada, reglas por etapa) y el evaluador de
alertas (una vez por umbral, resolución automática, recordatorios con máximo y escalamiento)."""

import base64
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion
from apps.aprobaciones.operaciones import crear_esquema_aprobacion, resolver_aprobacion
from apps.core.correo import construir
from apps.core.models import ConfiguracionSistema
from apps.notificaciones import alertas, catalogo, configuracion as cfg, correo, despacho, destinatarios as dest, mensajes
from apps.notificaciones.eventos import Evento, publicar
from apps.notificaciones.models import (
    Alerta,
    ConfiguracionNotificaciones,
    DEFAULTS,
    ConfiguracionNotificacionesServicio,
    EnvioCorreo,
    Grupo,
    Notificacion,
    PreferenciaNotificacion,
    ReglaNotificacion,
)
from apps.tareas.operaciones import crear_tarea
from apps.tickets.models import SolicitudInformacion, Ticket
from apps.tickets.operaciones import asignar_ticket, comentar_ticket, crear_borrador, solicitar_informacion
from apps.tickets.tests import (
    CLAVE_PRUEBA,
    Usuario,
    _EscenarioAtencionMixin,
    _MediaAisladaMixin,
    _otorgar_permiso,
)
from apps.core.pruebas import persona

# PNG 1×1 válido (el logo se valida por contenido).
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _Base(_EscenarioAtencionMixin):
    """Escenario de atención de tickets (`apps.tickets.tests`) con correos y sin encolar en Celery."""

    def setUp(self):
        # Lo que la preparación publica (radicar, asignaciones) se entrega aquí y se limpia: cada prueba empieza sin
        # avisos pendientes.
        with self.captureOnCommitCallbacks(execute=True):
            self._preparar_escenario()
        Notificacion.objects.all().delete()
        for usuario in (self.solicitante, self.responsable_directo, self.miembro_equipo, self.gestor_area):
            usuario.email = usuario.email.replace("@pruebas.orbita", "@edilandina.test")
            usuario.save(update_fields=["email"])
        self._encolar = mock.patch.object(correo, "encolar", lambda notificaciones: None)
        self._encolar.start()
        self.addCleanup(self._encolar.stop)
        cfg.olvidar_globales()
        self.addCleanup(cfg.olvidar_globales)

    def _de(self, usuario, evento=None):
        consulta = Notificacion.objects.filter(destinatario=usuario)
        return consulta.filter(evento=evento) if evento else consulta


# --- Eventos y despacho -------------------------------------------------------------------------------------


class EventosTests(_Base, TestCase):
    def test_un_dato_llamado_clave_no_choca_con_la_clave_del_evento(self):
        # El motor publica la clave estable de la fase o actividad como dato `clave`.
        with self.captureOnCommitCallbacks(execute=False):
            evento = publicar("FLUJO:FASE_ALCANZADA", ticket=self.ticket, clave="revision", nombre="Revisión")
        self.assertEqual(evento.clave, "FLUJO:FASE_ALCANZADA")
        self.assertEqual(evento.datos["clave"], "revision")

    def test_radicar_avisa_a_quien_radica_con_su_codigo(self):
        Notificacion.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            publicar("TICKET:RADICADO", actor=self.solicitante, ticket=self.ticket)
        aviso = self._de(self.solicitante, "TICKET_RADICADO").get()
        self.assertIn(self.ticket.codigo, aviso.resumen)
        self.assertEqual(aviso.grupo, Grupo.TICKETS)
        self.assertEqual(aviso.enlace, reverse("tickets:seguimiento", args=[self.ticket.pk]))
        self.assertTrue(EnvioCorreo.objects.filter(notificacion=aviso).exists())

    def test_nunca_se_avisa_a_quien_hizo_la_accion(self):
        asignar_ticket(self.ticket, self.gestor_area, usuario=self.responsable_directo)
        with self.captureOnCommitCallbacks(execute=True):
            comentar_ticket(self.ticket, self.solicitante, "Adjunto lo que faltaba.")
        self.assertTrue(self._de(self.responsable_directo, "COMENTARIO").exists())
        self.assertFalse(self._de(self.solicitante, "COMENTARIO").exists())
        self.assertEqual(self._de(self.responsable_directo, "COMENTARIO").get().extracto, "Adjunto lo que faltaba.")

    def test_una_operacion_deshecha_no_avisa(self):
        class Falla(Exception):
            pass

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    comentar_ticket(self.ticket, self.solicitante, "No debe llegar.")
                    raise Falla
            except Falla:
                pass
        self.assertFalse(Notificacion.objects.filter(evento="COMENTARIO").exists())

    def test_asignar_avisa_a_quien_recibe_y_no_a_quien_asigna(self):
        with self.captureOnCommitCallbacks(execute=True):
            asignar_ticket(self.ticket, self.gestor_area, usuario=self.responsable_directo)
        aviso = self._de(self.responsable_directo, "TICKET_ASIGNADO").get()
        self.assertEqual(aviso.enlace, reverse("tickets:espacio", args=[self.ticket.pk]))
        self.assertFalse(self._de(self.gestor_area).exists())
        self.assertTrue(self._de(self.solicitante, "TICKET_EN_ATENCION").exists())

    def test_informacion_solicitada_exige_accion_y_sale_por_correo(self):
        asignar_ticket(self.ticket, self.gestor_area, usuario=self.responsable_directo)  # RADICADO → EN_ATENCION
        PreferenciaNotificacion.objects.create(usuario=self.solicitante, grupo=Grupo.TICKETS, correo=False)
        with self.captureOnCommitCallbacks(execute=True):
            solicitar_informacion(Ticket.objects.get(pk=self.ticket.pk), self.responsable_directo, "Falta el NIT.")
        aviso = self._de(self.solicitante, "INFORMACION_SOLICITADA").get()
        self.assertTrue(aviso.requiere_accion)
        self.assertEqual(aviso.extracto, "Falta el NIT.")
        self.assertTrue(EnvioCorreo.objects.filter(notificacion=aviso).exists(), "lo que exige acción no se apaga")

    def test_aprobacion_secuencial_avisa_solo_al_turno_y_luego_al_siguiente(self):
        with self.captureOnCommitCallbacks(execute=True):
            esquema = crear_esquema_aprobacion(
                modo=EsquemaAprobacion.Modo.SECUENCIAL,
                participantes=[(Aprobacion.TipoAprobador.USUARIO, self.responsable_directo),
                               (Aprobacion.TipoAprobador.USUARIO, self.gestor_area)],
            )
        self.assertTrue(self._de(self.responsable_directo, "APROBACION_TURNO").exists())
        self.assertFalse(self._de(self.gestor_area, "APROBACION_TURNO").exists())
        primera = esquema.participaciones.get(orden=1)
        with self.captureOnCommitCallbacks(execute=True):
            resolver_aprobacion(primera, self.responsable_directo, decision=Aprobacion.Estado.APROBADA)
        self.assertTrue(self._de(self.gestor_area, "APROBACION_TURNO").exists())

    def test_tarea_creada_con_responsable_avisa_una_sola_vez(self):
        with self.captureOnCommitCallbacks(execute=True):
            crear_tarea(titulo="Revisar contrato", creada_por=self.gestor_area, usuario_responsable=self.responsable_directo)
        aviso = self._de(self.responsable_directo, "TAREA_ASIGNADA").get()
        self.assertIn("Revisar contrato", aviso.resumen)
        self.assertEqual(aviso.cantidad, 1)


class DespachoTests(_Base, TestCase):
    def test_lo_que_llega_en_lote_se_agrupa_por_persona(self):
        avisos = [
            despacho.Aviso(usuario=self.responsable_directo, clave="TAREA_ASIGNADA", ticket=self.ticket,
                           agrupacion="TAREA_ASIGNADA:t1", contexto={"tarea_id": i, "tarea": f"Pieza {i}"})
            for i in (1, 2, 3)
        ]
        avisos.append(despacho.Aviso(usuario=self.responsable_directo, clave="TAREA_ASIGNADA", ticket=self.ticket,
                                     agrupacion="TAREA_ASIGNADA:t1", contexto={"tarea_id": 3, "tarea": "Pieza 3"}))
        despacho.entregar(avisos)
        aviso = self._de(self.responsable_directo, "TAREA_ASIGNADA").get()
        self.assertEqual(aviso.cantidad, 3, "la misma tarea dos veces es una sola actividad")
        self.assertEqual(aviso.titulo, "Tienes 3 actividades nuevas")

    def test_un_aviso_especifico_suprime_al_general_del_mismo_lote(self):
        despacho.entregar([
            despacho.Aviso(usuario=self.responsable_directo, clave="TAREA_ASIGNADA", ticket=self.ticket,
                           agrupacion="TAREA_ASIGNADA:t9", contexto={"tarea_id": 1}),
            despacho.Aviso(usuario=self.responsable_directo, clave="GACETA_OBSERVACION", ticket=self.ticket,
                           suprime=("TAREA_ASIGNADA:t9",), contexto={"gaceta": "Parrilla", "extracto": "Ajusta el color"}),
        ])
        self.assertEqual(list(self._de(self.responsable_directo).values_list("evento", flat=True)), ["GACETA_OBSERVACION"])

    def test_sin_solicitante_no_se_inventa_un_destinatario(self):
        programado = Ticket(solicitante=None)
        self.assertEqual(dest.solicitante(programado), [])

    def test_usuarios_inactivos_no_reciben(self):
        self.responsable_directo.is_active = False
        self.responsable_directo.save(update_fields=["is_active"])
        despacho.entregar([despacho.Aviso(usuario=self.responsable_directo, clave="COMENTARIO", ticket=self.ticket)])
        self.assertFalse(Notificacion.objects.exists())

    def test_enlace_solo_para_quien_puede_abrir(self):
        ajeno = despacho.Aviso(usuario=self.ajeno, clave="COMENTARIO", ticket=self.ticket)
        self.assertEqual(mensajes.enlace_para(self.ajeno, ajeno), "")

    def test_el_ticket_congelado_decide_el_correo_de_los_avisos_base(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(notificaciones={"correo": {"TICKET_RESUELTO": False}})
        self.ticket.refresh_from_db()
        despacho.entregar([despacho.Aviso(usuario=self.solicitante, clave="TICKET_RESUELTO", ticket=self.ticket)])
        self.assertFalse(EnvioCorreo.objects.exists())

    def test_preferencia_apaga_el_correo_de_lo_que_no_exige_accion(self):
        PreferenciaNotificacion.objects.create(usuario=self.solicitante, grupo=Grupo.TICKETS, correo=False)
        despacho.entregar([despacho.Aviso(usuario=self.solicitante, clave="TICKET_RESUELTO", ticket=self.ticket)])
        self.assertTrue(Notificacion.objects.exists())
        self.assertFalse(EnvioCorreo.objects.exists())

    def test_sin_correo_no_hay_envio(self):
        self.solicitante.email = ""
        self.solicitante.save(update_fields=["email"])
        despacho.entregar([despacho.Aviso(usuario=self.solicitante, clave="ENTREGA_DISPONIBLE", ticket=self.ticket)])
        self.assertFalse(EnvioCorreo.objects.exists())

    def test_regla_por_etapa_avisa_a_quien_se_configuro(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(notificaciones={"reglas": [{
            "id": 7, "momento": "FASE_ALCANZADA", "clave_objetivo": "produccion", "nombre_objetivo": "Producción",
            "destinatario": "USUARIO", "usuario_id": self.gestor_area.pk, "correo": False, "mensaje": "Prepara la pauta",
        }]})
        self.ticket.refresh_from_db()
        evento = Evento(clave="FLUJO:FASE_ALCANZADA", datos={"clave": "produccion"})
        avisos = despacho._reglas(self.ticket, "FASE_ALCANZADA", "produccion", evento)
        self.assertEqual([a.usuario for a in avisos], [self.gestor_area])
        self.assertEqual(despacho._reglas(self.ticket, "FASE_ALCANZADA", "otra", evento), [])
        despacho.entregar(avisos)
        aviso = self._de(self.gestor_area, "REGLA_ETAPA").get()
        self.assertIn("Producción", aviso.titulo)
        self.assertEqual(aviso.extracto, "Prepara la pauta")
        self.assertFalse(EnvioCorreo.objects.filter(notificacion=aviso).exists(), "la regla pidió solo Órbita")

    def test_catalogo_lo_que_exige_accion_siempre_va_por_correo(self):
        self.assertTrue(catalogo.correo_por_defecto("APROBACION_TURNO", {"APROBACION_TURNO": False}))
        # Un mensaje de la conversación sí va por correo, pero diferido y solo si sigue sin leerse (`correo_conversacion`).
        self.assertTrue(catalogo.correo_por_defecto("COMENTARIO"))
        self.assertFalse(catalogo.correo_por_defecto("COMENTARIO", {"COMENTARIO": False}))


# --- Correo -------------------------------------------------------------------------------------------------


class CorreoTests(_MediaAisladaMixin, _Base, TestCase):
    def _notificacion(self):
        despacho.entregar([despacho.Aviso(usuario=self.solicitante, clave="TICKET_RESUELTO", ticket=self.ticket,
                                          actor_id=self.responsable_directo.pk)])
        notificacion = Notificacion.objects.get(destinatario=self.solicitante)
        return notificacion, EnvioCorreo.objects.get(notificacion=notificacion)

    def test_el_correo_lleva_el_logo_del_modo_claro_incrustado(self):
        sistema = ConfiguracionSistema.actual()
        sistema.logo = SimpleUploadedFile("logo.png", PNG, content_type="image/png")
        sistema.logo_oscuro = SimpleUploadedFile("oscuro.png", PNG, content_type="image/png")
        sistema.save()
        notificacion, _envio = self._notificacion()
        asunto, texto, html, imagenes = correo.contenido(notificacion)
        self.assertTrue(asunto.startswith(self.ticket.codigo))
        self.assertEqual([i[0] for i in imagenes], [correo.CID_LOGO])
        self.assertEqual(imagenes[0][1], PNG)
        self.assertIn(f"cid:{correo.CID_LOGO}", html)
        self.assertIn("Ver en", html)
        self.assertIn(reverse("notificaciones:abrir", args=[notificacion.pk]), texto)
        mensaje = construir("x@edilandina.test", asunto, texto, html=html, imagenes=imagenes)
        self.assertIn("Content-ID: <logo-orbita>", mensaje.as_string())

    def test_sin_logo_el_correo_lleva_la_inicial(self):
        notificacion, _envio = self._notificacion()
        _asunto, _texto, html, imagenes = correo.contenido(notificacion)
        self.assertEqual(imagenes, [])
        self.assertIn(ConfiguracionSistema.actual().inicial, html)

    @override_settings(NOTIFICACIONES_CORREO_ACTIVO=False)
    def test_con_el_interruptor_apagado_no_se_envia(self):
        _n, envio = self._notificacion()
        with mock.patch("apps.core.correo.enviar_mensaje") as enviar:
            correo.enviar(envio.pk)
        enviar.assert_not_called()
        envio.refresh_from_db()
        self.assertEqual(envio.estado, EnvioCorreo.Estado.OMITIDO)

    @override_settings(NOTIFICACIONES_CORREO_ACTIVO=True, CORREO_REDIRIGIR_A="")
    def test_envio_confirmado(self):
        _n, envio = self._notificacion()
        with mock.patch("apps.core.correo.enviar_mensaje", return_value="gmail-1") as enviar:
            correo.enviar(envio.pk)
            correo.enviar(envio.pk)  # idempotente: ya enviado
        self.assertEqual(enviar.call_count, 1)
        envio.refresh_from_db()
        self.assertEqual((envio.estado, envio.mensaje_id), (EnvioCorreo.Estado.ENVIADO, "gmail-1"))

    @override_settings(NOTIFICACIONES_CORREO_ACTIVO=True)
    def test_un_envio_incierto_no_se_reintenta(self):
        from apps.core.correo import ErrorCorreoIncierto

        _n, envio = self._notificacion()
        with mock.patch("apps.core.correo.enviar_mensaje", side_effect=ErrorCorreoIncierto("sin confirmar")) as enviar:
            correo.enviar(envio.pk)
            correo.enviar(envio.pk)
        self.assertEqual(enviar.call_count, 1)
        envio.refresh_from_db()
        self.assertEqual(envio.estado, EnvioCorreo.Estado.INCIERTO)

    @override_settings(NOTIFICACIONES_CORREO_ACTIVO=True, CORREO_REDIRIGIR_A="pruebas@edilandina.test")
    def test_en_pruebas_todo_correo_se_redirige(self):
        _n, envio = self._notificacion()
        with mock.patch("apps.core.correo.enviar_mensaje", return_value="gmail-2") as enviar:
            correo.enviar(envio.pk)
        mensaje = enviar.call_args[0][0]
        self.assertEqual(mensaje["To"], "pruebas@edilandina.test")
        self.assertIn(self.solicitante.email, mensaje["Subject"])
        envio.refresh_from_db()
        self.assertEqual(envio.correo, self.solicitante.email)


# --- Centro, preferencias y configuración -------------------------------------------------------------------


class CentroTests(_Base, TestCase):
    def setUp(self):
        super().setUp()
        despacho.entregar([despacho.Aviso(usuario=self.solicitante, clave="TICKET_RESUELTO", ticket=self.ticket)])
        despacho.entregar([despacho.Aviso(usuario=self.responsable_directo, clave="COMENTARIO", ticket=self.ticket)])
        self.mia = Notificacion.objects.get(destinatario=self.solicitante)
        self.ajena = Notificacion.objects.get(destinatario=self.responsable_directo)
        self.client.force_login(self.solicitante)

    def test_cada_persona_ve_solo_las_suyas(self):
        respuesta = self.client.get(reverse("notificaciones:centro"))
        self.assertEqual([f["n"].pk for f in respuesta.context["filas"]], [self.mia.pk])
        self.assertContains(respuesta, "notif-campana__cuenta")

    def test_abrir_marca_como_leida_y_lleva_al_objeto(self):
        respuesta = self.client.get(reverse("notificaciones:abrir", args=[self.mia.pk]))
        self.assertRedirects(respuesta, self.mia.enlace, fetch_redirect_response=False)
        self.mia.refresh_from_db()
        self.assertIsNotNone(self.mia.leida_en)
        self.assertEqual(self.client.get(reverse("notificaciones:abrir", args=[self.ajena.pk])).status_code, 404)

    def test_marcar_todas(self):
        self.client.post(reverse("notificaciones:todas_leidas"))
        self.assertFalse(Notificacion.objects.filter(destinatario=self.solicitante, leida_en__isnull=True).exists())
        self.assertTrue(Notificacion.objects.filter(destinatario=self.responsable_directo, leida_en__isnull=True).exists())

    def test_preferencias_desde_mi_perfil(self):
        self.client.post(reverse("notificaciones:preferencias"), {"correo_TRABAJO": "on"})
        guardadas = dict(PreferenciaNotificacion.objects.filter(usuario=self.solicitante).values_list("grupo", "correo"))
        self.assertTrue(guardadas[Grupo.TRABAJO])
        self.assertFalse(guardadas[Grupo.TICKETS])
        self.assertContains(self.client.get(reverse("core:perfil")), 'id="notificaciones"')

    def test_configuracion_general_exige_sistema_configurar(self):
        self.assertEqual(self.client.get(reverse("notificaciones:configuracion")).status_code, 403)
        _otorgar_permiso(self.solicitante, "sistema.configurar")
        datos = dict(DEFAULTS)
        datos.update(por_vencer_porcentaje=70, escalar="on")
        respuesta = self.client.post(reverse("notificaciones:configuracion"), datos)
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(ConfiguracionNotificaciones.actual().por_vencer_porcentaje, 70)
        self.assertEqual(cfg.parametros_de()["por_vencer_porcentaje"], 70)


# --- Studio › Notificaciones --------------------------------------------------------------------------------


class StudioNotificacionesTests(_Base, TestCase):
    def test_avisos_base_solo_guardan_cambios_y_no_apagan_lo_que_exige_accion(self):
        cfg.guardar_avisos_base(self.servicio, self.gestor_area, {
            "TICKET_RESUELTO": False, "COMENTARIO": False, "TICKET_RADICADO": True, "INFORMACION_SOLICITADA": False,
        })
        configuracion = ConfiguracionNotificacionesServicio.objects.get(servicio=self.servicio)
        self.assertEqual(configuracion.correo_eventos, {"TICKET_RESUELTO": False, "COMENTARIO": False})

    def test_la_foto_se_congela_al_crear_el_ticket(self):
        cfg.guardar_avisos_base(self.servicio, self.gestor_area, {"TICKET_RESUELTO": False})
        nuevo = crear_borrador(self.solicitante, self.servicio)
        nuevo.refresh_from_db()
        self.assertEqual(nuevo.notificaciones["correo"], {"TICKET_RESUELTO": False})
        cfg.guardar_avisos_base(self.servicio, self.gestor_area, {"TICKET_RESUELTO": True})
        nuevo.refresh_from_db()
        self.assertEqual(nuevo.notificaciones["correo"], {"TICKET_RESUELTO": False}, "cambiar el Studio no altera tickets creados")
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.notificaciones, "un ticket anterior conserva los valores por defecto")

    def test_una_regla_debe_apuntar_a_algo_del_flujo(self):
        with self.assertRaises(ValidationError):
            cfg.agregar_regla(self.servicio, self.gestor_area, momento="FASE_ALCANZADA", clave_objetivo="no-existe",
                              destinatario="SOLICITANTE")
        with self.assertRaises(ValidationError):
            cfg.agregar_regla(self.servicio, self.gestor_area, momento="NADA", clave_objetivo="x", destinatario="SOLICITANTE")

    def test_reglas_huerfanas_y_retiro_sin_borrar(self):
        regla = ReglaNotificacion.objects.create(
            servicio=self.servicio, momento="FASE_ALCANZADA", clave_objetivo="desaparecida", destinatario="SOLICITANTE",
        )
        self.assertEqual(cfg.reglas_huerfanas(self.servicio), [regla])
        self.assertEqual(cfg.estado(self.servicio)[0], "attention")
        cfg.retirar_regla(regla, self.gestor_area)
        self.assertTrue(ReglaNotificacion.objects.filter(pk=regla.pk, activa=False).exists())
        self.assertEqual(cfg.estado(self.servicio)[0], "optional")

    def test_la_seccion_aparece_en_el_studio(self):
        _otorgar_permiso(self.gestor_area, "catalogo.administrar")
        self.client.force_login(self.gestor_area)
        respuesta = self.client.get(f"{reverse('core:disenador_servicio', args=[self.servicio.pk])}?tab=notificaciones")
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Avisos base")
        self.assertIn("notificaciones", [s["clave"] for s in respuesta.context["secciones"]])

    def test_recordatorios_propios_sobre_los_generales(self):
        cfg.guardar_recordatorios(self.servicio, self.gestor_area, parametros={"por_vencer_porcentaje": 60}, escalar_a="SUPERVISION")
        nuevo = crear_borrador(self.solicitante, self.servicio)
        nuevo.refresh_from_db()
        efectivos = cfg.parametros_de(nuevo)
        self.assertEqual(efectivos["por_vencer_porcentaje"], 60)
        self.assertEqual(efectivos["sin_tomar_horas"], ConfiguracionNotificaciones.actual().sin_tomar_horas)
        with self.assertRaises(ValidationError):
            cfg.guardar_recordatorios(self.servicio, self.gestor_area, parametros={}, escalar_a="USUARIO")


# --- Alertas ------------------------------------------------------------------------------------------------


class AlertasTests(_Base, TestCase):
    def setUp(self):
        super().setUp()
        self.inicio = timezone.now() - timedelta(days=10)
        objetivo = self.inicio + timedelta(days=10, hours=2)
        # La restricción del ticket exige el compromiso completo: tiempo objetivo + fecha original + vigente.
        Ticket.objects.filter(pk=self.ticket.pk).update(
            radicado_en=self.inicio, tiempo_objetivo_cantidad=242, tiempo_objetivo_unidad="HORAS",
            fecha_objetivo_original=objetivo, fecha_objetivo_vigente=objetivo,
            usuario_responsable=self.responsable_directo,
        )
        self.ticket.refresh_from_db()

    def test_proximo_a_vencer_una_sola_vez(self):
        alertas.evaluar()
        alertas.evaluar()
        self.assertEqual(self._de(self.responsable_directo, "ALERTA_TICKET_POR_VENCER").count(), 1)
        self.assertEqual(alertas.estado_de_plazo(self.ticket), "por_vencer")

    def test_vencido_resuelve_el_aviso_previo_y_escala_a_quien_supervisa(self):
        alertas.evaluar()
        vencido = self.ticket.fecha_objetivo_vigente + timedelta(hours=25)
        alertas.evaluar(ahora=vencido)
        self.assertTrue(self._de(self.responsable_directo, "ALERTA_TICKET_VENCIDO").exists())
        self.assertEqual(Alerta.objects.get(tipo="ALERTA_TICKET_POR_VENCER").estado, Alerta.Estado.RESUELTA)
        escalamiento = self._de(self.gestor_area, "ESCALAMIENTO").get()
        self.assertTrue(escalamiento.requiere_accion)
        self.assertFalse(self._de(self.gestor_otra_area, "ESCALAMIENTO").exists(), "solo quien supervisa ESE alcance")

    def test_al_cerrarse_la_situacion_la_alerta_se_resuelve(self):
        alertas.evaluar()
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.RESUELTO)
        alertas.evaluar()
        self.assertFalse(Alerta.objects.filter(estado=Alerta.Estado.ACTIVA).exists())

    def test_recordatorios_de_informacion_hasta_el_maximo(self):
        solicitud = SolicitudInformacion.objects.create(
            ticket=self.ticket, solicitada_por=self.responsable_directo, destinatario=self.solicitante, mensaje="NIT",
        )
        SolicitudInformacion.objects.filter(pk=solicitud.pk).update(solicitada_en=timezone.now() - timedelta(days=30))
        alertas.evaluar()
        maximo = ConfiguracionNotificaciones.actual().maximo_recordatorios
        self.assertEqual(Alerta.objects.get(tipo="RECORDATORIO_INFORMACION").ocurrencia, maximo)
        alertas.evaluar(ahora=timezone.now() + timedelta(days=5))
        self.assertEqual(Alerta.objects.filter(tipo="RECORDATORIO_INFORMACION").count(), 1, "no pasa del máximo")

    def test_actividad_vencida_avisa_a_su_responsable(self):
        tarea = crear_tarea(titulo="Diseñar pieza", usuario_responsable=self.miembro_equipo,
                            fecha_limite=timezone.now() - timedelta(hours=1))
        alertas.evaluar()
        aviso = self._de(self.miembro_equipo, "ALERTA_TAREA_VENCIDA").get()
        self.assertIn("Diseñar pieza", aviso.resumen)
        self.assertEqual(Alerta.objects.get(tipo="ALERTA_TAREA_VENCIDA").objeto_id, tarea.pk)

    def test_umbral_configurable(self):
        configuracion = ConfiguracionNotificaciones.actual()
        configuracion.por_vencer_porcentaje = 99
        configuracion.save()
        cfg.olvidar_globales()
        # 236 h de 242 h ≈ 97,5 %: con el umbral general en 80 % estaría «por vencer»; con 99 % todavía no.
        self.assertEqual(alertas.estado_de_plazo(self.ticket, ahora=self.inicio + timedelta(days=9, hours=20)), "")
