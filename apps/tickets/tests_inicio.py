"""Bloque 1 — creación e inicio de tickets en las tres modalidades.

- SERVICIO: formulario inicial activo, obligatorios validados antes de radicar (sin cambios).
- PROCESO MANUAL: datos de inicio opcionales; sin datos previos a la radicación (sin campos, o con datos que
  pertenecen a la ejecución) se inicia directamente; las colecciones nacen vacías y no bloquean el inicio.
- PROCESO PROGRAMADO: se genera y radica solo, con su responsable inicial, sin diligenciamiento previo.
- Permisos: solicitante ≠ responsable; los ítems los registra quien atiende, no quien inicia.

Se entregan SIN ejecutar.
"""

from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.configuracion_ejecucion import activar_configuracion_ejecucion, preparar_configuracion_ejecucion
from apps.catalogo.models import Campo, Formulario, ProgramacionProceso, Servicio, ServicioResponsable
from apps.catalogo.operaciones import activar_servicio, datos_inicio_configurados, validar_publicacion
from apps.catalogo.tests import _permiso_get_or_create
from apps.catalogo.tests_fase4a import CLAVE, Usuario, _Base4A
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.tickets import datos_inicio, solicitud
from apps.tickets import parrilla as parrilla_ops
from apps.tickets.autorizacion import puede_editar_parrilla, puede_tomar
from apps.tickets.models import HistorialTicket, Ticket
from apps.tickets.operaciones import (
    crear_borrador,
    guardar_respuestas_borrador,
    radicar_ticket,
    tomar_ticket,
)


class _BaseInicio(_Base4A):
    def _preparar_inicio(self):
        self._preparar()
        self.solicitante = Usuario.objects.create_user("b1_solicitante", password=CLAVE)
        self.atiende = Usuario.objects.create_user("b1_atiende", password=CLAVE)
        _permiso_get_or_create(self.atiende, "tickets.atender", "Rol B1 atender")
        # El solicitante también tiene el permiso: aun así no puede tomar su propio ticket.
        _permiso_get_or_create(self.solicitante, "tickets.atender", "Rol B1 atender solicitante")

    def _formulario(self, nombre, campos):
        formulario = Formulario.objects.create(nombre=nombre)
        version = crear_nueva_version(formulario, self.admin)
        creados = {}
        for orden, (etiqueta, obligatorio) in enumerate(campos, start=1):
            creados[etiqueta] = Campo.objects.create(
                version=version, tipo="TEXTO", etiqueta=etiqueta, orden=orden, obligatorio=obligatorio
            )
        activar_version(formulario, version, self.admin)
        return formulario, creados

    def _publicar(self, nombre, *, tipo, campos=(), coleccion=False, en_ejecucion=False):
        workflow = self._flujo(f"Flujo {nombre}")
        formulario, creados = self._formulario(f"Entrada {nombre}", campos) if campos or tipo == Servicio.Tipo.SERVICIO else (None, {})
        servicio = Servicio.objects.create(
            nombre=nombre, categoria=self.categoria, tipo=tipo, workflow=workflow, formulario=formulario,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO, datos_inicio_en_ejecucion=en_ejecucion,
        )
        ServicioResponsable.objects.create(
            servicio=servicio, tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO, usuario=self.atiende
        )
        config = preparar_configuracion_ejecucion(servicio, self.admin)
        self._actividad(config, self._fase(workflow, "Recepción"), "Atender")
        activar_configuracion_ejecucion(servicio, config, self.admin)
        if coleccion:
            from apps.catalogo.parrilla import agregar_coleccion

            definicion = agregar_coleccion(servicio, self.admin, nombre_elemento="Ítem")
            version = definicion.formulario.versiones.get(numero=1)
            Campo.objects.create(version=version, tipo="TEXTO", etiqueta="Nombre del ítem", orden=1)
            activar_version(definicion.formulario, version, self.admin)
        servicio.refresh_from_db()
        activar_servicio(servicio, self.admin)
        servicio.refresh_from_db()
        return servicio, creados

    def _iniciar_por_la_vista(self, servicio):
        self.client.force_login(self.solicitante)
        entrada = self.client.get(reverse("tickets:solicitar", args=[servicio.pk]), follow=True)
        ticket = Ticket.objects.filter(solicitante=self.solicitante, detalle_servicio__servicio=servicio).latest("pk")
        return entrada, ticket


