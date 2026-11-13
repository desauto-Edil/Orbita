"""Bloque 5.6 — preparación automática de la demostración (`preparar_demo_orbita`) y operaciones de edición de
formularios extraídas del Studio (`apps.catalogo.edicion_formulario`). Se entregan SIN ejecutar."""

from datetime import date
from io import StringIO
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.catalogo import publicacion
from apps.catalogo.demo import contrato, fases, preparacion
from apps.catalogo.demo.participantes import Participantes
from apps.catalogo.edicion_formulario import guardar_campo, guardar_regla
from apps.catalogo.gacetas import errores_de_uso_por_item, normalizar as normalizar_gaceta
from apps.catalogo.models import (
    BloqueOperativo,
    Campo,
    Categoria,
    DefinicionGaceta,
    Formulario,
    ProgramacionProceso,
    RecursoDemo,
    Servicio,
    ServicioResponsable,
    TransicionBloqueOperativo,
)
from apps.catalogo.operaciones import errores_para_publicar
from apps.catalogo.tests import _permiso_get_or_create
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.core.models import Area, Equipo, MiembroEquipo, RegistroAuditoria, Usuario
from apps.workflows.models import Workflow
from apps.core.pruebas import correo_de_prueba, persona

CLAVE = "Clave-Segura-Demo-56"
HOY_PROGRAMADO = date(2026, 10, 20)


def _localdate(dia):
    return mock.patch("django.utils.timezone.localdate", return_value=dia)


class _EscenarioDemo:
    PERMISOS = ("catalogo.administrar", "formulario.administrar", "workflows.administrar", "workflows.vincular")

    def _preparar(self):
        self.admin = Usuario.objects.create_user("demo_admin@pruebas.orbita")
        for codigo in self.PERMISOS:
            _permiso_get_or_create(self.admin, codigo, f"Rol demo {codigo}")
        crear = lambda alias, **campos: Usuario.objects.create_user(correo_de_prueba(alias), **campos)  # noqa: E731
        self.planificador = crear("demo_planificador")
        self.productor = crear("demo_productor")
        self.aprobador = crear("demo_aprobador")
        self.revisor = crear("demo_revisor")
        self.coordinador = crear("demo_coordinador")
        self.disenador = crear("demo_disenador")
        self.comercial = crear("demo_comercial")
        self.solicitante = crear("demo_solicitante")
        self.sin_permisos = crear("demo_sin_permisos")
        # Publicar exige una ruta de atención: quienes atienden tienen `tickets.atender`.
        for usuario in (self.coordinador, self.disenador, self.comercial):
            _permiso_get_or_create(usuario, "tickets.atender", f"Rol demo atender {usuario.email}")
        self.diseno = Equipo.objects.create(nombre="Diseño demo")
        MiembroEquipo.objects.create(equipo=self.diseno, usuario=self.disenador)
        self.area = Area.objects.create(nombre="Mercadeo demo", codigo="MKT-DEMO")

    def _opciones(self, **extra):
        opciones = {
            "planificador": "demo_planificador", "productor": "demo_productor", "aprobador": "demo_aprobador",
            "revisores": "demo_revisor", "coordinador": "demo_coordinador", "equipo_diseno": "equipo:Diseño demo",
            "responsable_presentaciones": "demo_comercial", "dia_creacion": 20, "periodo": "MES_SIGUIENTE",
        }
        opciones.update(extra)
        return opciones

    def _preparar_caso(self, codigo, **extra):
        p = Participantes.desde_opciones(self._opciones(**extra))
        return preparacion.preparar_caso(codigo, self.admin, p, categoria_nombre="Demostración Órbita", sufijo=" · Demo")

    def _preparar_todos(self):
        return {codigo: self._preparar_caso(codigo)[0] for codigo in contrato.CASOS}

    def _comando(self, fase, *args):
        salida = StringIO()
        call_command("preparar_demo_orbita", "--fase", fase, "--actor", "demo_admin", *args, stdout=salida)
        return salida.getvalue()

    def _conteos(self):
        return {
            modelo.__name__: modelo.objects.count()
            for modelo in (Servicio, Campo, BloqueOperativo, TransicionBloqueOperativo, DefinicionGaceta, Workflow,
                           Categoria, ProgramacionProceso, RecursoDemo)
        }


class PreparacionDemoTests(_EscenarioDemo, TestCase):
    def setUp(self):
        self._preparar()

    def test_prepara_los_cuatro_casos_completos_y_sin_publicar(self):
        recursos = self._preparar_todos()
        self.assertEqual(RecursoDemo.objects.count(), 4)
        for codigo, recurso in recursos.items():
            with self.subTest(caso=codigo):
                self.assertFalse(recurso.servicio.activo)  # preparar no publica
                self.assertEqual(fases.discrepancias(recurso), [])
                self.assertEqual(recurso.servicio.tipo, contrato.CONTRATOS[codigo].tipo)
        # La programación mensual solo se crea al publicar.
        self.assertFalse(ProgramacionProceso.objects.exists())
        # Las plantillas propias quedan publicadas (requisito para vincularlas) y no las comparte nadie.
        for recurso in (recursos[contrato.PARRILLA], recursos[contrato.WEBINAR]):
            self.assertIsNotNone(recurso.workflow.version_activa_id)
            self.assertEqual(list(Servicio.objects.filter(workflow=recurso.workflow)), [recurso.servicio])
        self.assertIsNone(recursos[contrato.PIEZA_GRAFICA].workflow)

    def test_es_idempotente(self):
        self._preparar_todos()
        antes, auditoria = self._conteos(), RegistroAuditoria.objects.count()
        for codigo in contrato.CASOS:
            recurso, creado = self._preparar_caso(codigo)
            self.assertFalse(creado)
        self.assertEqual(self._conteos(), antes)
        self.assertEqual(RegistroAuditoria.objects.count(), auditoria)

    def test_la_simulacion_no_persiste_nada(self):
        antes = self._conteos()
        salida = self._comando(
            "preparar", "--simular", "--planificador", "demo_planificador", "--productor", "demo_productor",
            "--aprobador", "demo_aprobador", "--coordinador", "demo_coordinador",
            "--equipo-diseno", "equipo:Diseño demo", "--responsable-presentaciones", "demo_comercial",
        )
        self.assertIn("Simulación", salida)
        self.assertIn("Preparado", salida)
        self.assertEqual(self._conteos(), antes)

    def test_un_actor_sin_permisos_no_crea_nada(self):
        antes = self._conteos()
        with self.assertRaises(CommandError):
            call_command(
                "preparar_demo_orbita", "--fase", "preparar", "--actor", "demo_sin_permisos", "--casos", "pieza",
                "--equipo-diseno", "equipo:Diseño demo", stdout=StringIO(),
            )
        self.assertEqual(self._conteos(), antes)

    def test_un_parametro_faltante_no_deja_nada_a_medias(self):
        antes = self._conteos()
        with self.assertRaisesMessage(ValidationError, "--productor"):
            self._preparar_caso(contrato.PARRILLA, productor=None)
        self.assertEqual(self._conteos(), antes)

    def test_no_crea_usuarios_y_rechaza_los_inexistentes(self):
        usuarios = Usuario.objects.count()
        with self.assertRaisesMessage(ValidationError, "no existe una persona"):
            Participantes.desde_opciones(self._opciones(planificador="no_existe"))
        self._preparar_todos()
        self.assertEqual(Usuario.objects.count(), usuarios)