class ServicioInicioTests(_BaseInicio, TestCase):
    def setUp(self):
        self._preparar_inicio()

    def test_servicio_con_formulario_valido_radica_y_arranca_el_flujo(self):
        servicio, campos = self._publicar("Soporte", tipo=Servicio.Tipo.SERVICIO, campos=[("Motivo", True)])
        ticket = crear_borrador(self.solicitante, servicio)
        self.assertFalse(solicitud.inicio_directo(ticket))
        guardar_respuestas_borrador(ticket, self.solicitante, {campos["Motivo"].pk: "No enciende"})
        radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsNotNone(ticket.instancia_workflow_id)

    def test_servicio_con_formulario_incompleto_no_radica(self):
        servicio, _campos = self._publicar("Soporte incompleto", tipo=Servicio.Tipo.SERVICIO, campos=[("Motivo", True)])
        ticket = crear_borrador(self.solicitante, servicio)
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)

    def test_servicio_muestra_su_formulario_antes_de_radicar(self):
        servicio, _campos = self._publicar("Soporte vista", tipo=Servicio.Tipo.SERVICIO, campos=[("Motivo", True)])
        entrada, _ticket = self._iniciar_por_la_vista(servicio)
        self.assertTemplateUsed(entrada, "tickets/solicitud.html")

    def test_servicio_sin_formulario_no_se_publica(self):
        servicio = Servicio.objects.create(nombre="Sin entrada", categoria=self.categoria, tipo=Servicio.Tipo.SERVICIO)
        with self.assertRaises(ValidationError):
            validar_publicacion(servicio)