class ContratoParrillaTests(_EscenarioDemo, TestCase):
    def setUp(self):
        self._preparar()
        self.recurso, _ = self._preparar_caso(contrato.PARRILLA)
        self.config = self.recurso.servicio.configuraciones_ejecucion.get()

    def test_recorrido_individual_en_el_orden_del_contrato(self):
        recorrido = self.config.bloques.get(nombre=contrato.RECORRIDO)
        self.assertEqual(recorrido.tipo, "RECORRIDO_ITEMS")
        # UX-4 (contrato v3): la lluvia de ideas se envía completa; cada idea entra por su aprobación.
        self.assertEqual(recorrido.configuracion["ingreso"], "AL_ENVIAR")
        self.assertEqual(recorrido.configuracion["envio"], "CONJUNTO")
        self.assertEqual(recorrido.configuracion["etapa_borradores"], "Lluvia de ideas")
        self.assertEqual(
            recorrido.configuracion["paso_al_enviar"], self.config.bloques.get(nombre="Aprobación de la idea").clave
        )
        self.assertEqual(
            recorrido.configuracion["columnas_requeridas_ingreso"],
            [contrato.COPY, contrato.TIPO_PIEZA, contrato.CONTENIDO, contrato.FECHA_PUBLICACION],
        )
        self.assertTrue(recorrido.configuracion["espera_alcance_cerrado"])
        self.assertFalse(self.config.bloques.filter(nombre__in=["Idea", "Planear la parrilla"]).exists())
        pasos = list(recorrido.contenidos.order_by("orden", "pk").values_list("nombre", "tipo", "rama"))
        self.assertEqual(pasos, [
            ("Ajustar la idea", "ACTIVIDAD", "ITEM"),
            ("Aprobación de la idea", "APROBACION", "ITEM"),
            ("Producción", "ACTIVIDAD", "ITEM"),
            ("Aprobación de la pieza", "APROBACION", "ITEM"),
            ("Revisión colaborativa de la pieza", "GACETA", "ITEM"),
            ("Publicación", "ACTIVIDAD", "ITEM"),
            ("Evidencia de publicación", "ACTIVIDAD", "ITEM"),
        ])
        devoluciones = set(
            TransicionBloqueOperativo.objects.filter(bloque_origen__version=self.config, resultado_aprobacion="DEVUELTA")
            .values_list("bloque_origen__nombre", "bloque_destino__nombre")
        )
        self.assertEqual(
            devoluciones, {("Aprobación de la idea", "Ajustar la idea"), ("Aprobación de la pieza", "Producción")}
        )
        self.assertEqual(
            self.config.bloques.get(nombre="Producción").configuracion["columnas_requeridas"], [contrato.ARCHIVO_PIEZA]
        )
        self.assertEqual(fases.discrepancias(self.recurso), [])

    def test_gaceta_dentro_del_recorrido_y_compatible_con_la_revision_por_item(self):
        paso = self.config.bloques.get(nombre="Revisión colaborativa de la pieza")
        gaceta = DefinicionGaceta.objects.get(servicio=self.recurso.servicio)
        self.assertEqual(paso.configuracion, {"gaceta": gaceta.clave})
        cfg, _ = normalizar_gaceta(gaceta.configuracion)
        self.assertEqual(errores_de_uso_por_item(cfg), [])
        self.assertEqual(cfg["ingreso"], "MANUAL")
        self.assertTrue(cfg["exige_item_completo"])
        self.assertTrue(cfg["obligatoria"])
        # Ningún bloque GACETA del flujo principal espera la gaceta (sin sincronización global).
        self.assertFalse(self.config.bloques.filter(tipo="GACETA", contenedor__isnull=True).exists())

    def test_publicacion_con_fecha_programada_y_evidencia_obligatoria(self):
        publicar = self.config.bloques.get(nombre="Publicación").configuracion
        self.assertEqual(publicar["fecha_prevista_campo"], f"item.{contrato.FECHA_PUBLICACION}")
        self.assertEqual(publicar["fecha_limite_campo"], f"item.{contrato.FECHA_PUBLICACION}")
        self.assertEqual(publicar["usuario_id"], self.planificador.pk)
        evidencia = self.config.bloques.get(nombre="Evidencia de publicación").configuracion
        self.assertTrue(evidencia["requiere_evidencia"])
        produccion = self.config.bloques.get(nombre="Producción").configuracion
        self.assertEqual(produccion["usuario_id"], self.productor.pk)
        self.assertEqual(produccion["columnas_editables"], [contrato.ARCHIVO_PIEZA])

    def test_columnas_con_el_archivo_producido(self):
        definicion = self.recurso.servicio.definiciones_parrilla.get()
        columnas = {c.clave: c for c in definicion.formulario.versiones.get().campos.all()}
        self.assertEqual(columnas[contrato.CONTENIDO].tipo, "TEXTO_LARGO")
        self.assertEqual(columnas[contrato.FECHA_PUBLICACION].tipo, "FECHA")
        self.assertEqual(columnas[contrato.ARCHIVO_PIEZA].tipo, "ARCHIVO")
        self.assertEqual(
            set(columnas[contrato.TIPO_PIEZA].opciones.values_list("valor", flat=True)),
            {"post", "reel", "historia", "carrusel"},
        )


class PublicacionDemoTests(_EscenarioDemo, TestCase):
    def setUp(self):
        self._preparar()
        self.recursos = self._preparar_todos()

    def _publicar_todos(self, dia=HOY_PROGRAMADO):
        with _localdate(dia):
            return {codigo: fases.publicar(recurso, self.admin) for codigo, recurso in self.recursos.items()}

    def test_publica_los_cuatro_con_las_validaciones_actuales(self):
        self._publicar_todos()
        for codigo, recurso in self.recursos.items():
            with self.subTest(caso=codigo):
                recurso.servicio.refresh_from_db()
                self.assertTrue(recurso.servicio.activo)
                self.assertEqual(errores_para_publicar(recurso.servicio), [])
                self.assertFalse([p for p in publicacion.piezas(recurso.servicio) if p.pendiente])

    def test_la_parrilla_queda_programada_mensualmente(self):
        self._publicar_todos()
        programacion = ProgramacionProceso.objects.get(servicio=self.recursos[contrato.PARRILLA].servicio)
        self.assertTrue(programacion.activa)
        self.assertEqual((programacion.dia_creacion, programacion.periodo), (20, "MES_SIGUIENTE"))
        self.assertEqual(programacion.responsable_inicial.usuario, self.planificador)
        self.assertEqual(programacion.activada_desde, HOY_PROGRAMADO)

    def test_publicar_otra_vez_no_cambia_nada(self):
        self._publicar_todos()
        antes, auditoria = self._conteos(), RegistroAuditoria.objects.count()
        lineas = self._publicar_todos(date(2026, 10, 27))
        self.assertIn("Ya estaba publicado.", lineas[contrato.PARRILLA])
        self.assertEqual(self._conteos(), antes)
        self.assertEqual(RegistroAuditoria.objects.count(), auditoria)
        programacion = ProgramacionProceso.objects.get(servicio=self.recursos[contrato.PARRILLA].servicio)
        self.assertEqual(programacion.activada_desde, HOY_PROGRAMADO)  # no se reinició

    def test_verificar_no_modifica_datos(self):
        self._publicar_todos()
        antes, auditoria = self._conteos(), RegistroAuditoria.objects.count()
        salida = self._comando("verificar")
        self.assertIn("Coincide con el contrato", salida)
        self.assertIn("Publicable", salida)
        self.assertEqual(self._conteos(), antes)
        self.assertEqual(RegistroAuditoria.objects.count(), auditoria)

    def test_la_configuracion_de_un_ticket_queda_congelada(self):
        from apps.catalogo import flujo_api
        from apps.catalogo.configuracion_ejecucion import preparar_configuracion_ejecucion

        self._publicar_todos()
        recurso = self.recursos[contrato.PARRILLA]
        with _localdate(HOY_PROGRAMADO):
            fases.ejemplos(recurso, self.admin)
        ticket = recurso.servicio.tickets.get().ticket
        congelada = ticket.instancia_workflow.configuracion_ejecucion_version_id
        servicio = recurso.servicio
        borrador = preparar_configuracion_ejecucion(servicio, self.admin)
        flujo_api.editar_actividad(borrador, borrador.bloques.get(nombre="Cerrar la parrilla"), self.admin, nombre="Cierre del mes")
        publicacion.publicar_cambios(servicio, self.admin)
        servicio.refresh_from_db()
        ticket.instancia_workflow.refresh_from_db()
        self.assertNotEqual(servicio.configuracion_ejecucion_activa_id, congelada)
        self.assertEqual(ticket.instancia_workflow.configuracion_ejecucion_version_id, congelada)


class EjemplosDemoTests(_EscenarioDemo, TestCase):
    def setUp(self):
        self._preparar()
        self.recursos = self._preparar_todos()

    def _publicar(self, dia):
        with _localdate(dia):
            for recurso in self.recursos.values():
                fases.publicar(recurso, self.admin)

    def test_el_programado_no_se_fuerza_fuera_del_dia(self):
        self._publicar(date(2026, 10, 26))  # activada el 26; el día de creación es el 20
        with _localdate(date(2026, 10, 26)), mock.patch(
            "apps.tickets.programadas.reconciliar_ejecuciones_programadas"
        ) as reconciliador:
            lineas = fases.ejemplos(self.recursos[contrato.PARRILLA], self.admin)
        reconciliador.assert_not_called()
        self.assertIn("No hay ninguna ejecución vencida", " ".join(lineas))
        self.assertIn("2026-11-20", " ".join(lineas))
        self.assertFalse(self.recursos[contrato.PARRILLA].servicio.tickets.exists())

    def test_el_programado_genera_su_ejecucion_el_dia_programado_sin_duplicar(self):
        self._publicar(HOY_PROGRAMADO)
        recurso = self.recursos[contrato.PARRILLA]
        with _localdate(HOY_PROGRAMADO):
            primera = fases.ejemplos(recurso, self.admin)
            segunda = fases.ejemplos(recurso, self.admin)
        self.assertIn("Ejecución generada", primera[0])
        self.assertIn("No hay ninguna ejecución vencida", " ".join(segunda))
        ticket = recurso.servicio.tickets.get().ticket
        self.assertEqual(ticket.etiqueta, "Noviembre 2026")
        self.assertEqual((ticket.origen, ticket.estado), ("PROGRAMACION", "RADICADO"))
        self.assertIsNone(ticket.solicitante)
        self.assertEqual(ticket.usuario_responsable, self.planificador)

    def test_publicar_con_el_dia_de_hoy_permite_el_ejemplo_sin_alterar_fechas(self):
        recurso = self.recursos[contrato.PARRILLA]
        with _localdate(date(2026, 10, 26)):
            self._publicar(date(2026, 10, 26))
            fases.publicar(recurso, self.admin, dia_creacion=26)  # la regla normal: reinicia la programación hoy
            lineas = fases.ejemplos(recurso, self.admin)
        programacion = ProgramacionProceso.objects.get(servicio=recurso.servicio)
        self.assertEqual((programacion.dia_creacion, programacion.activada_desde), (26, date(2026, 10, 26)))
        self.assertIn("Ejecución generada", lineas[0])
        recurso.refresh_from_db()
        self.assertEqual(recurso.parametros["dia_creacion"], 26)
        with self.assertRaisesMessage(ValidationError, "--dia-creacion"):
            fases.publicar(recurso, self.admin, dia_creacion=31)

    def test_cada_idea_hace_su_recorrido_y_se_aprueba_o_devuelve_sola(self):
        from apps.tickets import parrilla as parrilla_ops
        from apps.tickets.operaciones import iniciar_atencion_ticket
        from apps.workflows import recorridos
        from apps.workflows.integracion import completar_tarea_workflow, resolver_aprobacion_workflow
        from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaWorkflow, TareaWorkflow

        self._publicar(HOY_PROGRAMADO)
        with _localdate(HOY_PROGRAMADO):
            fases.ejemplos(self.recursos[contrato.PARRILLA], self.admin)
        ticket = self.recursos[contrato.PARRILLA].servicio.tickets.get().ticket
        iniciar_atencion_ticket(ticket, self.planificador)
        columnas = {c.clave: c for c in parrilla_ops.columnas_de(ticket.parrilla)}

        def idea(copy, fecha):
            return parrilla_ops.crear_elemento(ticket, self.planificador, {
                columnas[contrato.COPY].pk: copy, columnas[contrato.TIPO_PIEZA].pk: "post",
                columnas[contrato.CONTENIDO].pk: "Pieza de ejemplo", columnas[contrato.FECHA_PUBLICACION].pk: fecha,
            })

        a, b = idea("Idea A", "2026-11-10"), idea("Idea B", "2026-11-18")
        incompleta = parrilla_ops.crear_elemento(ticket, self.planificador, {columnas[contrato.COPY].pk: "Sin datos"})

        def ultima(elemento):
            return InstanciaWorkflow.objects.get(elemento=elemento).ejecuciones_etapa.order_by("-orden").first()

        # Registrar NO envía: las tres son borradores de la lluvia de ideas y el flujo sigue esperando.
        self.assertFalse(InstanciaWorkflow.objects.filter(elemento__in=[a, b, incompleta]).exists())
        with self.assertRaisesMessage(ValidationError, "Se envían todos juntos"):
            recorridos.enviar_elemento(a, self.planificador)  # una sola no se envía
        with self.assertRaisesMessage(ValidationError, "Sin datos"):
            recorridos.enviar_conjunto(ticket, ticket.parrilla, self.planificador)  # una incompleta frena todo
        self.assertFalse(InstanciaWorkflow.objects.filter(elemento__in=[a, b]).exists())
        with self.assertRaisesMessage(ValidationError, "dentro de"):
            recorridos.enviar_conjunto(
                ticket, ticket.parrilla, self.planificador, cambios={b: {columnas[contrato.FECHA_PUBLICACION].pk: "2026-12-01"}}
            )
        parrilla_ops.retirar_elemento(incompleta, self.planificador)
        recorridos.enviar_conjunto(ticket, ticket.parrilla, self.planificador)
        for elemento in (a, b):
            self.assertEqual(ultima(elemento).bloque_operativo.nombre, "Aprobación de la idea")
        ticket.parrilla.refresh_from_db()
        self.assertTrue(ticket.parrilla.alcance_cerrado)  # enviada la lluvia de ideas, no se agregan más
        with self.assertRaises(ValidationError):
            idea("Tardía", "2026-11-20")

        def decidir(elemento, decision, observacion=""):
            participacion = EsquemaAprobacionWorkflow.objects.get(instancia_etapa=ultima(elemento)).esquema.participaciones.get()
            resolver_aprobacion_workflow(participacion, self.aprobador, decision=decision, observacion=observacion)

        decidir(a, "APROBADA")
        decidir(b, "DEVUELTA", "Ajusta el copy.")
        produccion = ultima(a)
        self.assertEqual(produccion.bloque_operativo.nombre, "Producción")
        tarea_produccion = TareaWorkflow.objects.get(instancia_etapa=produccion).tarea
        self.assertEqual(tarea_produccion.usuario_responsable, self.productor)
        ajuste = ultima(b)
        self.assertEqual(ajuste.bloque_operativo.nombre, "Ajustar la idea")
        completar_tarea_workflow(TareaWorkflow.objects.get(instancia_etapa=ajuste).tarea, self.planificador)
        self.assertEqual(ultima(b).bloque_operativo.nombre, "Aprobación de la idea")
        # La pieza no va a aprobación sin su archivo.
        with self.assertRaisesMessage(ValidationError, "Archivo de la pieza"):
            completar_tarea_workflow(tarea_produccion, self.productor)

    def test_los_solicitados_se_radican_una_sola_vez(self):
        self._publicar(HOY_PROGRAMADO)
        for codigo in (contrato.WEBINAR, contrato.PIEZA_GRAFICA, contrato.PRESENTACIONES):
            with self.subTest(caso=codigo):
                recurso = self.recursos[codigo]
                primera = fases.ejemplos(recurso, self.admin, solicitante=self.solicitante, area=self.area)
                segunda = fases.ejemplos(recurso, self.admin, solicitante=self.solicitante, area=self.area)
                self.assertIn("Ticket radicado", primera[0])
                self.assertIn("Ya existe", segunda[0])
                ticket = recurso.servicio.tickets.get().ticket
                self.assertEqual((ticket.estado, ticket.solicitante), ("RADICADO", self.solicitante))

    def test_el_webinar_sin_area_no_se_inventa(self):
        self._publicar(HOY_PROGRAMADO)
        lineas = fases.ejemplos(self.recursos[contrato.WEBINAR], self.admin, solicitante=self.solicitante)
        self.assertIn("--area-ejemplo", lineas[0])
        self.assertFalse(self.recursos[contrato.WEBINAR].servicio.tickets.exists())