class ProcesoManualInicioTests(_BaseInicio, TestCase):
    def setUp(self):
        self._preparar_inicio()

    def test_con_formulario_previo_se_muestra_valida_y_permite_borrador(self):
        proceso, campos = self._publicar("Con datos", tipo=Servicio.Tipo.PROCESO, campos=[("Motivo", True)])
        entrada, ticket = self._iniciar_por_la_vista(proceso)
        self.assertTemplateUsed(entrada, "tickets/solicitud.html")
        self.assertFalse(solicitud.inicio_directo(ticket))
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.solicitante)
        guardar_respuestas_borrador(ticket, self.solicitante, {campos["Motivo"].pk: "Cierre de mes"})
        radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_sin_formulario_se_inicia_directamente(self):
        proceso, _ = self._publicar("Sin datos", tipo=Servicio.Tipo.PROCESO)
        self.assertFalse(datos_inicio_configurados(proceso))  # el formulario técnico vacío no cuenta
        entrada, ticket = self._iniciar_por_la_vista(proceso)
        self.assertTrue(solicitud.inicio_directo(ticket))
        self.assertTemplateUsed(entrada, "tickets/solicitud_revision.html")
        self.assertContains(entrada, "Iniciar proceso")
        self.assertContains(entrada, "no pide información para iniciar")
        self.client.post(reverse("tickets:enviar", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(ticket.solicitante, self.solicitante)
        self.assertIsNotNone(ticket.instancia_workflow_id)
        self.assertTrue(HistorialTicket.objects.filter(ticket=ticket, tipo_evento="RADICADO").exists())

    def test_con_datos_en_ejecucion_no_los_pide_al_iniciar(self):
        proceso, _ = self._publicar("Datos en ejecución", tipo=Servicio.Tipo.PROCESO, campos=[("Motivo", True)], en_ejecucion=True)
        entrada, ticket = self._iniciar_por_la_vista(proceso)
        self.assertTrue(solicitud.inicio_directo(ticket))
        self.assertContains(entrada, "se completan durante su ejecución")
        self.client.post(reverse("tickets:enviar", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertTrue(datos_inicio.requiere_diligenciamiento(ticket))
        self.assertFalse(datos_inicio.datos_completos(ticket))

    def test_solo_con_coleccion_inicia_con_la_coleccion_vacia(self):
        proceso, _ = self._publicar("Solo colección", tipo=Servicio.Tipo.PROCESO, coleccion=True)
        _entrada, ticket = self._iniciar_por_la_vista(proceso)
        self.client.post(reverse("tickets:enviar", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        parrilla = parrilla_ops.parrilla_de(ticket)
        self.assertIsNotNone(parrilla)
        self.assertFalse(parrilla_ops.elementos_vigentes(parrilla).exists())

    def test_con_formulario_y_coleccion(self):
        proceso, campos = self._publicar("Ambos", tipo=Servicio.Tipo.PROCESO, campos=[("Motivo", True)], coleccion=True)
        ticket = crear_borrador(self.solicitante, proceso)
        self.assertFalse(solicitud.inicio_directo(ticket))
        guardar_respuestas_borrador(ticket, self.solicitante, {campos["Motivo"].pk: "Plan"})
        radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertFalse(parrilla_ops.elementos_vigentes(parrilla_ops.parrilla_de(ticket)).exists())

    def test_studio_no_presenta_el_formulario_tecnico_como_configurado(self):
        proceso, _ = self._publicar("Studio técnico", tipo=Servicio.Tipo.PROCESO)
        self.client.force_login(self.admin)
        pagina = self.client.get(f"{reverse('catalogo:studio', args=[proceso.pk])}?tab=entrada")
        self.assertTrue(pagina.context["datos_inicio_vacios"])
        self.assertContains(pagina, "Este proceso no pide datos de inicio")
        self.assertNotContains(pagina, 'class="constructor-barra" role="toolbar"')
        from apps.catalogo import publicacion

        self.assertNotIn("formulario", [p.clave for p in publicacion.piezas(proceso)])


class ProcesoProgramadoInicioTests(_BaseInicio, TestCase):
    def setUp(self):
        self._preparar_inicio()

    def _programar(self, proceso):
        from apps.catalogo.programacion import asegurar_responsable_inicial, configurar_programacion

        responsable = asegurar_responsable_inicial(proceso, self.admin, (ServicioResponsable.TipoResponsable.USUARIO, self.atiende))
        programacion = configurar_programacion(
            proceso, self.admin, dia_creacion=1, periodo=ProgramacionProceso.Periodo.MES_ACTUAL,
            responsable_inicial=responsable,
        )
        ProgramacionProceso.objects.filter(pk=programacion.pk).update(activada_desde=date(2020, 1, 1))
        return ProgramacionProceso.objects.get(pk=programacion.pk)

    def _generar(self, proceso):
        from apps.tickets.programadas import generar_ejecucion_programada

        ejecucion = generar_ejecucion_programada(self._programar(proceso))
        self.assertIsNotNone(ejecucion)
        return ejecucion.ticket

    def test_sin_datos_genera_y_radica_con_el_responsable_inicial(self):
        proceso, _ = self._publicar("Programado vacío", tipo=Servicio.Tipo.PROCESO, coleccion=True)
        ticket = self._generar(proceso)
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsNone(ticket.solicitante_id)
        self.assertEqual(ticket.usuario_responsable, self.atiende)
        self.assertIsNotNone(ticket.instancia_workflow_id)
        self.assertFalse(datos_inicio.requiere_diligenciamiento(ticket))
        self.assertFalse(parrilla_ops.elementos_vigentes(parrilla_ops.parrilla_de(ticket)).exists())

    def test_con_formulario_se_diligencia_dentro_de_la_ejecucion(self):
        proceso, _ = self._publicar("Programado con datos", tipo=Servicio.Tipo.PROCESO, campos=[("Motivo", True)])
        ticket = self._generar(proceso)
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertTrue(ticket.datos_inicio_en_ejecucion)
        self.assertTrue(datos_inicio.requiere_diligenciamiento(ticket))
        self.assertFalse(datos_inicio.datos_completos(ticket))


class PermisosDeInicioYEdicionTests(_BaseInicio, TestCase):
    def setUp(self):
        self._preparar_inicio()

    def test_el_iniciador_no_atiende_y_el_responsable_configurado_registra_items(self):
        proceso, _ = self._publicar("Permisos", tipo=Servicio.Tipo.PROCESO, coleccion=True)
        ticket = crear_borrador(self.solicitante, proceso)
        radicar_ticket(ticket, self.solicitante)  # inicia sin ser responsable
        ticket.refresh_from_db()
        self.assertFalse(puede_tomar(self.solicitante, ticket))
        self.assertFalse(puede_editar_parrilla(self.solicitante, ticket))
        self.assertTrue(puede_tomar(self.atiende, ticket))
        tomar_ticket(ticket, self.atiende)
        ticket.refresh_from_db()
        self.assertTrue(puede_editar_parrilla(self.atiende, ticket))
        self.assertFalse(puede_editar_parrilla(self.solicitante, ticket))
        parrilla_ops.crear_elemento(ticket, self.atiende)
        self.assertEqual(parrilla_ops.elementos_vigentes(parrilla_ops.parrilla_de(ticket)).count(), 1)
        with self.assertRaises((PermissionDenied, ValidationError)):
            parrilla_ops.crear_elemento(ticket, self.solicitante)


class CompatibilidadHistoricaTests(_BaseInicio, TestCase):
    def setUp(self):
        self._preparar_inicio()

    def test_un_ticket_existente_conserva_su_version_congelada(self):
        proceso, campos = self._publicar("Histórico", tipo=Servicio.Tipo.PROCESO, campos=[("Motivo", False)])
        ticket = crear_borrador(self.solicitante, proceso)
        radicar_ticket(ticket, self.solicitante)
        congelada = ticket.detalle_servicio.formulario_version_id
        # Cambia el formulario del proceso: el ticket ya radicado no cambia y sigue sin inicio directo.
        nueva = crear_nueva_version(proceso.formulario, self.admin)
        activar_version(proceso.formulario, nueva, self.admin)
        ticket.refresh_from_db()
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, congelada)
        self.assertEqual(ticket.respuesta_formulario.formulario_version_id, congelada)
        self.assertFalse(solicitud.inicio_directo(ticket))
        self.assertIn(campos["Motivo"], ticket.respuesta_formulario.formulario_version.campos.all())