class RetiroDemoTests(_EscenarioDemo, TestCase):
    def setUp(self):
        self._preparar()
        self.recursos = self._preparar_todos()
        with _localdate(HOY_PROGRAMADO):
            for recurso in self.recursos.values():
                fases.publicar(recurso, self.admin)
            fases.ejemplos(self.recursos[contrato.PARRILLA], self.admin)
        # Un ejemplo radicado por el propio actor (puede cancelarlo) y otro por otra persona (no puede).
        fases.ejemplos(self.recursos[contrato.PIEZA_GRAFICA], self.admin, solicitante=self.admin)
        fases.ejemplos(self.recursos[contrato.PRESENTACIONES], self.admin, solicitante=self.solicitante)

    def test_retira_sin_borrar_y_solo_lo_de_la_demo(self):
        antes = self._conteos()
        lineas = {codigo: fases.retirar(recurso, self.admin) for codigo, recurso in self.recursos.items()}
        self.assertEqual(self._conteos(), antes)  # nada se borra
        for recurso in self.recursos.values():
            recurso.refresh_from_db()
            recurso.servicio.refresh_from_db()
            self.assertFalse(recurso.servicio.activo)
            self.assertIsNotNone(recurso.retirado_en)
        self.assertFalse(ProgramacionProceso.objects.get(servicio=self.recursos[contrato.PARRILLA].servicio).activa)
        pieza = self.recursos[contrato.PIEZA_GRAFICA].servicio.tickets.get().ticket
        self.assertEqual(pieza.estado, "CANCELADO")
        presentacion = self.recursos[contrato.PRESENTACIONES].servicio.tickets.get().ticket
        self.assertEqual(presentacion.estado, "RADICADO")
        self.assertIn("Sin cancelar", " ".join(lineas[contrato.PRESENTACIONES]))

    def test_retirar_dos_veces_y_volver_a_publicar(self):
        for recurso in self.recursos.values():
            fases.retirar(recurso, self.admin)
        auditoria = RegistroAuditoria.objects.count()
        fases.retirar(self.recursos[contrato.WEBINAR], self.admin)
        self.assertEqual(RegistroAuditoria.objects.count(), auditoria)
        fases.publicar(self.recursos[contrato.WEBINAR], self.admin)
        self.recursos[contrato.WEBINAR].refresh_from_db()
        self.assertIsNone(self.recursos[contrato.WEBINAR].retirado_en)
        self.assertTrue(self.recursos[contrato.WEBINAR].servicio.activo)


class CompatibilidadConLoAjenoTests(_EscenarioDemo, TestCase):
    """Una configuración ajena con el MISMO nombre y una programación inactiva (como las 1, 3, 5 y 6 del
    entorno de pruebas) no se toca en ninguna fase."""

    def setUp(self):
        self._preparar()
        categoria = Categoria.objects.create(nombre="Procesos reales")
        self.ajeno = Servicio.objects.create(
            nombre="Parrilla mensual de publicaciones · Demo", categoria=categoria, tipo=Servicio.Tipo.PROCESO,
            activo=False,
        )
        responsable = ServicioResponsable.objects.create(servicio=self.ajeno, tipo_responsable="USUARIO", usuario=self.planificador)
        self.programacion = ProgramacionProceso.objects.create(
            servicio=self.ajeno, activa=False, frecuencia="MENSUAL", dia_creacion=20, periodo="MES_SIGUIENTE",
            activada_desde=date(2026, 1, 1), responsable_inicial=responsable,
        )

    def test_ninguna_fase_toca_lo_ajeno(self):
        foto = (self.ajeno.actualizado_en, self.programacion.actualizado_en)
        recursos = self._preparar_todos()
        self.assertNotEqual(recursos[contrato.PARRILLA].servicio_id, self.ajeno.pk)
        with _localdate(HOY_PROGRAMADO):
            for recurso in recursos.values():
                fases.publicar(recurso, self.admin)
            fases.ejemplos(recursos[contrato.PARRILLA], self.admin)
        for recurso in recursos.values():
            fases.retirar(recurso, self.admin)
        self.ajeno.refresh_from_db()
        self.programacion.refresh_from_db()
        self.assertEqual((self.ajeno.actualizado_en, self.programacion.actualizado_en), foto)
        self.assertFalse(self.ajeno.activo)
        self.assertFalse(self.programacion.activa)
        self.assertFalse(self.ajeno.tickets.exists())


class EdicionFormularioTests(_EscenarioDemo, TestCase):
    """Las operaciones que comparten el Studio y la demo hacen cumplir las mismas reglas."""

    def setUp(self):
        self._preparar()
        self.formulario = Formulario.objects.create(nombre="Formulario edición")
        self.version = crear_nueva_version(self.formulario, self.admin)

    def test_exige_permisos(self):
        with self.assertRaises(PermissionDenied):
            guardar_campo(self.version, self.sin_permisos, tipo="TEXTO", etiqueta="Nombre")

    def test_equipo_solo_en_columnas_de_una_coleccion(self):
        with self.assertRaisesMessage(ValidationError, "solo existe en las columnas"):
            guardar_campo(self.version, self.admin, tipo="EQUIPO", etiqueta="Equipo")
        self.assertEqual(guardar_campo(self.version, self.admin, tipo="EQUIPO", etiqueta="Equipo", en_elementos=True).tipo, "EQUIPO")

    def test_la_clave_se_genera_y_se_conserva_al_editar(self):
        campo = guardar_campo(self.version, self.admin, tipo="TEXTO", etiqueta="Nombre completo")
        clave = campo.clave
        self.assertTrue(clave)
        guardar_campo(self.version, self.admin, campo=campo, tipo="TEXTO", etiqueta="Nombre y apellido")
        campo.refresh_from_db()
        self.assertEqual(campo.clave, clave)

    def test_una_version_activa_no_se_edita(self):
        guardar_campo(self.version, self.admin, tipo="TEXTO", etiqueta="Nombre")
        activar_version(self.formulario, self.version, self.admin)
        self.version.refresh_from_db()
        with self.assertRaises(ValidationError):
            guardar_campo(self.version, self.admin, tipo="TEXTO", etiqueta="Otro")

    def test_una_regla_solo_relaciona_campos_del_mismo_borrador(self):
        origen = guardar_campo(self.version, self.admin, tipo="BOOLEANO", etiqueta="¿Aplica?")
        otro = crear_nueva_version(Formulario.objects.create(nombre="Otro"), self.admin)
        ajeno = guardar_campo(otro, self.admin, tipo="TEXTO", etiqueta="Ajeno")
        with self.assertRaisesMessage(ValidationError, "campos de este formulario"):
            guardar_regla(self.version, self.admin, campo_origen=origen, operador="IGUAL_A", valor="true",
                          campo_objetivo=ajeno, efecto="MOSTRAR")

    def test_el_studio_usa_la_operacion(self):
        from django.urls import reverse

        servicio = Servicio.objects.create(
            nombre="Servicio edición", categoria=Categoria.objects.create(nombre="Cat edición"), formulario=self.formulario,
        )
        self.client.force_login(persona("demo_admin@pruebas.orbita"))
        with mock.patch("apps.catalogo.studio.guardar_campo", wraps=guardar_campo) as operacion:
            self.client.post(reverse("catalogo:studio_campo_crear", args=[servicio.pk]), {
                "campo-nuevo-tipo": "EQUIPO", "campo-nuevo-etiqueta": "Equipo", "campo-nuevo-orden": 1,
            })
        operacion.assert_called_once()
        self.assertFalse(Campo.objects.filter(version=self.version, tipo="EQUIPO").exists())


class _ParrillaEnAtencion(_EscenarioDemo):
    """UX-4 — la parrilla (contrato v2) preparada, publicada, con su ejecución de noviembre generada y en atención
    del Planificador. Ayudas para registrar ideas, decidir aprobaciones y producir la pieza."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        from django.test import override_settings

        super().setUpClass()
        cls._media = override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="orbita_ux4_"))
        cls._media.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media.disable()
        super().tearDownClass()

    def setUp(self):
        from apps.tickets import parrilla as parrilla_ops
        from apps.tickets.operaciones import iniciar_atencion_ticket

        self._preparar()
        self.recurso, _ = self._preparar_caso(contrato.PARRILLA)
        with _localdate(HOY_PROGRAMADO):
            fases.publicar(self.recurso, self.admin)
            fases.ejemplos(self.recurso, self.admin)
        self.ticket = self.recurso.servicio.tickets.get().ticket
        iniciar_atencion_ticket(self.ticket, self.planificador)
        self.ticket.refresh_from_db()
        self.parrilla_ops = parrilla_ops
        self.columnas = {c.clave: c for c in parrilla_ops.columnas_de(self.ticket.parrilla)}

    def _idea(self, copy="Idea", fecha="2026-11-10"):
        c = self.columnas
        return self.parrilla_ops.crear_elemento(self.ticket, self.planificador, {
            c[contrato.COPY].pk: copy, c[contrato.TIPO_PIEZA].pk: "post", c[contrato.CONTENIDO].pk: "Pieza",
            c[contrato.FECHA_PUBLICACION].pk: fecha,
        })

    def _enviar(self, **datos):
        """Envía la lluvia de ideas completa (todas las ideas registradas) como lo hace la pantalla."""
        from apps.workflows import recorridos

        return recorridos.enviar_conjunto(self.ticket, self.ticket.parrilla, self.planificador, **datos)

    def _ultima(self, elemento):
        from apps.workflows.models import InstanciaWorkflow

        return InstanciaWorkflow.objects.get(elemento=elemento).ejecuciones_etapa.order_by("-orden").first()

    def _decidir(self, elemento, decision, observacion=""):
        from apps.workflows.integracion import resolver_aprobacion_workflow
        from apps.workflows.models import EsquemaAprobacionWorkflow

        participacion = EsquemaAprobacionWorkflow.objects.get(instancia_etapa=self._ultima(elemento)).esquema.participaciones.get()
        resolver_aprobacion_workflow(participacion, self.aprobador, decision=decision, observacion=observacion)

    def _tarea(self, elemento):
        from apps.workflows.models import TareaWorkflow

        return TareaWorkflow.objects.get(instancia_etapa=self._ultima(elemento)).tarea

    def _producir(self, elemento):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.parrilla_ops.adjuntar_archivo_elemento(
            elemento, self.columnas[contrato.ARCHIVO_PIEZA], self.productor,
            SimpleUploadedFile("pieza.png", b"\x89PNG demo", content_type="image/png"),
        )



class ExperienciaGuiadaParrillaTests(_ParrillaEnAtencion, TestCase):
    """UX-4 — reglas de la parrilla sobre el dominio real: borradores y envío, requisitos por etapa, periodo,
    acceso a archivos por intervención, corrección de la gaceta e independencia por ítem."""

    def test_el_periodo_del_ticket_se_exige_al_guardar(self):
        with self.assertRaisesMessage(ValidationError, "Noviembre 2026"):
            self._idea(fecha="2026-12-01")
        with self.assertRaisesMessage(ValidationError, "dentro de"):
            self._idea(fecha="2026-10-31")
        self.assertIsNotNone(self._idea(fecha="2026-11-30"))  # el último día del periodo vale
        periodo = self.parrilla_ops.periodo_de(self.ticket)
        self.assertEqual((periodo.inicio, periodo.fin), (date(2026, 11, 1), date(2026, 11, 30)))

    def test_enviar_es_de_quien_opera_la_coleccion_y_una_sola_vez(self):
        from apps.workflows import recorridos
        from apps.workflows.models import InstanciaWorkflow

        idea = self._idea()
        with self.assertRaises(PermissionDenied):
            recorridos.enviar_conjunto(self.ticket, self.ticket.parrilla, self.productor)
        self.assertFalse(InstanciaWorkflow.objects.filter(elemento=idea).exists())
        self._enviar()
        with self.assertRaises(ValidationError):
            self._enviar()  # ya no hay lluvia de ideas que enviar

    def test_enviar_es_atomico(self):
        """Si falta un dato, NADA de lo enviado en la misma acción queda guardado."""

        idea = self.parrilla_ops.crear_elemento(self.ticket, self.planificador, {self.columnas[contrato.COPY].pk: "Copy"})
        with self.assertRaisesMessage(ValidationError, "falta"):
            self._enviar(
                cambios={idea: {self.columnas[contrato.CONTENIDO].pk: "Nuevo"}},
                nuevos=[{self.columnas[contrato.COPY].pk: "Otra"}],
            )
        self.assertFalse(idea.valores.filter(campo__clave=contrato.CONTENIDO).exists())
        self.assertEqual(self.ticket.parrilla.elementos.count(), 1)  # la nueva tampoco quedó

    def test_un_borrador_impide_que_el_recorrido_termine_hasta_salir_del_alcance(self):
        from apps.workflows import recorridos
        from apps.workflows.models import InstanciaWorkflow

        # Si alguien cierra el alcance sin enviar, los borradores siguen frenando el proceso hasta enviarlos (ya
        # uno a uno: el conjunto quedó cerrado) o excluirlos.
        enviada, borrador = self._idea("A"), self._idea("B")
        self.parrilla_ops.cerrar_alcance(self.ticket, self.planificador)
        principal = InstanciaWorkflow.objects.get(pk=self.ticket.instancia_workflow_id)
        self.assertEqual(principal.ejecuciones_etapa.order_by("-orden").first().bloque_operativo.nombre, contrato.RECORRIDO)
        recorridos.enviar_elemento(enviada, self.planificador)
        self.parrilla_ops.decidir_salida_de_alcance(enviada, self.planificador, tipo="EXCLUIDO", justificacion="No va.")
        self.parrilla_ops.decidir_salida_de_alcance(borrador, self.planificador, tipo="EXCLUIDO", justificacion="Sin espacio.")
        self.assertEqual(principal.ejecuciones_etapa.order_by("-orden").first().bloque_operativo.nombre, "Cerrar la parrilla")

    def test_cada_item_avanza_solo_y_la_pieza_exige_su_archivo(self):
        from apps.workflows.integracion import completar_tarea_workflow

        a, b = self._idea("A"), self._idea("B")
        self._enviar()
        self._decidir(a, "APROBADA")
        self.assertEqual(self._ultima(b).bloque_operativo.nombre, "Aprobación de la idea")  # b no se movió
        produccion = self._tarea(a)
        with self.assertRaisesMessage(ValidationError, "Archivo de la pieza"):
            completar_tarea_workflow(produccion, self.productor)
        self._producir(a)
        completar_tarea_workflow(produccion, self.productor)
        self.assertEqual(self._ultima(a).bloque_operativo.nombre, "Aprobación de la pieza")
        self._decidir(a, "DEVUELTA", "Cambia el color.")
        self.assertEqual(self._ultima(a).bloque_operativo.nombre, "Producción")
        self.assertEqual(self._tarea(a).usuario_responsable, self.productor)

    def test_el_aprobador_ve_solo_los_archivos_de_los_items_en_que_interviene(self):
        from django.urls import reverse

        from apps.tickets.edicion_por_tarea import puede_ver_elemento_por_intervencion
        from apps.tickets.models import Adjunto
        from apps.workflows.integracion import completar_tarea_workflow

        a, otra = self._idea("A"), self._idea("Otra")
        self._enviar()
        self._decidir(a, "APROBADA")
        self._producir(a)
        completar_tarea_workflow(self._tarea(a), self.productor)
        self.assertTrue(puede_ver_elemento_por_intervencion(self.aprobador, a))
        self.assertTrue(puede_ver_elemento_por_intervencion(self.productor, a))
        self.assertFalse(puede_ver_elemento_por_intervencion(self.productor, otra))  # no la produce
        self.assertFalse(puede_ver_elemento_por_intervencion(self.revisor, a))
        self.assertFalse(puede_ver_elemento_por_intervencion(self.sin_permisos, a))
        archivo = Adjunto.objects.get(elemento=a)
        url = reverse("tickets:descargar_adjunto", args=[archivo.pk])
        self.client.force_login(self.aprobador)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(reverse("tickets:detalle", args=[self.ticket.pk])).status_code, 403)
        self.client.force_login(self.sin_permisos)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_la_correccion_de_la_gaceta_llega_a_quien_produjo(self):
        from apps.gacetas.models import ParticipacionItemGaceta
        from apps.gacetas.operaciones import observar
        from apps.workflows.integracion import completar_tarea_workflow

        a = self._idea("A")
        self._enviar()
        self._decidir(a, "APROBADA")
        self._producir(a)
        completar_tarea_workflow(self._tarea(a), self.productor)
        self._decidir(a, "APROBADA")
        participacion = ParticipacionItemGaceta.objects.get(elemento=a)
        observar(participacion, self.revisor, "El logo está cortado.")
        ronda = participacion.rondas.order_by("-numero").first()
        self.assertEqual(ronda.tarea_correccion.usuario_responsable, self.productor)


class EspacioCuatroCasosTests(_EscenarioDemo, TestCase):
    """UX-4 — la misma composición sirve a los cuatro casos: proceso programado con colección (Parrilla), proceso
    manual con flujo (Webinar) y servicios de atención directa (Pieza gráfica, Presentaciones)."""

    def setUp(self):
        self._preparar()
        self.recursos = self._preparar_todos()
        with _localdate(HOY_PROGRAMADO):
            for recurso in self.recursos.values():
                fases.publicar(recurso, self.admin)
            fases.ejemplos(self.recursos[contrato.PARRILLA], self.admin)
        fases.ejemplos(self.recursos[contrato.WEBINAR], self.admin, solicitante=self.solicitante, area=self.area)
        for codigo in (contrato.PIEZA_GRAFICA, contrato.PRESENTACIONES):
            fases.ejemplos(self.recursos[codigo], self.admin, solicitante=self.solicitante)

    def test_los_cuatro_casos_abren_una_superficie_sin_pestanas(self):
        from django.urls import reverse

        for codigo, recurso in self.recursos.items():
            with self.subTest(caso=codigo):
                ticket = recurso.servicio.tickets.get().ticket
                quien = self.planificador if codigo == contrato.PARRILLA else self.solicitante
                self.client.force_login(quien)
                pagina = self.client.get(reverse("tickets:espacio", args=[ticket.pk]))
                self.assertEqual(pagina.status_code, 200)
                self.assertContains(pagina, "Tu misión")
                self.assertContains(pagina, "Recorrido")
                self.assertNotContains(pagina, 'aria-label="Secciones del ticket"')
                # Con flujo por fases, sus fases; sin él (atención directa), los estados del ticket.
                self.assertTrue(pagina.context.get("proceso") or pagina.context.get("ruta_estados"))


class EliminacionDemoTests(_EscenarioDemo, TestCase):
    """UX-4 — `--fase eliminar`: borra físicamente SOLO lo que creó la demo (con tickets en curso, tareas y
    aprobaciones) y permite prepararla de nuevo desde cero. Personas, equipos y permisos se conservan."""

    def setUp(self):
        self._preparar()
        self.recursos = self._preparar_todos()
        with _localdate(HOY_PROGRAMADO):
            for recurso in self.recursos.values():
                fases.publicar(recurso, self.admin)
            fases.ejemplos(self.recursos[contrato.PARRILLA], self.admin)
        for codigo in (contrato.PIEZA_GRAFICA, contrato.PRESENTACIONES):
            fases.ejemplos(self.recursos[codigo], self.admin, solicitante=self.solicitante)

    def _parrilla_en_curso(self):
        from apps.tickets import parrilla as parrilla_ops
        from apps.tickets.operaciones import iniciar_atencion_ticket
        from apps.workflows import recorridos

        ticket = self.recursos[contrato.PARRILLA].servicio.tickets.get().ticket
        iniciar_atencion_ticket(ticket, self.planificador)
        ticket.refresh_from_db()
        c = {campo.clave: campo for campo in parrilla_ops.columnas_de(ticket.parrilla)}
        parrilla_ops.crear_elemento(ticket, self.planificador, {
            c[contrato.COPY].pk: "Idea", c[contrato.TIPO_PIEZA].pk: "post", c[contrato.CONTENIDO].pk: "Pieza",
            c[contrato.FECHA_PUBLICACION].pk: "2026-11-10",
        })
        recorridos.enviar_conjunto(ticket, ticket.parrilla, self.planificador)

    def test_elimina_solo_la_demo_y_se_puede_volver_a_preparar(self):
        from apps.aprobaciones.models import Aprobacion
        from apps.catalogo.demo.eliminacion import eliminar
        from apps.tickets.models import Ticket

        self._parrilla_en_curso()
        usuarios, equipos = Usuario.objects.count(), Equipo.objects.count()
        servicios = [r.servicio.pk for r in self.recursos.values()]
        for recurso in self.recursos.values():
            lineas = eliminar(recurso, self.admin)
            self.assertIn("Ya se puede volver a preparar", lineas[-1])
        self.assertFalse(RecursoDemo.objects.exists())
        self.assertFalse(Servicio.objects.filter(pk__in=servicios).exists())
        self.assertFalse(Ticket.objects.filter(detalle_servicio__servicio_id__in=servicios).exists())
        self.assertFalse(Aprobacion.objects.exists())
        self.assertEqual((Usuario.objects.count(), Equipo.objects.count()), (usuarios, equipos))
        recurso, creado = self._preparar_caso(contrato.PARRILLA)
        self.assertTrue(creado)
        self.assertEqual(fases.discrepancias(recurso), [])

    def test_sin_confirmar_no_borra_nada(self):
        antes = self._conteos()
        with self.assertRaisesMessage(CommandError, "--confirmar-eliminar"):
            self._comando("eliminar")
        self.assertEqual(self._conteos(), antes)

    def test_la_simulacion_no_borra_nada(self):
        antes = self._conteos()
        salida = self._comando("eliminar", "--confirmar-eliminar", "--simular")
        self.assertIn("Simulación", salida)
        self.assertNotIn("✗", salida)
        self.assertEqual(self._conteos(), antes)


class TableroParrillaTests(_ParrillaEnAtencion, TestCase):
    """UX-4b — el tablero de la parrilla, persona por persona: la lluvia de ideas como tabla que se envía completa,
    la revisión del aprobador, la elaboración del productor y las devoluciones que vuelven a quien preparó la idea."""

    def _espacio(self, **parametros):
        from django.urls import reverse

        url = reverse("tickets:espacio", args=[self.ticket.pk])
        return url + ("?" + "&".join(f"{k}={v}" for k, v in parametros.items()) if parametros else "")

    def _post_tabla(self, datos):
        from django.urls import reverse

        return self.client.post(
            reverse("tickets:tabla_borradores", args=[self.ticket.pk, self.ticket.parrilla.clave]),
            {"next": self._espacio(), **datos}, follow=True,
        )

    def _fila(self, prefijo, copy, fecha="2026-11-12", contenido="Pieza", tipo="post"):
        c = self.columnas
        return {
            f"{prefijo}__campo_{c[contrato.COPY].pk}": copy, f"{prefijo}__campo_{c[contrato.TIPO_PIEZA].pk}": tipo,
            f"{prefijo}__campo_{c[contrato.CONTENIDO].pk}": contenido,
            f"{prefijo}__campo_{c[contrato.FECHA_PUBLICACION].pk}": fecha,
        }

    def test_la_planificadora_ve_la_lluvia_de_ideas_como_tabla_a_todo_lo_ancho(self):
        self.client.force_login(self.planificador)
        pagina = self.client.get(self._espacio())
        self.assertEqual(pagina.context["mision"]["tipo"], "lluvia")
        self.assertContains(pagina, "data-tabla-editable")
        self.assertContains(pagina, "Enviar lluvia de ideas a aprobación")
        self.assertNotContains(pagina, "esp-contexto")  # sin columna lateral
        self.assertNotContains(pagina, 'aria-label="Secciones del ticket"')
        etapas = pagina.context["ruta"]["etapas"]
        self.assertEqual(etapas[0]["nombre"], "Lluvia de ideas")
        self.assertTrue(etapas[0]["mia"])

    def test_guardar_borrador_no_envia_nada(self):
        from apps.workflows.models import InstanciaWorkflow

        self.client.force_login(self.planificador)
        respuesta = self._post_tabla({**self._fila("n0", "Lanzamiento"), **self._fila("n1", "Promo", contenido="")})
        self.assertTrue(any("Borrador guardado" in str(m) for m in respuesta.context["messages"]))
        self.assertEqual(self.ticket.parrilla.elementos.count(), 2)
        self.assertFalse(InstanciaWorkflow.objects.filter(elemento__parrilla=self.ticket.parrilla).exists())
        lluvia = respuesta.context["lluvia"]
        self.assertEqual((lluvia["listas"], lluvia["incompletas"]), (1, 1))

    def test_enviar_la_lluvia_completa_y_luego_ver_el_avance(self):
        self.client.force_login(self.planificador)
        incompleta = self._post_tabla({**self._fila("n0", "Lanzamiento"), **self._fila("n1", "Promo", contenido=""), "accion": "enviar"})
        self.assertTrue(any("falta" in str(m) for m in incompleta.context["messages"]))
        self.assertEqual(self.ticket.parrilla.elementos.count(), 0)  # nada a medias
        enviada = self._post_tabla({**self._fila("n0", "Lanzamiento"), **self._fila("n1", "Promo"), "accion": "enviar"})
        self.assertTrue(any("a aprobación" in str(m) for m in enviada.context["messages"]))
        self.ticket.parrilla.refresh_from_db()
        self.assertTrue(self.ticket.parrilla.alcance_cerrado)
        self.assertIsNone(enviada.context["mision"])  # su parte terminó: ahora solo sigue el avance
        self.assertIn("2 en Aprobación de la idea", enviada.context["espera"])
        etapas = {e["clave"]: e for e in enviada.context["ruta"]["etapas"]}
        self.assertEqual(etapas["Aprobación de la idea"]["cantidad"], 2)

    def test_el_aprobador_revisa_en_tabla_y_cada_idea_sigue_su_camino(self):
        from django.urls import reverse

        a, b = self._idea("A"), self._idea("B")
        self._enviar()
        self.client.force_login(self.aprobador)
        pagina = self.client.get(self._espacio())
        self.assertEqual(pagina.context["mision"]["tipo"], "revision")
        self.assertEqual(len(pagina.context["filas_mision"]), 2)
        self.assertContains(pagina, 'value="APROBADA"')
        aprobacion = {f["elemento"].pk: f["accion"]["aprobacion"] for f in pagina.context["filas_mision"]}
        self.client.post(reverse("aprobaciones:decidir", args=[aprobacion[a.pk].pk]), {"decision": "APROBADA", "next": self._espacio()})
        self.client.post(reverse("aprobaciones:decidir", args=[aprobacion[b.pk].pk]), {"decision": "DEVUELTA", "observacion": "Más corto.", "next": self._espacio()})
        # La aprobada pasa al productor; la devuelta vuelve a la planificadora con la observación.
        self.client.force_login(self.productor)
        produccion = self.client.get(self._espacio())
        self.assertEqual(produccion.context["mision"]["tipo"], "items")
        self.assertEqual([f["elemento"].pk for f in produccion.context["filas_mision"]], [a.pk])
        self.assertEqual(produccion.context["filas_mision"][0]["accion"]["etiqueta_avance"], "Enviar a aprobación")
        self.client.force_login(self.planificador)
        ajuste = self.client.get(self._espacio())
        self.assertEqual(ajuste.context["mision"]["titulo"], "Ajustar la idea")
        self.assertEqual(ajuste.context["filas_mision"][0]["observacion"], "Más corto.")
        self.assertNotContains(ajuste, "data-tabla-editable")  # la lluvia ya se envió: no se agregan ideas

    def test_el_productor_envia_la_pieza_con_su_archivo_en_un_paso(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        a = self._idea("A")
        self._enviar()
        self._decidir(a, "APROBADA")
        tarea = self._tarea(a)
        self.client.force_login(self.productor)
        campo = self.columnas[contrato.ARCHIVO_PIEZA]
        self.client.post(reverse("tickets:tarea_avanzar", args=[self.ticket.pk, tarea.pk]), {
            "next": self._espacio(), "elemento": a.pk,
            f"archivo_{campo.pk}": SimpleUploadedFile("pieza.png", b"\x89PNG", content_type="image/png"),
        })
        self.assertEqual(self._ultima(a).bloque_operativo.nombre, "Aprobación de la pieza")


class ImperandinaDemoTests(_EscenarioDemo, TestCase):
    """Demo «Parrilla mensual - Imperandina»: personas, equipos, categoría y calendario fijos en su contrato,
    gaceta con ventana compartida de 7 días y silencio positivo, y ticket de prueba inmediato con sus ideas en
    borrador. No toca la demo original."""

    HOY = date(2026, 10, 29)  # después del día de creación (28): el ticket de prueba se adelanta

    def setUp(self):
        self._preparar()
        crear = lambda alias, **campos: Usuario.objects.create_user(correo_de_prueba(alias), **campos)  # noqa: E731
        self.lau, self.amasso, self.emm = crear("lau"), crear("amasso"), crear("emm")
        self.rbuitrago, self.scortes = crear("rbuitrago"), crear("scortes")

    def _preparar_imperandina(self):
        # Las opciones de la línea de comandos no cuentan: el contrato fija sus personas y equipos.
        return self._preparar_caso(contrato.IMPERANDINA)

    def _publicar(self, recurso):
        with _localdate(self.HOY):
            return fases.publicar(recurso, self.admin)

    def test_no_entra_en_los_casos_por_defecto(self):
        self.assertNotIn("imperandina", contrato.ALIAS_POR_DEFECTO)
        self.assertNotIn(contrato.IMPERANDINA, contrato.CASOS)
        self.assertEqual(contrato.ALIAS["imperandina"], contrato.IMPERANDINA)

    def test_prepara_con_personas_equipos_y_categoria_del_contrato(self):
        recurso, creado = self._preparar_imperandina()
        self.assertTrue(creado)
        servicio = recurso.servicio
        self.assertEqual(servicio.nombre, "Parrilla mensual - Imperandina")  # sin el sufijo de la demo
        self.assertEqual(servicio.categoria.nombre, "Mercadeo")
        self.assertEqual(fases.discrepancias(recurso), [])
        bpo = Equipo.objects.get(nombre="Mercadeo BPO")
        revisor = Equipo.objects.get(nombre="Mercadeo Imperandina")
        miembros = lambda equipo: set(  # noqa: E731
            MiembroEquipo.objects.filter(equipo=equipo, activo=True).values_list("usuario__email", flat=True)
        )
        self.assertEqual(miembros(bpo), {f"{a}@pruebas.orbita" for a in ("lau", "amasso", "emm")})
        self.assertEqual(miembros(revisor), {f"{a}@pruebas.orbita" for a in ("rbuitrago", "scortes")})
        cfg = DefinicionGaceta.objects.get(servicio=servicio).configuracion
        self.assertEqual(cfg["apertura"], "POR_FLUJO")
        self.assertEqual(cfg["plazo_revision"], {"cantidad": 7, "unidad": "DIAS", "habiles": False})
        self.assertEqual(cfg["revisores"], [{"tipo": "EQUIPO", "equipo_id": revisor.pk}])
        self.assertNotIn("aprobadores", cfg)  # regla de oro: nadie aprueba en una gaceta
        self.assertEqual(cfg["coordinador"], {"tipo": "USUARIO", "usuario_id": self.lau.pk})
        self.assertEqual(cfg["responsable_correccion_origen"], "ULTIMA_ACTIVIDAD")
        self.assertIn(contrato.MEDIO_PUBLICACION, cfg["columnas_visibles"])
        self.assertEqual(errores_de_uso_por_item(cfg), [])
        bloques = {b.nombre: b for b in BloqueOperativo.objects.filter(version__servicio=servicio)}
        # «Publicación» es del equipo (cualquiera sube la evidencia, sin tomarla); responde emm. Ya no hay un paso
        # aparte de evidencia: la evidencia es la lista «Evidencia de publicación» del ítem.
        self.assertNotIn("Evidencia de publicación", bloques)
        publicar = bloques["Publicación"].configuracion
        self.assertEqual(publicar["tipo_actor"], "EQUIPO")
        self.assertEqual(publicar["equipo_id"], bpo.pk)
        self.assertEqual(publicar["responde_usuario_id"], self.emm.pk)
        self.assertTrue(publicar["requiere_evidencia"])
        columnas = {c.clave: c for c in servicio.definiciones_parrilla.get().formulario.versiones.get().campos.all()}
        lista = columnas[contrato.EVIDENCIA_PUBLICACION]
        self.assertEqual(lista.tipo, "CHECKLIST")
        self.assertEqual(list(lista.opciones.values_list("valor", flat=True)), [contrato.PUNTO_PUBLICADA])
        self.assertIn(contrato.MEDIO_PUBLICACION, bloques[contrato.RECORRIDO].configuracion["columnas_requeridas_ingreso"])
        self.assertNotIn(contrato.ARCHIVO_PIEZA, bloques[contrato.RECORRIDO].configuracion["columnas_requeridas_ingreso"])

    def test_no_duplica_equipos_ni_membresias_y_es_idempotente(self):
        bpo = Equipo.objects.create(nombre="Mercadeo BPO")
        MiembroEquipo.objects.create(equipo=bpo, usuario=self.lau)
        recurso, _ = self._preparar_imperandina()
        self.assertEqual(Equipo.objects.filter(nombre__iexact="Mercadeo BPO").count(), 1)
        self.assertEqual(MiembroEquipo.objects.filter(equipo=bpo, usuario=self.lau).count(), 1)
        antes = (Equipo.objects.count(), MiembroEquipo.objects.count(), Servicio.objects.count())
        otra, creado = self._preparar_imperandina()
        self.assertFalse(creado)
        self.assertEqual(otra, recurso)
        self.assertEqual((Equipo.objects.count(), MiembroEquipo.objects.count(), Servicio.objects.count()), antes)

    def test_falta_un_usuario_y_no_queda_nada_a_medias(self):
        self.scortes.delete()
        antes = self._conteos()
        with self.assertRaisesMessage(ValidationError, "scortes"):
            self._preparar_imperandina()
        self.assertEqual(self._conteos(), antes)

    def test_publica_con_el_calendario_del_contrato(self):
        recurso, _ = self._preparar_imperandina()
        self._publicar(recurso)
        programacion = ProgramacionProceso.objects.get(servicio=recurso.servicio)
        self.assertTrue(programacion.activa)
        self.assertEqual((programacion.dia_creacion, programacion.periodo), (28, "MES_SIGUIENTE"))
        self.assertEqual(programacion.responsable_inicial.usuario, self.lau)

    def test_ticket_de_prueba_inmediato_con_las_ideas_en_borrador(self):
        from apps.catalogo.demo.imperandina import ticket_de_prueba
        from apps.tickets import parrilla as parrilla_ops
        from apps.workflows.models import InstanciaWorkflow

        recurso, _ = self._preparar_imperandina()
        self._publicar(recurso)
        with _localdate(self.HOY):
            lineas = ticket_de_prueba(recurso, hoy=self.HOY)
        self.assertIn("adelantado", " ".join(lineas))
        ticket = recurso.servicio.tickets.get().ticket
        self.assertEqual(ticket.etiqueta, "Noviembre 2026")
        self.assertEqual(ticket.estado, "EN_ATENCION")
        self.assertEqual(ticket.usuario_responsable, self.lau)
        parrilla = parrilla_ops.parrilla_de(ticket)
        self.assertFalse(parrilla.alcance_cerrado)  # la lluvia de ideas sigue abierta
        elementos = list(parrilla.elementos.order_by("numero"))
        self.assertEqual(len(elementos), 5)
        # Ninguna idea se envió: no hay recorridos por ítem.
        self.assertFalse(InstanciaWorkflow.objects.filter(elemento__in=elementos).exists())
        valores = [parrilla_ops.valores_de(e) for e in elementos]
        self.assertEqual([v[contrato.FECHA_PUBLICACION] for v in valores],
                         [date(2026, 11, d) for d in (5, 10, 15, 20, 24)])
        self.assertEqual([v[contrato.MEDIO_PUBLICACION] for v in valores],
                         ["instagram", "facebook", "linkedin", "instagram", "facebook"])
        self.assertTrue(all(contrato.ARCHIVO_PIEZA not in v for v in valores))  # el archivo llega en Producción
        # Repetirlo no crea otro ticket ni agrega ideas.
        with _localdate(self.HOY):
            ticket_de_prueba(recurso, hoy=self.HOY)
        self.assertEqual(recurso.servicio.tickets.count(), 1)
        self.assertEqual(parrilla.elementos.count(), 5)

    def test_la_demo_original_no_cambia(self):
        self._preparar_imperandina()
        recurso, creado = self._preparar_caso(contrato.PARRILLA)
        self.assertTrue(creado)
        self.assertEqual(recurso.servicio.nombre, "Parrilla mensual de publicaciones · Demo")
        self.assertEqual(recurso.servicio.categoria.nombre, "Demostración Órbita")
        self.assertEqual(fases.discrepancias(recurso), [])
        cfg = DefinicionGaceta.objects.get(servicio=recurso.servicio).configuracion
        # Regla de oro (nadie aprueba en una gaceta): la gaceta por ítem acepta al cerrar su periodo.
        self.assertEqual(cfg["plazo_revision"], {"cantidad": 5, "unidad": "DIAS", "habiles": True})


class StudioConservaOpcionesTests(TestCase):
    """Las opciones que el Studio aún no edita no se pierden al guardar el bloque o la columna desde el Studio."""

    def test_editar_un_bloque_conserva_las_opciones_sin_editor(self):
        from apps.catalogo.studio import conservar_opciones_sin_editor

        anterior = {"coleccion": "pub", "ingreso": "AL_ENVIAR", "envio": "CONJUNTO", "paso_al_enviar": "aprobacion",
                    "etapa_borradores": "Lluvia de ideas", "columnas_requeridas_ingreso": ["copy"]}
        nueva = conservar_opciones_sin_editor("RECORRIDO_ITEMS", anterior, {"coleccion": "pub"})
        self.assertEqual(nueva, anterior)
        actividad = conservar_opciones_sin_editor(
            "ACTIVIDAD", {"columnas_requeridas": ["archivo"], "usuario_id": 1}, {"tipo_actor": "USUARIO", "usuario_id": 2}
        )
        self.assertEqual(actividad, {"tipo_actor": "USUARIO", "usuario_id": 2, "columnas_requeridas": ["archivo"]})

    def test_editar_una_columna_parte_de_su_configuracion_guardada(self):
        from apps.catalogo.forms import CampoForm

        campo = Campo(pk=1, tipo="FECHA", etiqueta="Fecha", clave="fecha", configuracion={"dentro_del_periodo": True})
        form = CampoForm(instance=campo)
        self.assertTrue(form.initial["dentro_del_periodo"])
        archivo = Campo(pk=2, tipo="ARCHIVO", etiqueta="Archivo", clave="archivo",
                        configuracion={"extensiones_permitidas": ["png", "mp4"], "tamano_maximo_mb": 200})
        form = CampoForm(instance=archivo)
        self.assertEqual(form.initial["extensiones_permitidas"], "png, mp4")
        self.assertEqual(form.initial["tamano_maximo_mb"], 200)
