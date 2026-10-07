"""Pruebas de `apps.tickets` — incrementos 2.1 (modelo base y borradores),
2.2 (radicación y respuestas), 2.3 (atención y asignación), 2.4
(comunicación, adjuntos operativos y solicitud de información) y 2.5
(resolución, cierre, cancelación y reapertura).

Único archivo de pruebas de esta app (misma decisión que `apps.core` y
`apps.catalogo`: sin paquete `tests/`). No se ejecutan como parte de la
implementación — se entregan junto con los comandos exactos para correrlas
vía Docker; las corre el usuario.

2.2 agrega: radicar, `radicado`/`radicado_en`, validación final
(obligatoriedad dinámica vía REQUERIR/NO_REQUERIR, campos ocultos nunca
obligatorios), inmutabilidad posterior a la radicación e invariantes de
versión. 2.3 agrega: `ServicioContextoAtencion`→`TicketContextoAtencion`
(snapshot al radicar), `estados.py` (State), autorización de atención
(`tickets.atender` GLOBAL/AREA/UNIDAD + relación operacional),
tomar/asignar/reasignar, concurrencia (`TransactionTestCase` + hilos reales
contra Postgres) y `HistorialTicket`. 2.4 agrega: `ComentarioTicket`
(cronológico, sin threading), `Adjunto` (TICKET/COMENTARIO/SOLICITUD/
RESPUESTA_SOLICITUD, `CheckConstraint` de coherencia), `SolicitudInformacion`
+ `RespuestaSolicitudInformacion` (una respuesta final, `OneToOneField`),
autorización de participación separada de consulta
(`puede_comentar_ticket`/`puede_solicitar_informacion`/
`puede_responder_solicitud` ≠ `puede_consultar_ticket`), concurrencia de
`responder_solicitud` y la corrección de `descargar_archivo_respuesta_view`
para RQF-006. 2.5 agrega: `ResolucionTicket` (entidad propia, `OneToOneField`,
adjuntos vía `Adjunto.TipoRelacion.RESOLUCION`), las 4 transiciones finales
en `estados.py` (RESOLVER/CERRAR/CANCELAR/REABRIR), autorización de
finalización (`puede_resolver_ticket`/`puede_cerrar_ticket`/
`puede_cancelar_ticket`/`puede_reabrir_ticket` — ninguna se satisface solo
con alcance de `tickets.atender`), bloqueo de RESOLVER por
`SolicitudInformacion` pendiente, concurrencia de las 4 operaciones, y la
integración doble `HistorialTicket` + `RegistroAuditoria` por transición.
"""

import shutil
import tempfile
import threading
import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import (
    BloqueOperativo,
    Campo,
    Categoria,
    ConfiguracionEjecucionVersion,
    Formulario,
    OpcionCampo,
    ReglaCondicional,
    Servicio,
    ServicioContextoAtencion,
    ServicioResponsable,
)
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.core.models import (
    Area,
    AsignacionRol,
    Equipo,
    MiembroEquipo,
    Permiso,
    RegistroAuditoria,
    RolFuncional,
    RolPermiso,
    UnidadNegocio,
)
from apps.tickets.autorizacion import (
    es_propietario_borrador,
    es_responsable_actual,
    puede_asignar,
    puede_cancelar_ticket,
    puede_cerrar_ticket,
    puede_comentar_ticket,
    puede_consultar_ticket,
    puede_iniciar_atencion,
    puede_reabrir_ticket,
    puede_reasignar,
    puede_resolver_ticket,
    puede_responder_solicitud,
    puede_solicitar_informacion,
    puede_tomar,
    puede_ver_en_cola,
    usuario_es_responsable_configurado,
)
from apps.tickets.estados import exigir_transicion, puede_ejecutar
from apps.tickets.entregables import adjuntar_archivo_entregable, registrar_resultado_entregable, retirar_archivo_entregable
from apps.tickets.entregas import aceptar_entrega, cerrar_entrega_vencida, entregar_ticket, observar_entrega
from apps.tickets.models import (
    Adjunto,
    ArchivoRespuestaCampo,
    ComentarioTicket,
    EntregableTicket,
    EntregaTicket,
    ProrrogaTicket,
    ResultadoEntregaTicket,
    HistorialTicket,
    ResolucionTicket,
    RespuestaCampo,
    RespuestaFormulario,
    RespuestaSolicitudInformacion,
    SolicitudInformacion,
    Ticket,
    TicketServicio,
)
from apps.tickets.tasks import cerrar_entregas_vencidas
from apps.tickets.operaciones import (
    adjuntar_archivo_ticket,
    asignar_ticket,
    cancelar_ticket,
    cerrar_ticket,
    comentar_ticket,
    crear_borrador,
    eliminar_borrador,
    guardar_respuestas_borrador,
    iniciar_atencion_ticket,
    radicar_ticket,
    reabrir_ticket,
    reasignar_ticket,
    resolver_ticket,
    responder_solicitud,
    solicitar_informacion,
    tomar_ticket,
)

Usuario = get_user_model()

CLAVE_PRUEBA = "Clave-Segura-123"


def _otorgar_tickets_atender(usuario, *, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL, area=None, unidad_negocio=None):
    """Helper de pruebas: crea un `RolFuncional` con el permiso
    `tickets.atender` (sembrado por `0004_seed_permiso_tickets_atender`) y
    lo asigna a `usuario` en el alcance indicado.

    `get_or_create` (no `.get()`): las pruebas de concurrencia usan
    `TransactionTestCase`, que hace `flush` de toda la base de datos al
    terminar (TRUNCATE, no rollback de transacción) — eso incluye la fila
    sembrada por la data migration. La siguiente clase `TransactionTestCase`
    que corra necesita poder re-crearla."""
    permiso, _ = Permiso.objects.get_or_create(
        codigo="tickets.atender", defaults={"nombre": "Atender tickets (tomar, asignar, reasignar)"}
    )
    rol = RolFuncional.objects.create(nombre=f"Rol atender {usuario.username}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(
        usuario=usuario, rol=rol, tipo_alcance=tipo_alcance, area=area, unidad_negocio=unidad_negocio
    )


def _otorgar_permiso(usuario, codigo, *, nombre_rol=None):
    permiso, _ = Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": codigo})
    rol = RolFuncional.objects.create(nombre=nombre_rol or f"Rol {codigo} {usuario.username}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(
        usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
    )


def _auditorias_de_ticket(ticket):
    """Helper de pruebas 2.5: `RegistroAuditoria` generados sobre `ticket`
    (auditoría transversal — distinta de `HistorialTicket`)."""
    return RegistroAuditoria.objects.filter(
        content_type=ContentType.objects.get_for_model(Ticket), object_id=ticket.pk
    )


class _MediaAisladaMixin:
    """Aísla `MEDIA_ROOT` en un directorio temporal propio de la clase —
    solo la usan las clases que efectivamente suben archivos
    (`ArchivoRespuestaCampoTests`, `EliminarBorradorTests`), para no
    escribir en el `media/` real ni compartir directorio entre clases."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_dir = tempfile.mkdtemp(prefix="orbita_tickets_tests_")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_dir)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_dir, ignore_errors=True)
        super().tearDownClass()


def _crear_servicio_con_formulario(actor, campos_spec, reglas_builder=None):
    """Helper de pruebas: Servicio PUBLICO_INTERNO activo con un Formulario
    de una sola versión ACTIVA, con los campos de `campos_spec` (lista de
    kwargs para `Campo.objects.create`, sin `version`; una clave extra
    `opciones` — lista de `(valor, etiqueta)` — crea sus `OpcionCampo`).

    `reglas_builder`, si se da, recibe `{etiqueta: campo}` y crea las
    `ReglaCondicional` necesarias — SE LLAMA ANTES de `activar_version`,
    porque `ReglaCondicional.save()` exige que su versión siga en BORRADOR
    (RN-013, ver `apps/catalogo/models/formularios.py`).

    Devuelve `(servicio, version, {etiqueta: campo})`.
    """
    categoria = Categoria.objects.create(nombre="Categoría de prueba")
    formulario = Formulario.objects.create(nombre="Formulario de prueba")
    servicio = Servicio.objects.create(
        nombre="Servicio de prueba",
        categoria=categoria,
        formulario=formulario,
        alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
    )
    version = crear_nueva_version(formulario, actor=actor)
    campos = {}
    for spec in campos_spec:
        spec = dict(spec)
        opciones = spec.pop("opciones", None)
        campo = Campo.objects.create(version=version, **spec)
        if opciones:
            for orden, (valor, etiqueta) in enumerate(opciones):
                OpcionCampo.objects.create(campo=campo, valor=valor, etiqueta=etiqueta, orden=orden)
        campos[spec["etiqueta"]] = campo
    if reglas_builder is not None:
        reglas_builder(campos)
    activar_version(formulario, version, actor=actor)
    version.refresh_from_db()
    return servicio, version, campos


class TicketModelosTests(TestCase):
    """Estructura básica: OneToOne obligatorios entre Ticket y sus
    especializaciones/respuestas."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="asolano", password=CLAVE_PRUEBA)
        self.servicio, self.version, _ = _crear_servicio_con_formulario(self.usuario, [])

    def test_ticketservicio_es_unico_por_ticket(self):
        ticket = Ticket.objects.create(solicitante=self.usuario)
        TicketServicio.objects.create(ticket=ticket, servicio=self.servicio, formulario_version=self.version)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TicketServicio.objects.create(
                    ticket=ticket, servicio=self.servicio, formulario_version=self.version
                )

    def test_respuestaformulario_es_unica_por_ticket(self):
        ticket = Ticket.objects.create(solicitante=self.usuario)
        RespuestaFormulario.objects.create(ticket=ticket, formulario_version=self.version)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RespuestaFormulario.objects.create(ticket=ticket, formulario_version=self.version)

    def test_ticket_por_defecto_es_borrador_servicio_manual(self):
        ticket = Ticket.objects.create(solicitante=self.usuario)
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertEqual(ticket.tipo, Ticket.Tipo.SERVICIO)
        self.assertEqual(ticket.origen, Ticket.Origen.MANUAL)


class CrearBorradorTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="bcortes", password=CLAVE_PRUEBA)
        self.servicio, self.version, _ = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Motivo"}]
        )

    def test_crea_ticket_servicio_y_congela_version_activa(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.assertEqual(ticket.solicitante_id, self.usuario.id)
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertEqual(ticket.detalle_servicio.servicio_id, self.servicio.pk)
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, self.version.pk)
        self.assertEqual(ticket.respuesta_formulario.formulario_version_id, self.version.pk)

    def test_rechaza_servicio_inactivo(self):
        self.servicio.activo = False
        self.servicio.save()
        with self.assertRaises(PermissionDenied):
            crear_borrador(self.usuario, self.servicio)

    def test_rechaza_servicio_restringido_sin_concesion(self):
        self.servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.RESTRINGIDO
        self.servicio.save()
        with self.assertRaises(PermissionDenied):
            crear_borrador(self.usuario, self.servicio)

    def test_rechaza_servicio_sin_formulario(self):
        categoria = Categoria.objects.create(nombre="Sin formulario")
        servicio = Servicio.objects.create(
            nombre="Servicio sin formulario",
            categoria=categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )
        with self.assertRaises(ValidationError):
            crear_borrador(self.usuario, servicio)

    def test_rechaza_formulario_sin_version_activa(self):
        categoria = Categoria.objects.create(nombre="Formulario sin activar")
        formulario = Formulario.objects.create(nombre="Nunca activado")
        servicio = Servicio.objects.create(
            nombre="Servicio con formulario sin activar",
            categoria=categoria,
            formulario=formulario,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )
        with self.assertRaises(ValidationError):
            crear_borrador(self.usuario, servicio)


class CongelacionVersionTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="ccastro", password=CLAVE_PRUEBA)
        self.servicio, self.version_1, _ = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Motivo"}]
        )

    def test_activar_nueva_version_no_altera_borrador_existente(self):
        ticket = crear_borrador(self.usuario, self.servicio)

        formulario = self.servicio.formulario
        version_2 = crear_nueva_version(formulario, actor=self.usuario)
        activar_version(formulario, version_2, actor=self.usuario)

        formulario.refresh_from_db()
        self.assertEqual(formulario.version_activa_id, version_2.pk)

        ticket.detalle_servicio.refresh_from_db()
        ticket.respuesta_formulario.refresh_from_db()
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, self.version_1.pk)
        self.assertEqual(ticket.respuesta_formulario.formulario_version_id, self.version_1.pk)


class PersistenciaRespuestasTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="dpaez", password=CLAVE_PRUEBA)
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-TCK")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-TCK")
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"},
                {"tipo": Campo.TipoCampo.NUMERO, "etiqueta": "Numero"},
                {"tipo": Campo.TipoCampo.FECHA, "etiqueta": "Fecha"},
                {"tipo": Campo.TipoCampo.FECHA_HORA, "etiqueta": "FechaHora"},
                {"tipo": Campo.TipoCampo.BOOLEANO, "etiqueta": "Booleano"},
                {
                    "tipo": Campo.TipoCampo.LISTA,
                    "etiqueta": "Lista",
                    "opciones": [("BAJA", "Baja"), ("ALTA", "Alta")],
                },
                {
                    "tipo": Campo.TipoCampo.MULTILISTA,
                    "etiqueta": "Multilista",
                    "opciones": [("URGENTE", "Urgente"), ("INTERNO", "Interno")],
                },
                {"tipo": Campo.TipoCampo.USUARIO, "etiqueta": "Usuario"},
                {"tipo": Campo.TipoCampo.AREA, "etiqueta": "Area"},
                {"tipo": Campo.TipoCampo.UNIDAD, "etiqueta": "Unidad"},
            ],
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)

    def _respuesta(self, etiqueta):
        return RespuestaCampo.objects.get(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos[etiqueta]
        )

    def test_guarda_texto_en_columna_texto(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Texto"].id: "Hola mundo"})
        respuesta = self._respuesta("Texto")
        self.assertEqual(respuesta.valor_texto, "Hola mundo")
        self.assertIsNone(respuesta.valor_numero)

    def test_guarda_numero_en_columna_numero(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Numero"].id: "42"})
        respuesta = self._respuesta("Numero")
        self.assertEqual(respuesta.valor_numero, 42)

    def test_guarda_fecha_en_columna_fecha(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Fecha"].id: "2026-03-05"})
        respuesta = self._respuesta("Fecha")
        self.assertEqual(str(respuesta.valor_fecha), "2026-03-05")

    def test_guarda_fecha_hora_en_columna_fecha_hora(self):
        guardar_respuestas_borrador(
            self.ticket, self.usuario, {self.campos["FechaHora"].id: "2026-03-05T10:30:00"}
        )
        respuesta = self._respuesta("FechaHora")
        # EstrategiaTemporal.normalizar (1.2, sin modificar) produce un
        # datetime naive; con USE_TZ=True, Django lo interpreta en la
        # zona horaria activa al guardarlo y lo devuelve aware (en UTC) al
        # leerlo — la hora que escribió el usuario se conserva en la zona
        # activa (TIME_ZONE), sea cual sea; no se asume UTC.
        hora_local = timezone.localtime(respuesta.valor_fecha_hora)
        self.assertEqual(hora_local.replace(tzinfo=None).isoformat(), "2026-03-05T10:30:00")

    def test_guarda_booleano_en_columna_booleano(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Booleano"].id: True})
        respuesta = self._respuesta("Booleano")
        self.assertTrue(respuesta.valor_booleano)

    def test_lista_usa_columna_texto(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Lista"].id: "ALTA"})
        respuesta = self._respuesta("Lista")
        self.assertEqual(respuesta.valor_texto, "ALTA")

    def test_multilista_usa_columna_json(self):
        guardar_respuestas_borrador(
            self.ticket, self.usuario, {self.campos["Multilista"].id: ["URGENTE", "INTERNO"]}
        )
        respuesta = self._respuesta("Multilista")
        self.assertEqual(sorted(respuesta.valor_json), ["INTERNO", "URGENTE"])

    def test_usuario_se_guarda_como_fk_no_como_id_crudo_en_numero(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Usuario"].id: self.usuario.pk})
        respuesta = self._respuesta("Usuario")
        self.assertEqual(respuesta.valor_usuario_id, self.usuario.pk)
        self.assertIsNone(respuesta.valor_numero)

    def test_area_se_guarda_como_fk(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Area"].id: self.area.pk})
        respuesta = self._respuesta("Area")
        self.assertEqual(respuesta.valor_area_id, self.area.pk)

    def test_unidad_se_guarda_como_fk(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Unidad"].id: self.unidad.pk})
        respuesta = self._respuesta("Unidad")
        self.assertEqual(respuesta.valor_unidad_id, self.unidad.pk)

    def test_valor_vacio_borra_respuesta_existente(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Texto"].id: "Algo"})
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Texto"].id: ""})
        self.assertFalse(
            RespuestaCampo.objects.filter(
                respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Texto"]
            ).exists()
        )

    def test_no_modifica_campos_no_incluidos_en_la_llamada(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Texto"].id: "Primero"})
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Numero"].id: "5"})
        respuesta_texto = self._respuesta("Texto")
        self.assertEqual(respuesta_texto.valor_texto, "Primero")

    def test_rechaza_valor_invalido_segun_estrategia(self):
        # NUMERO no admite decimales por defecto (Campo.configuracion no
        # habilita `permite_decimales`) — EstrategiaNumerica.validar_valor
        # rechaza "42.5" con ValidationError.
        with self.assertRaises(ValidationError):
            guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Numero"].id: "42.5"})


class ReglasCondicionalesEnBorradorTests(TestCase):
    """RQF-053 (parcial, MOSTRAR/OCULTAR únicamente — REQUERIR/NO_REQUERIR
    es 2.2). Reutiliza `EspecificacionRegla` (1.2) sin cambios."""

    def setUp(self):
        def _crear_regla(campos):
            ReglaCondicional.objects.create(
                campo_origen=campos["EsUrgente"],
                operador=ReglaCondicional.Operador.IGUAL_A,
                valor="true",
                campo_objetivo=campos["Justificacion"],
                efecto=ReglaCondicional.Efecto.MOSTRAR,
            )

        self.usuario = Usuario.objects.create_user(username="egomez", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.BOOLEANO, "etiqueta": "EsUrgente"},
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Justificacion"},
            ],
            reglas_builder=_crear_regla,
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)

    def _respuesta_justificacion(self):
        return RespuestaCampo.objects.filter(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Justificacion"]
        ).first()

    def test_campo_oculto_por_defecto_descarta_valor_enviado(self):
        # Sin EsUrgente=True todavía, MOSTRAR no se satisface -> Justificacion está oculta.
        guardar_respuestas_borrador(
            self.ticket, self.usuario, {self.campos["Justificacion"].id: "No debería guardarse"}
        )
        self.assertIsNone(self._respuesta_justificacion())

    def test_campo_se_muestra_y_guarda_cuando_la_condicion_se_satisface(self):
        guardar_respuestas_borrador(
            self.ticket,
            self.usuario,
            {self.campos["EsUrgente"].id: True, self.campos["Justificacion"].id: "Producción caída"},
        )
        respuesta = self._respuesta_justificacion()
        self.assertIsNotNone(respuesta)
        self.assertEqual(respuesta.valor_texto, "Producción caída")

    def test_campo_que_pasa_a_oculto_borra_respuesta_previa(self):
        guardar_respuestas_borrador(
            self.ticket,
            self.usuario,
            {self.campos["EsUrgente"].id: True, self.campos["Justificacion"].id: "Motivo"},
        )
        self.assertIsNotNone(self._respuesta_justificacion())

        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["EsUrgente"].id: False})
        self.assertIsNone(self._respuesta_justificacion())


class IntegridadRespuestaCampoTests(TestCase):
    """Corrección del usuario: garantías por las vías ordinarias de
    dominio, no solo por convención en `operaciones.py`."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="fnino", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)

        otro_formulario = Formulario.objects.create(nombre="Otro formulario")
        self.otra_version = crear_nueva_version(otro_formulario, actor=self.usuario)
        self.campo_ajeno = Campo.objects.create(
            version=self.otra_version, tipo=Campo.TipoCampo.TEXTO, etiqueta="Ajeno"
        )

    def test_rechaza_campo_de_otra_formularioversion(self):
        respuesta = RespuestaCampo(
            respuesta_formulario=self.ticket.respuesta_formulario,
            campo=self.campo_ajeno,
            valor_texto="x",
        )
        with self.assertRaises(ValidationError):
            respuesta.save()

    def test_rechaza_valor_en_columna_incompatible(self):
        respuesta = RespuestaCampo(
            respuesta_formulario=self.ticket.respuesta_formulario,
            campo=self.campos["Texto"],
            valor_numero=5,  # TEXTO debe usar valor_texto, no valor_numero
        )
        with self.assertRaises(ValidationError):
            respuesta.save()

    def test_guarda_correctamente_cuando_columna_coincide_con_el_tipo(self):
        respuesta = RespuestaCampo(
            respuesta_formulario=self.ticket.respuesta_formulario,
            campo=self.campos["Texto"],
            valor_texto="ok",
        )
        respuesta.save()
        self.assertIsNotNone(respuesta.pk)


class ArchivoRespuestaCampoTests(_MediaAisladaMixin, TestCase):
    """Corrección del usuario: soporte de persistencia para Campo tipo
    ARCHIVO, distinto de los adjuntos de comunicación (2.4)."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="gluna", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="hrojas", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {
                    "tipo": Campo.TipoCampo.ARCHIVO,
                    "etiqueta": "Soporte",
                    "configuracion": {"extensiones_permitidas": ["pdf", "txt"], "tamano_maximo_mb": 1},
                }
            ],
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)

    def _respuesta_soporte(self):
        return RespuestaCampo.objects.get(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Soporte"]
        )

    def test_guarda_archivo_de_respuesta(self):
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: archivo})

        respuesta = self._respuesta_soporte()
        self.assertEqual(respuesta.archivo.nombre_original, "evidencia.txt")
        self.assertEqual(respuesta.archivo.tamano_bytes, len(b"contenido"))
        self.assertEqual(respuesta.archivo.subido_por_id, self.usuario.id)
        # Ninguna columna escalar debe quedar poblada para ARCHIVO.
        self.assertEqual(respuesta.valor_texto, "")
        self.assertIsNone(respuesta.valor_numero)

    def test_rechaza_extension_no_permitida(self):
        archivo = SimpleUploadedFile("evidencia.exe", b"contenido", content_type="application/octet-stream")
        with self.assertRaises(ValidationError):
            guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: archivo})

    def test_rechaza_archivo_que_excede_tamano_maximo(self):
        archivo = SimpleUploadedFile("grande.pdf", b"x" * (2 * 1024 * 1024), content_type="application/pdf")
        with self.assertRaises(ValidationError):
            guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: archivo})

    def test_reemplaza_archivo_existente(self):
        primero = SimpleUploadedFile("v1.txt", b"contenido 1", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: primero})
        ruta_original = self._respuesta_soporte().archivo.archivo.name
        self.assertTrue(self._respuesta_soporte().archivo.archivo.storage.exists(ruta_original))

        segundo = SimpleUploadedFile("v2.txt", b"contenido 2", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: segundo})

        respuesta = self._respuesta_soporte()
        self.assertEqual(respuesta.archivo.nombre_original, "v2.txt")
        self.assertFalse(respuesta.archivo.archivo.storage.exists(ruta_original))

    def test_valor_vacio_elimina_archivo_de_borrador(self):
        archivo = SimpleUploadedFile("v1.txt", b"contenido", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: archivo})
        ruta = self._respuesta_soporte().archivo.archivo.name

        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: None})

        self.assertFalse(
            RespuestaCampo.objects.filter(
                respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Soporte"]
            ).exists()
        )
        self.assertFalse(ArchivoRespuestaCampo.objects.filter(archivo=ruta).exists())

    def test_usuario_ajeno_no_puede_descargar_archivo(self):
        archivo = SimpleUploadedFile("privado.txt", b"contenido", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: archivo})
        archivo_id = self._respuesta_soporte().archivo.pk

        self.client.login(username="hrojas", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_archivo", args=[archivo_id]))
        self.assertEqual(respuesta.status_code, 403)

    def test_propietario_si_puede_descargar_archivo(self):
        archivo = SimpleUploadedFile("propio.txt", b"contenido", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Soporte"].id: archivo})
        archivo_id = self._respuesta_soporte().archivo.pk

        self.client.login(username="gluna", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_archivo", args=[archivo_id]))
        self.assertEqual(respuesta.status_code, 200)


class EliminarBorradorTests(_MediaAisladaMixin, TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="ivargas2", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="jmora", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"},
                {
                    "tipo": Campo.TipoCampo.ARCHIVO,
                    "etiqueta": "Soporte",
                    "configuracion": {"extensiones_permitidas": ["txt"], "tamano_maximo_mb": 1},
                },
            ],
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        guardar_respuestas_borrador(
            self.ticket,
            self.usuario,
            {self.campos["Texto"].id: "Algo", self.campos["Soporte"].id: archivo},
        )

    def test_elimina_ticket_respuestas_y_archivo_fisico_en_cascada(self):
        archivo_respuesta = ArchivoRespuestaCampo.objects.get(
            respuesta_campo__respuesta_formulario__ticket=self.ticket
        )
        ruta_archivo = archivo_respuesta.archivo.name
        storage = archivo_respuesta.archivo.storage
        self.assertTrue(storage.exists(ruta_archivo))

        ticket_id = self.ticket.pk
        eliminar_borrador(self.ticket, self.usuario)

        self.assertFalse(Ticket.objects.filter(pk=ticket_id).exists())
        self.assertFalse(TicketServicio.objects.filter(ticket_id=ticket_id).exists())
        self.assertFalse(RespuestaFormulario.objects.filter(ticket_id=ticket_id).exists())
        self.assertFalse(RespuestaCampo.objects.filter(respuesta_formulario__ticket_id=ticket_id).exists())
        self.assertFalse(ArchivoRespuestaCampo.objects.filter(pk=archivo_respuesta.pk).exists())
        self.assertFalse(storage.exists(ruta_archivo))

    def test_solo_solicitante_puede_eliminar(self):
        with self.assertRaises(PermissionDenied):
            eliminar_borrador(self.ticket, self.otro_usuario)
        self.assertTrue(Ticket.objects.filter(pk=self.ticket.pk).exists())

    def test_no_permite_eliminar_ticket_que_ya_no_es_borrador(self):
        self.ticket.estado = Ticket.Estado.RADICADO
        self.ticket.save()
        with self.assertRaises(ValidationError):
            eliminar_borrador(self.ticket, self.usuario)


class AutorizacionBorradorTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="kalvarez", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="lmontes", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)

    def test_es_propietario_borrador(self):
        self.assertTrue(es_propietario_borrador(self.usuario, self.ticket))
        self.assertFalse(es_propietario_borrador(self.otro_usuario, self.ticket))

    def test_usuario_ajeno_no_puede_guardar_respuestas(self):
        with self.assertRaises(PermissionDenied):
            guardar_respuestas_borrador(self.ticket, self.otro_usuario, {self.campos["Texto"].id: "x"})

    def test_un_cambio_de_visibilidad_del_servicio_no_bloquea_el_borrador_propio(self):
        # Corrección del usuario: visibilidad solo se valida al CREAR.
        self.servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.RESTRINGIDO
        self.servicio.save()
        # No debe lanzar PermissionDenied aunque el servicio ya no sea visible.
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Texto"].id: "Sigo pudiendo"})
        respuesta = RespuestaCampo.objects.get(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Texto"]
        )
        self.assertEqual(respuesta.valor_texto, "Sigo pudiendo")


class VistasBorradorTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="mrivas", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="npardo", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )

    def test_iniciar_borrador_crea_ticket_y_redirige(self):
        self.client.login(username="mrivas", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tickets:iniciar", args=[self.servicio.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(Ticket.objects.filter(solicitante=self.usuario).exists())

    def test_guardar_respuestas_via_post(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="mrivas", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:borrador", args=[ticket.pk]), {f"campo_{self.campos['Texto'].id}": "Desde la vista"}
        )
        self.assertEqual(respuesta.status_code, 302)
        rc = RespuestaCampo.objects.get(respuesta_formulario=ticket.respuesta_formulario, campo=self.campos["Texto"])
        self.assertEqual(rc.valor_texto, "Desde la vista")

    def test_usuario_ajeno_recibe_403_en_borrador_de_otro(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="npardo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:borrador", args=[ticket.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_mis_tickets_lista_solo_los_propios(self):
        crear_borrador(self.usuario, self.servicio)
        self.client.login(username="npardo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(len(respuesta.context["tickets"]), 0)


def _completar_texto(ticket, usuario, campos, etiqueta="Texto", valor="Motivo válido"):
    guardar_respuestas_borrador(ticket, usuario, {campos[etiqueta].id: valor})


class RadicarTicketTests(TestCase):
    """CU-014/RQF-053/054, RN-014/RN-016 — incremento 2.2."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="oquintero", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="pbarrios", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto", "obligatorio": True},
                {"tipo": Campo.TipoCampo.NUMERO, "etiqueta": "Numero"},
            ],
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)

    def test_radica_correctamente(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        radicar_ticket(self.ticket, self.usuario)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsInstance(self.ticket.radicado, uuid.UUID)
        self.assertIsNotNone(self.ticket.radicado_en)

    def test_actor_ajeno_no_puede_radicar(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        with self.assertRaises(PermissionDenied):
            radicar_ticket(self.ticket, self.otro_usuario)

    def test_no_se_puede_radicar_dos_veces(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        radicar_ticket(self.ticket, self.usuario)
        with self.assertRaises(ValidationError):
            radicar_ticket(self.ticket, self.usuario)

    def test_rechaza_si_servicio_esta_inactivo(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        self.servicio.activo = False
        self.servicio.save()
        with self.assertRaises(ValidationError):
            radicar_ticket(self.ticket, self.usuario)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.BORRADOR)

    def test_cambio_de_visibilidad_no_bloquea_la_radicacion(self):
        # Decisión de negocio del proyecto (no texto literal del Excel):
        # un cambio de visibilidad posterior a crear el borrador no
        # bloquea radicar — mismo criterio que 2.1 aplica al guardar.
        _completar_texto(self.ticket, self.usuario, self.campos)
        self.servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.RESTRINGIDO
        self.servicio.save()
        radicar_ticket(self.ticket, self.usuario)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RADICADO)

    def test_falla_si_falta_campo_obligatorio(self):
        with self.assertRaises(ValidationError):
            radicar_ticket(self.ticket, self.usuario)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.BORRADOR)
        self.assertIsNone(self.ticket.radicado)

    def test_rollback_completo_si_falla_la_validacion(self):
        with self.assertRaises(ValidationError):
            radicar_ticket(self.ticket, self.usuario)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.radicado)
        self.assertIsNone(self.ticket.radicado_en)

    def test_radicado_es_unico_entre_dos_tickets(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        radicar_ticket(self.ticket, self.usuario)

        otro_ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(otro_ticket, self.usuario, self.campos)
        radicar_ticket(otro_ticket, self.usuario)

        self.assertNotEqual(self.ticket.radicado, otro_ticket.radicado)

    def test_radicado_en_usa_la_hora_del_sistema(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        antes = timezone.now()
        radicar_ticket(self.ticket, self.usuario)
        despues = timezone.now()
        self.ticket.refresh_from_db()
        self.assertGreaterEqual(self.ticket.radicado_en, antes)
        self.assertLessEqual(self.ticket.radicado_en, despues)

    def test_campo_opcional_puede_quedar_vacio(self):
        _completar_texto(self.ticket, self.usuario, self.campos)
        radicar_ticket(self.ticket, self.usuario)  # Numero nunca se llenó
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RADICADO)


class ObligatoriedadDinamicaEnRadicarTests(TestCase):
    """RQF-043/044 — REQUERIR/NO_REQUERIR modifican dinámicamente la
    obligatoriedad base de `Campo.obligatorio` (decisión aprobada, sin
    precedencia: MOSTRAR/OCULTAR y REQUERIR/NO_REQUERIR no pueden mezclarse
    con su opuesto sobre el mismo objetivo — ver `apps/catalogo/tests.py`)."""

    def setUp(self):
        def _reglas(campos):
            ReglaCondicional.objects.create(
                campo_origen=campos["Tipo"],
                operador=ReglaCondicional.Operador.IGUAL_A,
                valor="URGENTE",
                campo_objetivo=campos["Justificacion"],
                efecto=ReglaCondicional.Efecto.REQUERIR,
            )
            ReglaCondicional.objects.create(
                campo_origen=campos["Tipo"],
                operador=ReglaCondicional.Operador.IGUAL_A,
                valor="RUTINA",
                campo_objetivo=campos["Aprobador"],
                efecto=ReglaCondicional.Efecto.NO_REQUERIR,
            )

        self.usuario = Usuario.objects.create_user(username="qsierra", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {
                    "tipo": Campo.TipoCampo.LISTA,
                    "etiqueta": "Tipo",
                    "opciones": [("URGENTE", "Urgente"), ("RUTINA", "Rutina"), ("NORMAL", "Normal")],
                },
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Justificacion", "obligatorio": False},
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Aprobador", "obligatorio": True},
            ],
            reglas_builder=_reglas,
        )

    def _ticket_con_tipo(self, valor_tipo):
        ticket = crear_borrador(self.usuario, self.servicio)
        guardar_respuestas_borrador(
            ticket, self.usuario, {self.campos["Tipo"].id: valor_tipo, self.campos["Aprobador"].id: "Ana"}
        )
        return ticket

    def test_requerir_satisfecha_exige_el_campo_normalmente_opcional(self):
        ticket = self._ticket_con_tipo("URGENTE")  # Justificacion pasa a requerida, sin valor
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario)

    def test_requerir_no_satisfecha_conserva_el_estado_base_opcional(self):
        ticket = self._ticket_con_tipo("RUTINA")  # REQUERIR de Justificacion no se satisface
        radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_no_requerir_satisfecha_libera_el_campo_normalmente_obligatorio(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        guardar_respuestas_borrador(ticket, self.usuario, {self.campos["Tipo"].id: "RUTINA"})  # Aprobador sin valor
        radicar_ticket(ticket, self.usuario)  # NO_REQUERIR se satisface -> Aprobador ya no es obligatorio
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_no_requerir_no_satisfecha_conserva_obligatorio_por_defecto(self):
        # "NORMAL" no satisface ni REQUERIR (Tipo==URGENTE) ni NO_REQUERIR
        # (Tipo==RUTINA) — aísla el caso: solo Aprobador queda sin valor.
        ticket = crear_borrador(self.usuario, self.servicio)
        guardar_respuestas_borrador(ticket, self.usuario, {self.campos["Tipo"].id: "NORMAL"})  # Aprobador sin valor
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario)

    def test_campo_oculto_nunca_es_obligatorio_aunque_tenga_valor_residual(self):
        # Simula un valor residual accidental en un campo que en ese
        # momento evalúa como oculto — no debe considerarse "obligatorio
        # incumplido" (instrucción explícita del usuario para 2.2).
        def _reglas_visibilidad(campos):
            ReglaCondicional.objects.create(
                campo_origen=campos["Disparador"],
                operador=ReglaCondicional.Operador.IGUAL_A,
                valor="true",
                campo_objetivo=campos["Oculto"],
                efecto=ReglaCondicional.Efecto.MOSTRAR,
            )

        servicio, version, campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.BOOLEANO, "etiqueta": "Disparador"},
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Oculto", "obligatorio": True},
            ],
            reglas_builder=_reglas_visibilidad,
        )
        ticket = crear_borrador(self.usuario, servicio)
        respuesta_formulario = ticket.respuesta_formulario
        # Se inserta directamente por ORM (bypass de guardar_respuestas_borrador,
        # que purgaría el valor por estar oculto) para simular el residuo.
        RespuestaCampo.objects.create(
            respuesta_formulario=respuesta_formulario, campo=campos["Oculto"], valor_texto="residual"
        )
        radicar_ticket(ticket, self.usuario)  # Disparador nunca fue True -> Oculto sigue oculto
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)


class InmutabilidadPosteriorARadicarTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="rzapata", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(self.ticket, self.usuario, self.campos)
        radicar_ticket(self.ticket, self.usuario)
        self.ticket.refresh_from_db()

    def test_no_se_pueden_guardar_respuestas_despues_de_radicar(self):
        with self.assertRaises(ValidationError):
            guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Texto"].id: "Otro valor"})

    def test_no_se_puede_eliminar_despues_de_radicar(self):
        with self.assertRaises(ValidationError):
            eliminar_borrador(self.ticket, self.usuario)

    def test_no_se_puede_guardar_respuestacampo_directamente(self):
        respuesta = RespuestaCampo.objects.get(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Texto"]
        )
        respuesta.valor_texto = "Modificado por ORM directo"
        with self.assertRaises(ValidationError):
            respuesta.save()

    def test_no_se_puede_modificar_formulario_version_de_ticketservicio(self):
        otra_version = crear_nueva_version(Formulario.objects.create(nombre="Otro"), actor=self.usuario)
        self.ticket.detalle_servicio.formulario_version = otra_version
        with self.assertRaises(ValidationError):
            self.ticket.detalle_servicio.save()

    def test_no_se_puede_modificar_formulario_version_de_respuestaformulario(self):
        otra_version = crear_nueva_version(Formulario.objects.create(nombre="Otro"), actor=self.usuario)
        self.ticket.respuesta_formulario.formulario_version = otra_version
        with self.assertRaises(ValidationError):
            self.ticket.respuesta_formulario.save()


class InvariantesDeVersionEnRadicacionTests(TestCase):
    def test_invariantes_de_version_se_preservan_tras_radicar(self):
        usuario = Usuario.objects.create_user(username="sflores", password=CLAVE_PRUEBA)
        servicio, version, campos = _crear_servicio_con_formulario(
            usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        ticket = crear_borrador(usuario, servicio)
        _completar_texto(ticket, usuario, campos)
        radicar_ticket(ticket, usuario)

        ticket.refresh_from_db()
        detalle = ticket.detalle_servicio
        respuesta_formulario = ticket.respuesta_formulario
        self.assertEqual(detalle.formulario_version_id, respuesta_formulario.formulario_version_id)
        for respuesta_campo in respuesta_formulario.respuestas_campo.select_related("campo"):
            self.assertEqual(respuesta_campo.campo.version_id, respuesta_formulario.formulario_version_id)


class RadicarViewTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="tnunez", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="uvelez", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto", "obligatorio": True},
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Opcional"},
            ],
        )

    def test_radicar_view_exitoso_redirige_al_detalle(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="tnunez", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:radicar", args=[ticket.pk]), {f"campo_{self.campos['Texto'].id}": "Listo"}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_radicar_view_conserva_lo_guardado_si_falla_la_validacion(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="tnunez", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:radicar", args=[ticket.pk]),
            {f"campo_{self.campos['Opcional'].id}": "No se pierde"},
        )
        self.assertEqual(respuesta.status_code, 200)  # re-render con errores, no redirect
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        rc = RespuestaCampo.objects.get(
            respuesta_formulario=ticket.respuesta_formulario, campo=self.campos["Opcional"]
        )
        self.assertEqual(rc.valor_texto, "No se pierde")

    def test_actor_ajeno_recibe_403_al_radicar(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="uvelez", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:radicar", args=[ticket.pk]), {f"campo_{self.campos['Texto'].id}": "x"}
        )
        self.assertEqual(respuesta.status_code, 403)


class DetalleViewTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="vcorrea", password=CLAVE_PRUEBA)
        self.otro_usuario = Usuario.objects.create_user(username="wgomez", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(self.ticket, self.usuario, self.campos)
        radicar_ticket(self.ticket, self.usuario)

    def test_propietario_ve_el_detalle(self):
        self.client.login(username="vcorrea", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 200)

    def test_usuario_ajeno_recibe_403_en_detalle(self):
        self.client.login(username="wgomez", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_detalle_de_un_borrador_redirige_al_formulario_editable(self):
        borrador = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="vcorrea", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[borrador.pk]))
        self.assertRedirects(respuesta, reverse("tickets:borrador", args=[borrador.pk]))

    def test_mis_tickets_incluye_borradores_y_radicados(self):
        crear_borrador(self.usuario, self.servicio)
        self.client.login(username="vcorrea", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        estados = {t.estado for t in respuesta.context["tickets"]}
        self.assertEqual(estados, {Ticket.Estado.BORRADOR, Ticket.Estado.RADICADO})


# ---------------------------------------------------------------------------
# 2.3 — Atención y asignación (CU-016/CU-017)
# ---------------------------------------------------------------------------


class EstadosTests(TestCase):
    """`apps/tickets/estados.py` — RN-018, única transición real de 2.3."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="xrivera", password=CLAVE_PRUEBA)
        self.servicio, _, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )

    def test_puede_ejecutar_tomar_desde_radicado(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        self.assertTrue(puede_ejecutar(ticket, "TOMAR"))
        self.assertTrue(puede_ejecutar(ticket, "ASIGNAR_USUARIO"))

    def test_no_puede_ejecutar_tomar_desde_borrador(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.assertFalse(puede_ejecutar(ticket, "TOMAR"))

    def test_no_existe_transicion_reasignar(self):
        # Decisión explícita del usuario: REASIGNAR nunca es una transición
        # de estado, ni siquiera EN_ATENCION -> EN_ATENCION.
        ticket = Ticket(estado=Ticket.Estado.EN_ATENCION)
        self.assertFalse(puede_ejecutar(ticket, "REASIGNAR"))

    def test_exigir_transicion_aplica_el_estado_resultante(self):
        ticket = Ticket(estado=Ticket.Estado.RADICADO)
        exigir_transicion(ticket, "TOMAR")
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_exigir_transicion_lanza_validationerror_si_no_hay_transicion(self):
        ticket = Ticket(estado=Ticket.Estado.EN_ATENCION)
        with self.assertRaises(ValidationError):
            exigir_transicion(ticket, "TOMAR")


class ContextoAtencionSnapshotTests(TestCase):
    """RQF-062/RN-019 — `radicar_ticket` congela los `ServicioContextoAtencion`
    activos del servicio en `TicketContextoAtencion`, sin elegir uno solo
    cuando el servicio es transversal."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="ysalazar", password=CLAVE_PRUEBA)
        self.servicio, _, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        self.area = Area.objects.create(nombre="TIC", codigo="TIC-SNAP")
        self.unidad = UnidadNegocio.objects.create(nombre="Infraestructura", codigo="INFRA-SNAP")

    def test_congela_todos_los_contextos_activos_del_servicio_transversal(self):
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA, area=self.area
        )
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio,
            tipo_alcance=ServicioContextoAtencion.TipoAlcance.UNIDAD,
            unidad_negocio=self.unidad,
        )
        ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)

        contextos = list(ticket.contextos_atencion.all())
        self.assertEqual(len(contextos), 2)
        tipos = {c.tipo_alcance for c in contextos}
        self.assertEqual(tipos, {"AREA", "UNIDAD"})

    def test_no_copia_contextos_inactivos(self):
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio,
            tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA,
            area=self.area,
            activo=False,
        )
        ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.contextos_atencion.count(), 0)

    def test_cambios_posteriores_en_la_configuracion_no_alteran_el_snapshot(self):
        contexto = ServicioContextoAtencion.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA, area=self.area
        )
        ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)

        contexto.activo = False
        contexto.save()
        otra_area = Area.objects.create(nombre="Financiera", codigo="FIN-SNAP")
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA, area=otra_area
        )

        snapshot = list(ticket.contextos_atencion.all())
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0].area_id, self.area.id)

    def test_sin_contextos_configurados_no_congela_nada(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.contextos_atencion.count(), 0)


class _EscenarioAtencionMixin:
    """Base compartida por las pruebas de autorización/operaciones de 2.3:
    un Servicio con un `ServicioResponsable` USUARIO directo, un
    `ServicioResponsable` EQUIPO (con un miembro), un contexto AREA, y un
    Ticket ya RADICADO con ese contexto congelado."""

    def _preparar_escenario(self):
        self.solicitante = Usuario.objects.create_user(username="solicitante23", password=CLAVE_PRUEBA)
        self.responsable_directo = Usuario.objects.create_user(username="responsable23", password=CLAVE_PRUEBA)
        self.miembro_equipo = Usuario.objects.create_user(username="miembro23", password=CLAVE_PRUEBA)
        self.gestor_area = Usuario.objects.create_user(username="gestorarea23", password=CLAVE_PRUEBA)
        self.gestor_otra_area = Usuario.objects.create_user(username="gestorotra23", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user(username="ajeno23", password=CLAVE_PRUEBA)

        self.equipo = Equipo.objects.create(nombre="Mesa de ayuda 2.3")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.miembro_equipo, activo=True)

        self.area = Area.objects.create(nombre="TIC", codigo="TIC-AT")
        self.otra_area = Area.objects.create(nombre="Financiera", codigo="FIN-AT")

        self.servicio, _, self.campos = _crear_servicio_con_formulario(
            self.solicitante, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        ServicioResponsable.objects.create(
            servicio=self.servicio,
            tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO,
            usuario=self.responsable_directo,
        )
        ServicioResponsable.objects.create(
            servicio=self.servicio, tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO, equipo=self.equipo
        )
        ServicioContextoAtencion.objects.create(
            servicio=self.servicio, tipo_alcance=ServicioContextoAtencion.TipoAlcance.AREA, area=self.area
        )

        _otorgar_tickets_atender(self.responsable_directo)
        _otorgar_tickets_atender(self.miembro_equipo)
        _otorgar_tickets_atender(
            self.gestor_area, tipo_alcance=AsignacionRol.TipoAlcance.AREA, area=self.area
        )
        _otorgar_tickets_atender(
            self.gestor_otra_area, tipo_alcance=AsignacionRol.TipoAlcance.AREA, area=self.otra_area
        )

        self.ticket = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(self.ticket, self.solicitante, self.campos)
        radicar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()


class AutorizacionAtencionTests(_EscenarioAtencionMixin, TestCase):
    """RQF-061, RQF-028, RN-006/009 — `apps/tickets/autorizacion.py`."""

    def setUp(self):
        self._preparar_escenario()

    def test_usuario_sin_permiso_no_ve_ni_puede_tomar(self):
        self.assertFalse(puede_ver_en_cola(self.ajeno, self.ticket))
        self.assertFalse(puede_tomar(self.ajeno, self.ticket))

    def test_responsable_directo_configurado_puede_tomar(self):
        self.assertTrue(usuario_es_responsable_configurado(self.responsable_directo, self.servicio))
        self.assertTrue(puede_tomar(self.responsable_directo, self.ticket))

    def test_miembro_de_equipo_responsable_puede_tomar(self):
        self.assertTrue(usuario_es_responsable_configurado(self.miembro_equipo, self.servicio))
        self.assertTrue(puede_tomar(self.miembro_equipo, self.ticket))

    def test_alcance_area_sin_relacion_operacional_no_basta_para_tomar(self):
        # RQF-028/RN-009: coincidir con el área del gestor no otorga por
        # sí solo capacidad de TOMAR — no está configurado como responsable.
        self.assertFalse(usuario_es_responsable_configurado(self.gestor_area, self.servicio))
        self.assertFalse(puede_tomar(self.gestor_area, self.ticket))

    def test_alcance_area_si_basta_para_asignar_a_un_tercero(self):
        # Función supervisora: sí basta para ASIGNAR, sin exigir que el
        # gestor esté configurado como responsable del servicio.
        self.assertTrue(puede_asignar(self.gestor_area, self.ticket))

    def test_area_distinta_no_cubre_el_ticket(self):
        self.assertFalse(puede_asignar(self.gestor_otra_area, self.ticket))
        self.assertFalse(puede_ver_en_cola(self.gestor_otra_area, self.ticket))

    def test_miembro_de_equipo_sin_permiso_atender_no_ve_ni_asigna(self):
        otro_miembro = Usuario.objects.create_user(username="miembrosinpermiso", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=otro_miembro, activo=True)
        self.assertFalse(puede_ver_en_cola(otro_miembro, self.ticket))
        self.assertFalse(puede_asignar(otro_miembro, self.ticket))

    def test_no_se_puede_tomar_un_ticket_en_borrador(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        self.assertFalse(puede_tomar(self.responsable_directo, borrador))
        self.assertFalse(puede_ver_en_cola(self.responsable_directo, borrador))

    def test_responsable_actual_puede_reasignar_sin_alcance_de_area(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertTrue(es_responsable_actual(self.responsable_directo, self.ticket))
        self.assertTrue(puede_reasignar(self.responsable_directo, self.ticket))


class TomarTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_responsable_configurado_toma_correctamente(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)

    def test_miembro_de_equipo_responsable_toma_correctamente(self):
        tomar_ticket(self.ticket, self.miembro_equipo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.miembro_equipo.id)

    def test_sin_autorizacion_no_puede_tomar(self):
        with self.assertRaises(PermissionDenied):
            tomar_ticket(self.ticket, self.ajeno)

    def test_gestor_de_area_no_puede_tomar_sin_relacion_operacional(self):
        with self.assertRaises(PermissionDenied):
            tomar_ticket(self.ticket, self.gestor_area)

    def test_no_se_puede_tomar_dos_veces(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            tomar_ticket(self.ticket, self.miembro_equipo)

    def test_no_se_puede_tomar_un_borrador(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        with self.assertRaises(PermissionDenied):
            tomar_ticket(borrador, self.responsable_directo)


class AsignarTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_gestor_area_asigna_usuario_dispara_transicion(self):
        asignar_ticket(self.ticket, self.gestor_area, usuario=self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)

    def test_asignar_solo_equipo_no_cambia_el_estado(self):
        asignar_ticket(self.ticket, self.gestor_area, equipo=self.equipo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(self.ticket.equipo_responsable_id, self.equipo.id)
        self.assertIsNone(self.ticket.usuario_responsable_id)

    def test_sin_alcance_no_puede_asignar(self):
        with self.assertRaises(PermissionDenied):
            asignar_ticket(self.ticket, self.gestor_otra_area, usuario=self.responsable_directo)

    def test_requiere_usuario_o_equipo(self):
        with self.assertRaises(ValidationError):
            asignar_ticket(self.ticket, self.gestor_area)

    def test_no_se_puede_asignar_si_ya_tiene_responsable(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            asignar_ticket(self.ticket, self.gestor_area, usuario=self.miembro_equipo)


class ReasignarTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_gestor_area_reasigna_correctamente(self):
        reasignar_ticket(self.ticket, self.gestor_area, usuario=self.miembro_equipo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual(self.ticket.usuario_responsable_id, self.miembro_equipo.id)

    def test_reasignar_no_pasa_por_estados_ni_cambia_estado(self):
        estado_previo = self.ticket.estado
        reasignar_ticket(self.ticket, self.gestor_area, equipo=self.equipo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, estado_previo)

    def test_responsable_actual_puede_reasignar_su_propio_ticket(self):
        reasignar_ticket(self.ticket, self.responsable_directo, usuario=self.miembro_equipo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.miembro_equipo.id)

    def test_sin_alcance_y_sin_ser_responsable_no_puede_reasignar(self):
        with self.assertRaises(PermissionDenied):
            reasignar_ticket(self.ticket, self.ajeno, usuario=self.miembro_equipo)

    def test_no_se_puede_reasignar_un_ticket_sin_tomar(self):
        otro_ticket = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(otro_ticket, self.solicitante, self.campos)
        radicar_ticket(otro_ticket, self.solicitante)
        with self.assertRaises(ValidationError):
            reasignar_ticket(otro_ticket, self.gestor_area, usuario=self.miembro_equipo)


class HistorialTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_radicar_registra_un_unico_evento_radicado(self):
        eventos = list(self.ticket.historial.all())
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0].tipo_evento, HistorialTicket.TipoEvento.RADICADO)
        self.assertEqual(eventos[0].actor_id, self.solicitante.id)

    def test_tomar_agrega_un_unico_evento_tomado(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        eventos = list(self.ticket.historial.all())
        self.assertEqual(len(eventos), 2)
        self.assertEqual(eventos[1].tipo_evento, HistorialTicket.TipoEvento.TOMADO)
        self.assertEqual(eventos[1].actor_id, self.responsable_directo.id)

    def test_orden_cronologico_radicado_luego_tomado_luego_reasignado(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        reasignar_ticket(self.ticket, self.gestor_area, usuario=self.miembro_equipo)
        tipos = [e.tipo_evento for e in self.ticket.historial.all()]
        self.assertEqual(
            tipos,
            [HistorialTicket.TipoEvento.RADICADO, HistorialTicket.TipoEvento.TOMADO, HistorialTicket.TipoEvento.REASIGNADO],
        )

    def test_asignar_solo_equipo_no_genera_evento_cambio_estado(self):
        asignar_ticket(self.ticket, self.gestor_area, equipo=self.equipo)
        tipos = [e.tipo_evento for e in self.ticket.historial.all()]
        self.assertEqual(tipos, [HistorialTicket.TipoEvento.RADICADO, HistorialTicket.TipoEvento.ASIGNADO])


class ConcurrenciaTomarTicketTests(_EscenarioAtencionMixin, TransactionTestCase):
    """Punto 12 (aprobado): dos usuarios intentando TOMAR el mismo ticket a
    la vez — solo uno gana, el otro recibe un error explícito en vez de
    sobrescribir en silencio. `TransactionTestCase` + hilos reales contra
    Postgres (no `TestCase`, que envuelve cada test en una única
    transacción y no serializa hilos de verdad)."""

    def setUp(self):
        self._preparar_escenario()

    def test_dos_tomas_concurrentes_solo_una_gana(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _intentar_tomar(usuario, clave):
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                tomar_ticket(ticket, usuario)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_tomado"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_intentar_tomar, args=(self.responsable_directo, "a"))
        hilo_b = threading.Thread(target=_intentar_tomar, args=(self.miembro_equipo, "b"))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_tomado"), 1)

        ticket = Ticket.objects.get(pk=self.ticket.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertIn(ticket.usuario_responsable_id, [self.responsable_directo.id, self.miembro_equipo.id])
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=ticket, tipo_evento=HistorialTicket.TipoEvento.TOMADO
            ).count(),
            1,
        )


class ConcurrenciaReasignarTicketTests(_EscenarioAtencionMixin, TransactionTestCase):
    """Punto 12 (aprobado), reasignación: sin `select_for_update()`, la
    segunda reasignación podría registrar en `HistorialTicket` un
    `usuario_anterior_id` obsoleto (leído antes de que la primera
    confirmara) en vez del responsable que la primera realmente dejó."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.candidato_a = Usuario.objects.create_user(username="candidatoa23", password=CLAVE_PRUEBA)
        self.candidato_b = Usuario.objects.create_user(username="candidatob23", password=CLAVE_PRUEBA)
        _otorgar_tickets_atender(self.candidato_a)
        _otorgar_tickets_atender(self.candidato_b)

    def test_reasignaciones_concurrentes_no_pierden_actualizaciones(self):
        barrera = threading.Barrier(2)
        errores = []

        def _reasignar(usuario_destino):
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                reasignar_ticket(ticket, self.gestor_area, usuario=usuario_destino)
            except Exception as exc:  # noqa: BLE001 - se reporta, no se oculta
                errores.append(exc)
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_reasignar, args=(self.candidato_a,))
        hilo_b = threading.Thread(target=_reasignar, args=(self.candidato_b,))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        self.assertEqual(errores, [])
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        self.assertIn(ticket.usuario_responsable_id, [self.candidato_a.id, self.candidato_b.id])

        eventos = list(
            HistorialTicket.objects.filter(
                ticket=ticket, tipo_evento=HistorialTicket.TipoEvento.REASIGNADO
            ).order_by("creado_en")
        )
        self.assertEqual(len(eventos), 2)
        primer_destino_id = eventos[0].datos["usuario_id"]
        segundo_anterior_id = eventos[1].datos["usuario_anterior_id"]
        self.assertEqual(primer_destino_id, segundo_anterior_id)


class ColaAtencionViewTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_responsable_configurado_ve_el_ticket_en_su_cola(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:cola"))
        self.assertIn(self.ticket, respuesta.context["tickets"])

    def test_gestor_de_area_lo_ve_en_supervision_y_no_como_algo_que_pueda_tomar(self):
        # 4.F3: la Cola es lo que se PUEDE tomar; la supervisión por alcance (asignar) es un filtro aparte.
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        self.assertNotIn(self.ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        supervision = self.client.get(reverse("tickets:cola"), {"ver": "supervision"})
        self.assertIn(self.ticket, supervision.context["tickets"])
        self.assertNotContains(supervision, reverse("tickets:tomar", args=[self.ticket.pk]))

    def test_usuario_sin_autorizacion_no_ve_el_ticket(self):
        self.client.login(username="ajeno23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:cola"))
        self.assertNotIn(self.ticket, respuesta.context["tickets"])

    def test_solicitante_no_ve_su_propio_ticket_solo_por_ser_solicitante(self):
        # Separación explícita: "Mis tickets" no se mezcla con la cola.
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:cola"))
        self.assertNotIn(self.ticket, respuesta.context["tickets"])


class DetalleViewAtencionTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_gestor_de_area_puede_ver_el_detalle_de_un_ticket_ajeno(self):
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["puede_asignar"])

    def test_usuario_sin_autorizacion_recibe_403(self):
        self.client.login(username="ajeno23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_tomar_view_exitoso_redirige_al_detalle_y_transiciona(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tickets:tomar", args=[self.ticket.pk]))
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_tomar_view_rechaza_get(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:tomar", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 405)

    def test_asignar_view_exitoso(self):
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:asignar", args=[self.ticket.pk]), {"usuario_id": self.responsable_directo.id}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)

    def test_reasignar_view_exitoso(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:reasignar", args=[self.ticket.pk]), {"usuario_id": self.miembro_equipo.id}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.miembro_equipo.id)

    def test_asignar_view_sin_autorizacion_no_modifica_el_ticket(self):
        # El `ajeno` tampoco puede VER el detalle (403), así que solo se
        # verifica el redirect sin seguirlo (`assertRedirects` exigiría
        # 200 en el destino) y que el ticket quedó intacto.
        self.client.login(username="ajeno23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:asignar", args=[self.ticket.pk]), {"usuario_id": self.responsable_directo.id}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.usuario_responsable_id)


# ---------------------------------------------------------------------------
# 2.4 — Comunicación, adjuntos operativos y solicitud de información
# (CU-018, RQF-006/007/052/057/058). Reutiliza `_EscenarioAtencionMixin`
# (solicitante/responsable_directo/miembro_equipo/gestor_area/
# gestor_otra_area/ajeno, ticket ya RADICADO) — cada clase toma sobre esa
# base al responsable concreto (`tomar_ticket`) cuando lo necesita.
# ---------------------------------------------------------------------------


class AutorizacionComunicacionTests(_EscenarioAtencionMixin, TestCase):
    """Corrección explícita del usuario: consultar y participar NO son lo
    mismo — `puede_comentar_ticket`/`puede_solicitar_informacion` no se
    definen en términos de `puede_consultar_ticket`."""

    def setUp(self):
        self._preparar_escenario()

    def test_solicitante_puede_comentar(self):
        self.assertTrue(puede_comentar_ticket(self.solicitante, self.ticket))

    def test_gestor_con_alcance_puede_consultar_pero_no_comentar(self):
        # El gestor SÍ puede consultar (por alcance), pero eso no le da
        # capacidad de escribir — solo el solicitante o el responsable actual.
        self.assertTrue(puede_consultar_ticket(self.gestor_area, self.ticket))
        self.assertFalse(puede_comentar_ticket(self.gestor_area, self.ticket))

    def test_responsable_actual_puede_comentar(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertTrue(puede_comentar_ticket(self.responsable_directo, self.ticket))

    def test_ajeno_no_puede_comentar_ni_consultar(self):
        self.assertFalse(puede_consultar_ticket(self.ajeno, self.ticket))
        self.assertFalse(puede_comentar_ticket(self.ajeno, self.ticket))

    def test_no_se_puede_comentar_un_borrador(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        self.assertFalse(puede_comentar_ticket(self.solicitante, borrador))

    def test_solo_responsable_actual_puede_solicitar_informacion(self):
        # RQF-058 (actor "Ejecutor"): ni el gestor con alcance (sin ser
        # responsable de ESTE ticket) ni el solicitante pueden solicitar.
        self.assertFalse(puede_solicitar_informacion(self.gestor_area, self.ticket))
        self.assertFalse(puede_solicitar_informacion(self.solicitante, self.ticket))
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertTrue(puede_solicitar_informacion(self.responsable_directo, self.ticket))

    def test_puede_responder_solicitud_exige_ser_el_destinatario(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Puedes confirmar el equipo?")
        # R.1: destinatario siempre el solicitante del Ticket.
        self.assertEqual(solicitud.destinatario_id, self.solicitante.id)
        self.assertTrue(puede_responder_solicitud(self.solicitante, solicitud))
        self.assertFalse(puede_responder_solicitud(self.responsable_directo, solicitud))
        self.assertFalse(puede_responder_solicitud(self.ajeno, solicitud))


class ComentarTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_solicitante_comenta_correctamente(self):
        comentario = comentar_ticket(self.ticket, self.solicitante, "Quedo atento.")
        self.assertEqual(comentario.autor_id, self.solicitante.id)
        self.assertEqual(comentario.contenido, "Quedo atento.")
        self.assertIn(comentario, self.ticket.comentarios.all())

    def test_responsable_actual_comenta_correctamente(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        comentario = comentar_ticket(self.ticket, self.responsable_directo, "Estoy revisando el caso.")
        self.assertEqual(comentario.autor_id, self.responsable_directo.id)

    def test_gestor_con_alcance_sin_ser_responsable_no_puede_comentar(self):
        with self.assertRaises(PermissionDenied):
            comentar_ticket(self.ticket, self.gestor_area, "Intento no autorizado.")

    def test_ajeno_no_puede_comentar(self):
        with self.assertRaises(PermissionDenied):
            comentar_ticket(self.ticket, self.ajeno, "Intento no autorizado.")

    def test_comentario_vacio_es_rechazado(self):
        with self.assertRaises(ValidationError):
            comentar_ticket(self.ticket, self.solicitante, "   ")

    def test_no_se_puede_comentar_un_borrador(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        with self.assertRaises(PermissionDenied):
            comentar_ticket(borrador, self.solicitante, "Todavía no.")

    def test_comentarios_quedan_en_orden_cronologico(self):
        comentar_ticket(self.ticket, self.solicitante, "Primero.")
        comentar_ticket(self.ticket, self.solicitante, "Segundo.")
        contenidos = list(self.ticket.comentarios.values_list("contenido", flat=True))
        self.assertEqual(contenidos, ["Primero.", "Segundo."])

    def test_comentario_no_genera_evento_en_historial(self):
        # Decisión explícita 2.4: sin evento espejo por comentario/adjunto.
        comentar_ticket(self.ticket, self.solicitante, "Sin eco en el historial.")
        self.assertFalse(
            HistorialTicket.objects.filter(
                ticket=self.ticket,
                tipo_evento__in=[
                    HistorialTicket.TipoEvento.INFORMACION_SOLICITADA,
                    HistorialTicket.TipoEvento.INFORMACION_RESPONDIDA,
                ],
            ).exists()
        )


class ComentarioConAdjuntoTests(_MediaAisladaMixin, _EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_comentario_con_archivo_crea_adjunto_asociado(self):
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        comentario = comentar_ticket(self.ticket, self.solicitante, "Ver adjunto.", archivos=[archivo])
        self.assertEqual(comentario.adjuntos.count(), 1)
        adjunto = comentario.adjuntos.get()
        self.assertEqual(adjunto.tipo_relacion, Adjunto.TipoRelacion.COMENTARIO)
        self.assertEqual(adjunto.nombre_original, "evidencia.txt")
        self.assertEqual(adjunto.tamano_bytes, len(b"contenido"))
        self.assertEqual(adjunto.subido_por_id, self.solicitante.id)
        self.assertEqual(adjunto.ticket_relacionado.pk, self.ticket.pk)

    def test_archivo_vacio_es_rechazado(self):
        archivo = SimpleUploadedFile("vacio.txt", b"", content_type="text/plain")
        with self.assertRaises(ValidationError):
            comentar_ticket(self.ticket, self.solicitante, "Adjunto vacío.", archivos=[archivo])


class AdjuntarArchivoTicketTests(_MediaAisladaMixin, _EscenarioAtencionMixin, TestCase):
    """R.4 aprobado: adjunto directo al Ticket, sin exigir un comentario."""

    def setUp(self):
        self._preparar_escenario()

    def test_solicitante_adjunta_directo_al_ticket(self):
        archivo = SimpleUploadedFile("plano.pdf", b"contenido", content_type="application/pdf")
        adjunto = adjuntar_archivo_ticket(self.ticket, self.solicitante, archivo)
        self.assertEqual(adjunto.tipo_relacion, Adjunto.TipoRelacion.TICKET)
        self.assertEqual(adjunto.ticket_id, self.ticket.pk)
        self.assertIsNone(adjunto.comentario_id)
        self.assertIn(adjunto, self.ticket.adjuntos.all())

    def test_gestor_con_alcance_sin_ser_responsable_no_puede_adjuntar(self):
        archivo = SimpleUploadedFile("plano.pdf", b"contenido", content_type="application/pdf")
        with self.assertRaises(PermissionDenied):
            adjuntar_archivo_ticket(self.ticket, self.gestor_area, archivo)

    def test_ajeno_no_puede_adjuntar(self):
        archivo = SimpleUploadedFile("plano.pdf", b"contenido", content_type="application/pdf")
        with self.assertRaises(PermissionDenied):
            adjuntar_archivo_ticket(self.ticket, self.ajeno, archivo)


class AdjuntoModeloTests(TestCase):
    """`CheckConstraint` de coherencia del discriminador — sin `GenericForeignKey`
    (4 ramas conocidas: TICKET/COMENTARIO/SOLICITUD/RESPUESTA_SOLICITUD)."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="autoradj24", password=CLAVE_PRUEBA)
        self.servicio, _, _ = _crear_servicio_con_formulario(self.usuario, [])
        self.ticket = crear_borrador(self.usuario, self.servicio)

    def _archivo(self):
        return SimpleUploadedFile("a.txt", b"x", content_type="text/plain")

    def test_tipo_ticket_exige_solo_ticket_poblado(self):
        Adjunto.objects.create(
            tipo_relacion=Adjunto.TipoRelacion.TICKET,
            ticket=self.ticket,
            archivo=self._archivo(),
            nombre_original="a.txt",
            tamano_bytes=1,
            subido_por=self.usuario,
        )

    def test_tipo_ticket_con_comentario_tambien_poblado_es_rechazado(self):
        comentario = ComentarioTicket.objects.create(ticket=self.ticket, autor=self.usuario, contenido="x")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Adjunto.objects.create(
                    tipo_relacion=Adjunto.TipoRelacion.TICKET,
                    ticket=self.ticket,
                    comentario=comentario,
                    archivo=self._archivo(),
                    nombre_original="a.txt",
                    tamano_bytes=1,
                    subido_por=self.usuario,
                )

    def test_tipo_comentario_sin_comentario_poblado_es_rechazado(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Adjunto.objects.create(
                    tipo_relacion=Adjunto.TipoRelacion.COMENTARIO,
                    archivo=self._archivo(),
                    nombre_original="a.txt",
                    tamano_bytes=1,
                    subido_por=self.usuario,
                )


class SolicitarInformacionTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_responsable_actual_solicita_correctamente(self):
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Cuál es el equipo afectado?")
        self.assertEqual(solicitud.solicitada_por_id, self.responsable_directo.id)
        self.assertEqual(solicitud.destinatario_id, self.solicitante.id)
        self.assertEqual(solicitud.estado, SolicitudInformacion.Estado.PENDIENTE)

    def test_destinatario_no_se_puede_forzar_a_otro_distinto_del_solicitante(self):
        # R.1: `solicitar_informacion` no acepta destinatario por parámetro —
        # siempre se deriva de `ticket.solicitante`.
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "Mensaje.")
        self.assertEqual(solicitud.destinatario_id, self.ticket.solicitante_id)

    def test_gestor_con_alcance_no_puede_solicitar(self):
        with self.assertRaises(PermissionDenied):
            solicitar_informacion(self.ticket, self.gestor_area, "Mensaje.")

    def test_solicitante_no_puede_solicitarse_informacion_a_si_mismo(self):
        with self.assertRaises(PermissionDenied):
            solicitar_informacion(self.ticket, self.solicitante, "Mensaje.")

    def test_mensaje_vacio_es_rechazado(self):
        with self.assertRaises(ValidationError):
            solicitar_informacion(self.ticket, self.responsable_directo, "   ")

    def test_permite_varias_solicitudes_simultaneas_pendientes(self):
        solicitar_informacion(self.ticket, self.responsable_directo, "Primera.")
        solicitar_informacion(self.ticket, self.responsable_directo, "Segunda.")
        self.assertEqual(
            self.ticket.solicitudes_informacion.filter(estado=SolicitudInformacion.Estado.PENDIENTE).count(), 2
        )

    def test_registra_evento_informacion_solicitada_en_historial(self):
        solicitar_informacion(self.ticket, self.responsable_directo, "Mensaje.")
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.INFORMACION_SOLICITADA
            ).count(),
            1,
        )


class ResponderSolicitudTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Confirmas el equipo?")

    def test_destinatario_responde_correctamente(self):
        respuesta = responder_solicitud(self.solicitud, self.solicitante, "Sí, es el equipo A.")
        self.assertEqual(respuesta.respondida_por_id, self.solicitante.id)
        self.solicitud.refresh_from_db()
        self.assertEqual(self.solicitud.estado, SolicitudInformacion.Estado.RESPONDIDA)
        self.assertEqual(self.solicitud.respuesta, respuesta)

    def test_no_destinatario_no_puede_responder(self):
        with self.assertRaises(PermissionDenied):
            responder_solicitud(self.solicitud, self.responsable_directo, "Respuesta no autorizada.")

    def test_ajeno_no_puede_responder(self):
        with self.assertRaises(PermissionDenied):
            responder_solicitud(self.solicitud, self.ajeno, "Respuesta no autorizada.")

    def test_respuesta_vacia_es_rechazada(self):
        with self.assertRaises(ValidationError):
            responder_solicitud(self.solicitud, self.solicitante, "   ")

    def test_no_se_puede_responder_dos_veces(self):
        responder_solicitud(self.solicitud, self.solicitante, "Primera respuesta.")
        with self.assertRaises(ValidationError):
            responder_solicitud(self.solicitud, self.solicitante, "Segunda respuesta.")

    def test_segunda_respuesta_no_crea_fila_duplicada(self):
        responder_solicitud(self.solicitud, self.solicitante, "Primera respuesta.")
        with self.assertRaises(ValidationError):
            responder_solicitud(self.solicitud, self.solicitante, "Segunda respuesta.")
        self.assertEqual(
            RespuestaSolicitudInformacion.objects.filter(solicitud=self.solicitud).count(), 1
        )

    def test_registra_evento_informacion_respondida_en_historial(self):
        responder_solicitud(self.solicitud, self.solicitante, "Respuesta.")
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.INFORMACION_RESPONDIDA
            ).count(),
            1,
        )

    def test_respuesta_con_archivo_crea_adjunto_asociado(self):
        archivo = SimpleUploadedFile("respaldo.txt", b"contenido", content_type="text/plain")
        respuesta = responder_solicitud(self.solicitud, self.solicitante, "Con evidencia.", archivos=[archivo])
        self.assertEqual(respuesta.adjuntos.count(), 1)
        adjunto = respuesta.adjuntos.get()
        self.assertEqual(adjunto.tipo_relacion, Adjunto.TipoRelacion.RESPUESTA_SOLICITUD)
        self.assertEqual(adjunto.ticket_relacionado.pk, self.ticket.pk)


class ConcurrenciaResponderSolicitudTests(_EscenarioAtencionMixin, TransactionTestCase):
    """Punto 8 (aprobado): dos respuestas simultáneas a la misma solicitud —
    solo una gana. Mismo patrón que `ConcurrenciaTomarTicketTests` (hilos
    reales contra Postgres, `TransactionTestCase`)."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Confirmas?")

    def test_dos_respuestas_concurrentes_solo_una_gana(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _intentar_responder(clave, contenido):
            barrera.wait()
            try:
                solicitud = SolicitudInformacion.objects.get(pk=self.solicitud.pk)
                responder_solicitud(solicitud, self.solicitante, contenido)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_respondida"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_intentar_responder, args=("a", "Respuesta A"))
        hilo_b = threading.Thread(target=_intentar_responder, args=("b", "Respuesta B"))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_respondida"), 1)
        self.assertEqual(
            RespuestaSolicitudInformacion.objects.filter(solicitud=self.solicitud).count(), 1
        )
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.INFORMACION_RESPONDIDA
            ).count(),
            1,
        )


class DescargarAdjuntoViewTests(_MediaAisladaMixin, _EscenarioAtencionMixin, TestCase):
    """RQF-006/RQ-NFN-04 — descarga de `Adjunto` centralizada en
    `puede_consultar_ticket` (nunca por conocer la URL de MEDIA)."""

    def setUp(self):
        self._preparar_escenario()
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        self.adjunto = adjuntar_archivo_ticket(self.ticket, self.solicitante, archivo)

    def test_solicitante_puede_descargar(self):
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_adjunto", args=[self.adjunto.pk]))
        self.assertEqual(respuesta.status_code, 200)

    def test_gestor_con_alcance_puede_descargar(self):
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_adjunto", args=[self.adjunto.pk]))
        self.assertEqual(respuesta.status_code, 200)

    def test_ajeno_no_puede_descargar(self):
        self.client.login(username="ajeno23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_adjunto", args=[self.adjunto.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_intento_de_descarga_sin_autenticar_redirige_a_login(self):
        respuesta = self.client.get(reverse("tickets:descargar_adjunto", args=[self.adjunto.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)


class DescargaArchivoRespuestaRegresionTests(_MediaAisladaMixin, TestCase):
    """2.4 corrige RQF-006 en `descargar_archivo_respuesta_view`: antes solo
    el solicitante podía descargar (`es_propietario_borrador`); ahora
    cualquiera autorizado a CONSULTAR el ticket (`puede_consultar_ticket`)
    también puede — mismo criterio que ya aplicaba al detalle desde 2.3."""

    def setUp(self):
        self.solicitante = Usuario.objects.create_user(username="solicitante24da", password=CLAVE_PRUEBA)
        self.gestor = Usuario.objects.create_user(username="gestor24da", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user(username="ajeno24da", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.solicitante, [{"tipo": Campo.TipoCampo.ARCHIVO, "etiqueta": "Soporte"}]
        )
        self.ticket = crear_borrador(self.solicitante, self.servicio)
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        guardar_respuestas_borrador(self.ticket, self.solicitante, {self.campos["Soporte"].id: archivo})
        radicar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        _otorgar_tickets_atender(self.gestor)
        self.archivo_id = RespuestaCampo.objects.get(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos["Soporte"]
        ).archivo.pk

    def test_solicitante_sigue_pudiendo_descargar(self):
        self.client.login(username="solicitante24da", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_archivo", args=[self.archivo_id]))
        self.assertEqual(respuesta.status_code, 200)

    def test_gestor_autorizado_a_consultar_ahora_tambien_puede_descargar(self):
        self.client.login(username="gestor24da", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_archivo", args=[self.archivo_id]))
        self.assertEqual(respuesta.status_code, 200)

    def test_ajeno_sigue_sin_poder_descargar(self):
        self.client.login(username="ajeno24da", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:descargar_archivo", args=[self.archivo_id]))
        self.assertEqual(respuesta.status_code, 403)


class ComunicacionViewsTests(_EscenarioAtencionMixin, TestCase):
    """Vistas POST de 2.4 — mismo patrón que `DetalleViewAtencionTests`
    (2.3): la vista solo hace `get_object_or_404` + delega en `operaciones`,
    sin repetir autorización; `PermissionDenied` desde la operación se
    captura y se muestra como mensaje (no 403 — el ajeno sí puede ver el
    ticket si tiene alcance, pero no puede participar)."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_comentar_view_exitoso(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:comentar", args=[self.ticket.pk]), {"contenido": "Ya lo reviso."}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(self.ticket.comentarios.count(), 1)

    def test_comentar_view_rechaza_get(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:comentar", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 405)

    def test_comentar_view_sin_autorizacion_no_crea_comentario(self):
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:comentar", args=[self.ticket.pk]), {"contenido": "No autorizado."}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self.ticket.comentarios.count(), 0)

    def test_solicitar_informacion_view_exitoso(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:solicitar_informacion", args=[self.ticket.pk]), {"mensaje": "¿Confirmas?"}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(self.ticket.solicitudes_informacion.count(), 1)

    def test_responder_solicitud_view_exitoso(self):
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Confirmas?")
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:responder_solicitud", args=[self.ticket.pk, solicitud.pk]),
            {"contenido": "Confirmado."},
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudInformacion.Estado.RESPONDIDA)

    def test_responder_solicitud_view_no_destinatario_no_responde(self):
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Confirmas?")
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:responder_solicitud", args=[self.ticket.pk, solicitud.pk]),
            {"contenido": "Intento no autorizado."},
        )
        self.assertEqual(respuesta.status_code, 302)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, SolicitudInformacion.Estado.PENDIENTE)

    def test_detalle_view_expone_contexto_de_comunicacion(self):
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertIn("comentarios", respuesta.context)
        self.assertIn("solicitudes", respuesta.context)
        self.assertTrue(respuesta.context["puede_comentar"])
        self.assertFalse(respuesta.context["puede_solicitar_informacion"])


# ---------------------------------------------------------------------------
# 2.5 — Resolución, cierre, cancelación y reapertura (CU-019, RQF-059).
# Reutiliza `_EscenarioAtencionMixin` (mismo escenario de 2.3/2.4).
# `tomar_ticket`/`asignar_ticket`/`reasignar_ticket` (2.3) NO generan
# `RegistroAuditoria` (decisión explícita: no se corrige retroactivamente
# en 2.5) — solo resolver/cerrar/cancelar/reabrir lo hacen.
# ---------------------------------------------------------------------------


class AutorizacionFinalizacionTests(_EscenarioAtencionMixin, TestCase):
    """V1 aprobado: consultar ≠ gestionar asignación ≠ participar ≠
    finalizar. Alcance de `tickets.atender` (gestor_area) no concede por
    sí solo resolver/cerrar/cancelar/reabrir."""

    def setUp(self):
        self._preparar_escenario()

    def test_solo_responsable_actual_puede_resolver(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertTrue(puede_resolver_ticket(self.responsable_directo, self.ticket))
        self.assertFalse(puede_resolver_ticket(self.gestor_area, self.ticket))
        self.assertFalse(puede_resolver_ticket(self.solicitante, self.ticket))
        self.assertFalse(puede_resolver_ticket(self.ajeno, self.ticket))

    def test_solicitante_o_responsable_pueden_cerrar(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.assertTrue(puede_cerrar_ticket(self.solicitante, self.ticket))
        self.assertTrue(puede_cerrar_ticket(self.responsable_directo, self.ticket))
        self.assertFalse(puede_cerrar_ticket(self.gestor_area, self.ticket))
        self.assertFalse(puede_cerrar_ticket(self.ajeno, self.ticket))

    def test_solicitante_o_responsable_pueden_cancelar_desde_radicado(self):
        self.assertTrue(puede_cancelar_ticket(self.solicitante, self.ticket))
        self.assertFalse(puede_cancelar_ticket(self.gestor_area, self.ticket))
        self.assertFalse(puede_cancelar_ticket(self.ajeno, self.ticket))

    def test_solicitante_o_responsable_pueden_cancelar_desde_en_atencion(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertTrue(puede_cancelar_ticket(self.solicitante, self.ticket))
        self.assertTrue(puede_cancelar_ticket(self.responsable_directo, self.ticket))
        self.assertFalse(puede_cancelar_ticket(self.gestor_area, self.ticket))

    def test_solo_responsable_actual_puede_reabrir(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.assertTrue(puede_reabrir_ticket(self.responsable_directo, self.ticket))
        self.assertFalse(puede_reabrir_ticket(self.solicitante, self.ticket))
        self.assertFalse(puede_reabrir_ticket(self.gestor_area, self.ticket))


class ResolverTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_responsable_actual_resuelve_correctamente(self):
        resolucion = resolver_ticket(self.ticket, self.responsable_directo, "Se reemplazó el equipo.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RESUELTO)
        self.assertEqual(resolucion.ticket_id, self.ticket.pk)
        self.assertEqual(resolucion.resuelto_por_id, self.responsable_directo.id)
        self.assertEqual(resolucion.descripcion, "Se reemplazó el equipo.")

    def test_resolver_bloqueado_por_solicitud_pendiente(self):
        solicitar_informacion(self.ticket, self.responsable_directo, "¿Confirmas el equipo?")
        with self.assertRaises(ValidationError):
            resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertFalse(ResolucionTicket.objects.filter(ticket=self.ticket).exists())

    def test_resolver_permitido_tras_responder_la_solicitud(self):
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Confirmas el equipo?")
        responder_solicitud(solicitud, self.solicitante, "Sí, confirmado.")
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RESUELTO)

    def test_descripcion_vacia_es_rechazada(self):
        with self.assertRaises(ValidationError):
            resolver_ticket(self.ticket, self.responsable_directo, "   ")

    def test_gestor_por_alcance_no_puede_resolver(self):
        with self.assertRaises(PermissionDenied):
            resolver_ticket(self.ticket, self.gestor_area, "Intento no autorizado.")

    def test_ajeno_no_puede_resolver(self):
        with self.assertRaises(PermissionDenied):
            resolver_ticket(self.ticket, self.ajeno, "Intento no autorizado.")

    def test_doble_submit_resolver_no_duplica_nada(self):
        # "resolución solo puede existir una vez" + "doble submit no
        # duplica ninguno de los anteriores" (puntos 15 aprobados).
        resolver_ticket(self.ticket, self.responsable_directo, "Primera resolución.")
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            resolver_ticket(self.ticket, self.responsable_directo, "Segunda resolución.")

        self.assertEqual(ResolucionTicket.objects.filter(ticket=self.ticket).count(), 1)
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.RESUELTO
            ).count(),
            1,
        )
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "RESUELTO"}).count(), 1
        )

    def test_resolver_genera_exactamente_un_historial(self):
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.RESUELTO
            ).count(),
            1,
        )

    def test_resolver_genera_exactamente_una_auditoria_de_cambio_de_estado(self):
        # `self.setUp` ya ejecuta TOMAR, que desde 2.C también genera su
        # propia `RegistroAuditoria` (RQF-119) — se filtra explícitamente
        # por el `datos_nuevos` de ESTA transición para no confundirla.
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        auditorias = _auditorias_de_ticket(self.ticket).filter(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, datos_nuevos={"estado": "RESUELTO"}
        )
        self.assertEqual(auditorias.count(), 1)
        registro = auditorias.get()
        self.assertEqual(registro.usuario_id, self.responsable_directo.id)
        self.assertEqual(registro.origen, RegistroAuditoria.Origen.USUARIO)
        self.assertEqual(registro.datos_anteriores, {"estado": "EN_ATENCION"})
        self.assertEqual(registro.datos_nuevos, {"estado": "RESUELTO"})

    def test_responsable_se_preserva_tras_resolver(self):
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)

    def test_resolver_de_nuevo_tras_reabrir_funciona(self):
        # 2.C — corrección: `RESUELTO → REABRIR → EN_ATENCION` es una
        # transición aprobada, y un ticket reabierto SÍ puede resolverse
        # de nuevo. `ResolucionTicket.ticket` pasó de `OneToOneField` a
        # `ForeignKey` (related_name="resoluciones") exactamente para esto.
        primera = resolver_ticket(self.ticket, self.responsable_directo, "Primera resolución.")
        self.ticket.refresh_from_db()
        reabrir_ticket(self.ticket, self.responsable_directo, "Falta un detalle.")
        self.ticket.refresh_from_db()

        segunda = resolver_ticket(self.ticket, self.responsable_directo, "Segunda resolución.")
        self.ticket.refresh_from_db()

        self.assertEqual(self.ticket.estado, Ticket.Estado.RESUELTO)
        self.assertEqual(ResolucionTicket.objects.filter(ticket=self.ticket).count(), 2)
        self.assertNotEqual(primera.pk, segunda.pk)

    def test_primera_resolucion_permanece_intacta_tras_la_segunda(self):
        primera = resolver_ticket(self.ticket, self.responsable_directo, "Primera resolución.")
        self.ticket.refresh_from_db()
        reabrir_ticket(self.ticket, self.responsable_directo, "Falta un detalle.")
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Segunda resolución.")

        primera.refresh_from_db()
        self.assertEqual(primera.descripcion, "Primera resolución.")
        self.assertEqual(primera.resuelto_por_id, self.responsable_directo.id)

    def test_segunda_resolucion_es_la_mas_reciente(self):
        resolver_ticket(self.ticket, self.responsable_directo, "Primera resolución.")
        self.ticket.refresh_from_db()
        reabrir_ticket(self.ticket, self.responsable_directo, "Falta un detalle.")
        self.ticket.refresh_from_db()
        segunda = resolver_ticket(self.ticket, self.responsable_directo, "Segunda resolución.")

        mas_reciente = self.ticket.resoluciones.order_by("-resuelto_en").first()
        self.assertEqual(mas_reciente.pk, segunda.pk)
        self.assertEqual(mas_reciente.descripcion, "Segunda resolución.")


class ResolverConAdjuntoTests(_MediaAisladaMixin, _EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_adjunto_resolucion_queda_correctamente_asociado(self):
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        resolucion = resolver_ticket(
            self.ticket, self.responsable_directo, "Resuelto con evidencia.", archivos=[archivo]
        )
        self.assertEqual(resolucion.adjuntos.count(), 1)
        adjunto = resolucion.adjuntos.get()
        self.assertEqual(adjunto.tipo_relacion, Adjunto.TipoRelacion.RESOLUCION)
        self.assertEqual(adjunto.nombre_original, "evidencia.txt")
        self.assertEqual(adjunto.ticket_relacionado.pk, self.ticket.pk)

    def test_adjuntos_de_resoluciones_distintas_no_se_mezclan(self):
        # 2.C, punto 2: cada Adjunto.RESOLUCION apunta a SU resolución
        # concreta — dos ciclos RESOLVER→REABRIR→RESOLVER no deben mezclar
        # la evidencia de uno con la del otro.
        archivo_a = SimpleUploadedFile("evidencia_a.txt", b"A", content_type="text/plain")
        primera = resolver_ticket(
            self.ticket, self.responsable_directo, "Primera resolución.", archivos=[archivo_a]
        )
        self.ticket.refresh_from_db()
        reabrir_ticket(self.ticket, self.responsable_directo, "Falta un detalle.")
        self.ticket.refresh_from_db()

        archivo_b = SimpleUploadedFile("evidencia_b.txt", b"B", content_type="text/plain")
        segunda = resolver_ticket(
            self.ticket, self.responsable_directo, "Segunda resolución.", archivos=[archivo_b]
        )

        self.assertEqual(primera.adjuntos.count(), 1)
        self.assertEqual(primera.adjuntos.get().nombre_original, "evidencia_a.txt")
        self.assertEqual(segunda.adjuntos.count(), 1)
        self.assertEqual(segunda.adjuntos.get().nombre_original, "evidencia_b.txt")


class CerrarTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()

    def test_solicitante_puede_cerrar(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CERRADO)

    def test_responsable_actual_puede_cerrar(self):
        cerrar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CERRADO)

    def test_gestor_por_alcance_no_puede_cerrar(self):
        with self.assertRaises(PermissionDenied):
            cerrar_ticket(self.ticket, self.gestor_area)

    def test_ajeno_no_puede_cerrar(self):
        with self.assertRaises(PermissionDenied):
            cerrar_ticket(self.ticket, self.ajeno)

    def test_no_se_puede_cerrar_sin_estar_resuelto(self):
        otro = crear_borrador(self.solicitante, self.servicio)
        with self.assertRaises(ValidationError):
            cerrar_ticket(otro, self.solicitante)

    def test_cerrado_es_terminal_no_se_reabre(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            reabrir_ticket(self.ticket, self.responsable_directo, "Motivo.")

    def test_cerrado_es_terminal_no_se_cancela(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            cancelar_ticket(self.ticket, self.solicitante, "Motivo.")

    def test_doble_submit_cerrar_no_duplica_nada(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            cerrar_ticket(self.ticket, self.solicitante)
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.CERRADO
            ).count(),
            1,
        )
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "CERRADO"}).count(), 1
        )

    def test_cerrar_genera_exactamente_un_historial_y_una_auditoria(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.CERRADO
            ).count(),
            1,
        )
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "CERRADO"}).count(), 1
        )

    def test_responsable_se_preserva_tras_cerrar(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)


class CancelarTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_solicitante_cancela_desde_radicado(self):
        cancelar_ticket(self.ticket, self.solicitante, "Ya no se necesita.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CANCELADO)

    def test_responsable_actual_cancela_desde_en_atencion(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        cancelar_ticket(self.ticket, self.responsable_directo, "No se puede atender.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CANCELADO)

    def test_motivo_obligatorio(self):
        with self.assertRaises(ValidationError):
            cancelar_ticket(self.ticket, self.solicitante, "   ")

    def test_gestor_por_alcance_no_puede_cancelar(self):
        with self.assertRaises(PermissionDenied):
            cancelar_ticket(self.ticket, self.gestor_area, "Motivo.")

    def test_ajeno_no_puede_cancelar(self):
        with self.assertRaises(PermissionDenied):
            cancelar_ticket(self.ticket, self.ajeno, "Motivo.")

    def test_resuelto_no_se_puede_cancelar(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            cancelar_ticket(self.ticket, self.solicitante, "Motivo.")

    def test_cancelado_es_terminal(self):
        cancelar_ticket(self.ticket, self.solicitante, "Motivo.")
        self.ticket.refresh_from_db()
        # "Terminal" se verifica primero contra la propia tabla de estados
        # (RN-018: ¿existe la transición?) — ninguna sale de CANCELADO.
        self.assertFalse(puede_ejecutar(self.ticket, "TOMAR"))
        self.assertFalse(puede_ejecutar(self.ticket, "CANCELAR"))
        self.assertFalse(puede_ejecutar(self.ticket, "RESOLVER"))
        # A nivel de operación, `tomar_ticket` la rechaza igual — el tipo
        # exacto de excepción depende de qué chequeo llega primero
        # (`puede_tomar` ya gatea por estado antes de llegar a
        # `exigir_transicion`), así que se acepta cualquiera de los dos.
        with self.assertRaises((PermissionDenied, ValidationError)):
            tomar_ticket(self.ticket, self.responsable_directo)

    def test_cancelar_no_elimina_fisicamente_el_ticket(self):
        cancelar_ticket(self.ticket, self.solicitante, "Motivo.")
        self.assertTrue(Ticket.objects.filter(pk=self.ticket.pk).exists())

    def test_no_se_puede_eliminar_fisicamente_un_ticket_cancelado(self):
        cancelar_ticket(self.ticket, self.solicitante, "Motivo.")
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.ticket.delete()
        self.assertTrue(Ticket.objects.filter(pk=self.ticket.pk).exists())

    def test_doble_submit_cancelar_no_duplica_nada(self):
        cancelar_ticket(self.ticket, self.solicitante, "Primer motivo.")
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            cancelar_ticket(self.ticket, self.solicitante, "Segundo motivo.")
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.CANCELADO
            ).count(),
            1,
        )
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "CANCELADO"}).count(), 1
        )

    def test_cancelar_genera_historial_con_motivo_y_una_auditoria(self):
        cancelar_ticket(self.ticket, self.solicitante, "Ya no se necesita.")
        evento = HistorialTicket.objects.get(
            ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.CANCELADO
        )
        self.assertEqual(evento.datos["motivo"], "Ya no se necesita.")
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "CANCELADO"}).count(), 1
        )

    def test_responsable_se_preserva_tras_cancelar(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        cancelar_ticket(self.ticket, self.responsable_directo, "Motivo.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)


class ReabrirTicketTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()

    def test_responsable_actual_reabre_correctamente(self):
        reabrir_ticket(self.ticket, self.responsable_directo, "Faltó un detalle.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_motivo_obligatorio(self):
        with self.assertRaises(ValidationError):
            reabrir_ticket(self.ticket, self.responsable_directo, "   ")

    def test_solicitante_no_puede_reabrir(self):
        with self.assertRaises(PermissionDenied):
            reabrir_ticket(self.ticket, self.solicitante, "Motivo.")

    def test_gestor_por_alcance_no_puede_reabrir(self):
        with self.assertRaises(PermissionDenied):
            reabrir_ticket(self.ticket, self.gestor_area, "Motivo.")

    def test_cerrado_no_se_reabre(self):
        cerrar_ticket(self.ticket, self.solicitante)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            reabrir_ticket(self.ticket, self.responsable_directo, "Motivo.")

    def test_reabrir_conserva_responsable_y_equipo(self):
        reabrir_ticket(self.ticket, self.responsable_directo, "Motivo.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.usuario_responsable_id, self.responsable_directo.id)

    def test_no_crea_una_nueva_resolucion_ni_elimina_la_existente(self):
        reabrir_ticket(self.ticket, self.responsable_directo, "Motivo.")
        self.assertEqual(ResolucionTicket.objects.filter(ticket=self.ticket).count(), 1)

    def test_doble_submit_reabrir_no_duplica_nada(self):
        reabrir_ticket(self.ticket, self.responsable_directo, "Primer motivo.")
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            reabrir_ticket(self.ticket, self.responsable_directo, "Segundo motivo.")
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.REABIERTO
            ).count(),
            1,
        )
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "EN_ATENCION"}).count(), 1
        )

    def test_reabrir_genera_historial_con_motivo_y_una_auditoria(self):
        reabrir_ticket(self.ticket, self.responsable_directo, "Faltó un detalle.")
        evento = HistorialTicket.objects.get(
            ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.REABIERTO
        )
        self.assertEqual(evento.datos["motivo"], "Faltó un detalle.")
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(datos_nuevos={"estado": "EN_ATENCION"}).count(), 1
        )


class ConcurrenciaFinalizacionTests(_EscenarioAtencionMixin, TransactionTestCase):
    """Punto 11 (aprobado): las 4 operaciones de 2.5 revalidan estado bajo
    `select_for_update()` — mismo patrón que `ConcurrenciaTomarTicketTests`
    (2.3) y `ConcurrenciaResponderSolicitudTests` (2.4), con hilos reales
    contra Postgres."""

    def setUp(self):
        self._preparar_escenario()

    def test_dos_resoluciones_concurrentes_solo_una_gana(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resultados = {}
        barrera = threading.Barrier(2)

        def _intentar_resolver(clave, descripcion):
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                resolver_ticket(ticket, self.responsable_directo, descripcion)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_resuelto"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_intentar_resolver, args=("a", "Resolución A"))
        hilo_b = threading.Thread(target=_intentar_resolver, args=("b", "Resolución B"))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_resuelto"), 1)
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.RESUELTO)
        self.assertEqual(ResolucionTicket.objects.filter(ticket=ticket).count(), 1)
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=ticket, tipo_evento=HistorialTicket.TipoEvento.RESUELTO
            ).count(),
            1,
        )
        self.assertEqual(_auditorias_de_ticket(ticket).filter(datos_nuevos={"estado": "RESUELTO"}).count(), 1)

    def test_cerrar_mientras_otro_reabre_solo_uno_gana(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        resultados = {}
        barrera = threading.Barrier(2)

        def _cerrar():
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                cerrar_ticket(ticket, self.solicitante)
                resultados["cerrar"] = "ok"
            except ValidationError:
                resultados["cerrar"] = "fallo"
            finally:
                connection.close()

        def _reabrir():
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                reabrir_ticket(ticket, self.responsable_directo, "Reapertura.")
                resultados["reabrir"] = "ok"
            except ValidationError:
                resultados["reabrir"] = "fallo"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_cerrar)
        hilo_b = threading.Thread(target=_reabrir)
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("fallo"), 1)
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        self.assertIn(ticket.estado, [Ticket.Estado.CERRADO, Ticket.Estado.EN_ATENCION])

    def test_cancelar_mientras_otro_toma_solo_uno_gana(self):
        """CANCELAR admite tanto RADICADO como EN_ATENCION (a diferencia de
        TOMAR, que exige exclusivamente RADICADO) — así que, a diferencia
        de las otras 3 carreras de esta clase, aquí "solo uno gana" no
        aplica simétricamente: si TOMAR corre y comete primero, CANCELAR
        igual procede después (el ticket sigue en un estado que cancelar
        acepta, EN_ATENCION). El invariante real bajo `select_for_update()`
        es que CANCELAR siempre termina ganando — corra antes o después de
        TOMAR, siempre encuentra un estado que le permite ejecutarse —
        mientras que TOMAR solo tiene éxito si comete antes que CANCELAR."""
        resultados = {}
        barrera = threading.Barrier(2)

        def _cancelar():
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                cancelar_ticket(ticket, self.solicitante, "Motivo.")
                resultados["cancelar"] = "ok"
            except (PermissionDenied, ValidationError):
                resultados["cancelar"] = "fallo"
            finally:
                connection.close()

        def _tomar():
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                tomar_ticket(ticket, self.responsable_directo)
                resultados["tomar"] = "ok"
            except (PermissionDenied, ValidationError):
                resultados["tomar"] = "fallo"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_cancelar)
        hilo_b = threading.Thread(target=_tomar)
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        self.assertEqual(resultados["cancelar"], "ok")
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.CANCELADO)
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=ticket, tipo_evento=HistorialTicket.TipoEvento.CANCELADO
            ).count(),
            1,
        )

    def test_reabrir_dos_veces_concurrente_solo_una_gana(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        resultados = {}
        barrera = threading.Barrier(2)

        def _reabrir(clave):
            barrera.wait()
            try:
                ticket = Ticket.objects.get(pk=self.ticket.pk)
                reabrir_ticket(ticket, self.responsable_directo, f"Motivo {clave}.")
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_reabierto"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_reabrir, args=("a",))
        hilo_b = threading.Thread(target=_reabrir, args=("b",))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_reabierto"), 1)
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual(
            HistorialTicket.objects.filter(
                ticket=ticket, tipo_evento=HistorialTicket.TipoEvento.REABIERTO
            ).count(),
            1,
        )


class FinalizacionViewsTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_resolver_view_exitoso(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:resolver", args=[self.ticket.pk]), {"descripcion": "Resuelto vía vista."}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RESUELTO)

    def test_resolver_view_rechaza_get(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:resolver", args=[self.ticket.pk]))
        self.assertEqual(respuesta.status_code, 405)

    def test_resolver_view_sin_autorizacion_no_cambia_estado(self):
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:resolver", args=[self.ticket.pk]), {"descripcion": "Intento no autorizado."}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_cerrar_view_exitoso(self):
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tickets:cerrar", args=[self.ticket.pk]))
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CERRADO)

    def test_cancelar_view_exitoso(self):
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:cancelar", args=[self.ticket.pk]), {"motivo": "Ya no se necesita."}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.CANCELADO)

    def test_reabrir_view_exitoso(self):
        resolver_ticket(self.ticket, self.responsable_directo, "Resuelto.")
        self.ticket.refresh_from_db()
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("tickets:reabrir", args=[self.ticket.pk]), {"motivo": "Faltó un detalle."}
        )
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.EN_ATENCION)

    def test_detalle_view_expone_contexto_de_finalizacion(self):
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertTrue(respuesta.context["puede_resolver"])
        self.assertFalse(respuesta.context["puede_cerrar"])
        self.assertIn("resolucion_actual", respuesta.context)
        self.assertIn("resoluciones_anteriores", respuesta.context)

    def test_detalle_view_expone_resolucion_actual_y_anteriores(self):
        resolver_ticket(self.ticket, self.responsable_directo, "Primera resolución.")
        self.ticket.refresh_from_db()
        reabrir_ticket(self.ticket, self.responsable_directo, "Falta un detalle.")
        self.ticket.refresh_from_db()
        resolver_ticket(self.ticket, self.responsable_directo, "Segunda resolución.")
        self.ticket.refresh_from_db()

        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertEqual(respuesta.context["resolucion_actual"].descripcion, "Segunda resolución.")
        anteriores = respuesta.context["resoluciones_anteriores"]
        self.assertEqual(len(anteriores), 1)
        self.assertEqual(anteriores[0].descripcion, "Primera resolución.")


# ---------------------------------------------------------------------------
# 2.C — Cierre técnico Sprint 2: RQF-119 (auditoría de responsable/
# asignación, deuda cerrada — TOMAR/ASIGNAR/REASIGNAR de 2.3 ahora también
# alimentan RegistroAuditoria, no solo HistorialTicket).
# ---------------------------------------------------------------------------


class AuditoriaAsignacionTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def test_tomar_genera_exactamente_una_auditoria_de_asignacion(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        auditorias = _auditorias_de_ticket(self.ticket).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual(auditorias.count(), 1)
        registro = auditorias.get()
        self.assertEqual(registro.usuario_id, self.responsable_directo.id)
        self.assertEqual(registro.origen, RegistroAuditoria.Origen.USUARIO)
        self.assertEqual(
            registro.datos_anteriores, {"usuario_responsable_id": None, "equipo_responsable_id": None}
        )
        self.assertEqual(
            registro.datos_nuevos,
            {"usuario_responsable_id": self.responsable_directo.id, "equipo_responsable_id": None},
        )

    def test_asignar_genera_exactamente_una_auditoria(self):
        asignar_ticket(self.ticket, self.gestor_area, usuario=self.responsable_directo)
        auditorias = _auditorias_de_ticket(self.ticket).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual(auditorias.count(), 1)
        registro = auditorias.get()
        self.assertEqual(registro.usuario_id, self.gestor_area.id)
        self.assertEqual(
            registro.datos_nuevos,
            {"usuario_responsable_id": self.responsable_directo.id, "equipo_responsable_id": None},
        )

    def test_asignar_solo_equipo_tambien_audita(self):
        asignar_ticket(self.ticket, self.gestor_area, equipo=self.equipo)
        auditorias = _auditorias_de_ticket(self.ticket).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual(auditorias.count(), 1)
        registro = auditorias.get()
        self.assertEqual(
            registro.datos_nuevos,
            {"usuario_responsable_id": None, "equipo_responsable_id": self.equipo.id},
        )

    def test_reasignar_genera_exactamente_una_auditoria_con_datos_correctos(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        reasignar_ticket(self.ticket, self.gestor_area, usuario=self.miembro_equipo)

        auditorias = _auditorias_de_ticket(self.ticket).filter(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, usuario=self.gestor_area
        )
        self.assertEqual(auditorias.count(), 1)
        registro = auditorias.get()
        self.assertEqual(registro.datos_anteriores["usuario_responsable_id"], self.responsable_directo.id)
        self.assertEqual(registro.datos_nuevos["usuario_responsable_id"], self.miembro_equipo.id)

    def test_doble_intento_fallido_de_tomar_no_genera_auditoria_adicional(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            tomar_ticket(self.ticket, self.miembro_equipo)
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).count(), 1
        )

    def test_no_audita_comentarios_ni_adjuntos(self):
        # Punto 5 (aprobado): no se audita comunicación por este cambio —
        # solo TOMAR/ASIGNAR/REASIGNAR generan RegistroAuditoria de Ticket.
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        comentar_ticket(self.ticket, self.responsable_directo, "Un comentario cualquiera.")
        self.assertEqual(
            _auditorias_de_ticket(self.ticket).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).count(), 1
        )


class RadicacionProcesoTests(TestCase):
    """4.1: catálogo común, entrada congelada y una ejecución por radicación."""

    def setUp(self):
        from apps.workflows.models import ConfiguracionEtapaTarea, Etapa, TransicionEtapa, Workflow, WorkflowVersion
        from apps.workflows.versionamiento import activar_version as activar_workflow

        self.usuario = Usuario.objects.create_user("proceso41")
        self.servicio, self.formulario_version, _ = _crear_servicio_con_formulario(self.usuario, [])
        self.workflow = Workflow.objects.create(nombre="Ejecución compartida")
        self.version = WorkflowVersion.objects.create(workflow=self.workflow, numero=1)
        inicio = Etapa.objects.create(version=self.version, tipo="INICIO", nombre="Inicio")
        tarea = Etapa.objects.create(version=self.version, tipo="TAREA", nombre="Trabajar")
        fin = Etapa.objects.create(version=self.version, tipo="FIN", nombre="Fin")
        ConfiguracionEtapaTarea.objects.create(etapa=tarea)
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        activar_workflow(self.workflow, self.version, self.usuario)
        self.servicio.tipo = "PROCESO"
        self.servicio.workflow = self.workflow
        self.servicio.save()

    def test_proceso_radica_instancia_sin_crear_definiciones(self):
        from apps.workflows.models import InstanciaWorkflow, Workflow, WorkflowVersion

        definiciones = Workflow.objects.count(), WorkflowVersion.objects.count()
        ticket = crear_borrador(self.usuario, self.servicio)
        self.assertEqual(ticket.tipo, "PROCESO")
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, self.formulario_version.pk)
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.estado, "RADICADO")
        self.assertEqual(ticket.instancia_workflow.workflow_version_id, self.version.pk)
        self.assertEqual(ticket.instancia_workflow.ticket, ticket)
        self.assertEqual(ticket.instancia_workflow.estado, "EN_ESPERA")
        self.assertEqual((Workflow.objects.count(), WorkflowVersion.objects.count()), definiciones)
        self.assertEqual(InstanciaWorkflow.objects.count(), 1)
        evento = RegistroAuditoria.objects.get(modelo="workflows.instanciaworkflow", object_id=ticket.instancia_workflow_id)
        self.assertEqual(evento.origen, "SISTEMA")
        self.assertIsNone(evento.usuario_id)
        self.assertEqual(ticket.historial.get(tipo_evento="RADICADO").actor, self.usuario)

    def test_servicio_con_workflow_inicia_instancia_al_radicar(self):
        from apps.workflows.models import InstanciaWorkflow

        self.servicio.tipo = "SERVICIO"
        self.servicio.save()
        ticket = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.tipo, "SERVICIO")
        self.assertEqual(ticket.estado, "RADICADO")
        self.assertIsNotNone(ticket.instancia_workflow_id)
        self.assertEqual(ticket.instancia_workflow.workflow_version_id, self.version.pk)
        self.assertEqual(InstanciaWorkflow.objects.count(), 1)

    def test_tipo_se_resuelve_de_nuevo_al_radicar(self):
        self.servicio.tipo = "SERVICIO"
        self.servicio.save()
        ticket = crear_borrador(self.usuario, self.servicio)
        self.servicio.tipo = "PROCESO"
        self.servicio.save()
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.tipo, "PROCESO")
        self.assertIsNotNone(ticket.instancia_workflow_id)

    def test_formulario_congelado_y_workflow_vigente_al_radicar(self):
        from apps.workflows.versionamiento import activar_version as activar_workflow, crear_nueva_version as clonar_workflow

        ticket = crear_borrador(self.usuario, self.servicio)
        nueva_entrada = crear_nueva_version(self.servicio.formulario, self.usuario)
        activar_version(self.servicio.formulario, nueva_entrada, self.usuario)
        nueva_ejecucion = clonar_workflow(self.workflow, self.usuario)
        activar_workflow(self.workflow, nueva_ejecucion, self.usuario)
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, self.formulario_version.pk)
        self.assertEqual(ticket.respuesta_formulario.formulario_version_id, self.formulario_version.pk)
        self.assertEqual(ticket.instancia_workflow.workflow_version_id, nueva_ejecucion.pk)
        siguiente = clonar_workflow(self.workflow, self.usuario)
        activar_workflow(self.workflow, siguiente, self.usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.instancia_workflow.workflow_version_id, nueva_ejecucion.pk)
        otro = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(otro, self.usuario)
        self.assertEqual(otro.instancia_workflow.workflow_version_id, siguiente.pk)
        self.assertNotEqual(otro.instancia_workflow_id, ticket.instancia_workflow_id)
        self.assertEqual(otro.detalle_servicio.formulario_version_id, nueva_entrada.pk)

    def test_no_revalida_formulario_actual_despues_de_congelar(self):
        ticket = crear_borrador(self.usuario, self.servicio)
        self.servicio.formulario = None
        self.servicio.save()
        radicar_ticket(ticket, self.usuario)
        self.assertEqual(ticket.estado, "RADICADO")
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, self.formulario_version.pk)

    def test_no_radica_proceso_sin_workflow_o_sin_version_activa(self):
        from apps.workflows.models import InstanciaWorkflow

        ticket = crear_borrador(self.usuario, self.servicio)
        eventos = RegistroAuditoria.objects.count()
        self.servicio.workflow = None
        self.servicio.save()
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario)
        self.servicio.workflow = self.workflow
        self.servicio.save()
        self.workflow.version_activa = None
        self.workflow.save()
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, "BORRADOR")
        self.assertIsNone(ticket.radicado)
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertFalse(InstanciaWorkflow.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_actor_ajeno_no_inicia_instancia(self):
        from apps.workflows.models import InstanciaWorkflow

        ticket = crear_borrador(self.usuario, self.servicio)
        otro = Usuario.objects.create_user("ajeno41")
        with self.assertRaises(PermissionDenied):
            radicar_ticket(ticket, otro)
        self.assertFalse(InstanciaWorkflow.objects.exists())

    def test_doble_radicacion_con_objeto_obsoleto_no_duplica_instancia(self):
        from apps.workflows.models import InstanciaWorkflow

        ticket = crear_borrador(self.usuario, self.servicio)
        obsoleto = Ticket.objects.get(pk=ticket.pk)
        radicar_ticket(ticket, self.usuario)
        with self.assertRaises(ValidationError):
            radicar_ticket(obsoleto, self.usuario)
        self.assertEqual(InstanciaWorkflow.objects.count(), 1)

    def test_rollback_ante_excepcion_o_resultado_error_del_motor(self):
        from unittest.mock import patch
        from apps.tareas.models import Tarea
        from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow
        from apps.workflows.motor import iniciar_workflow

        ticket = crear_borrador(self.usuario, self.servicio)
        eventos = RegistroAuditoria.objects.count()

        def iniciar_y_fallar(*args, **kwargs):
            iniciar_workflow(*args, **kwargs)
            raise RuntimeError("Fallo después de crear instancia y tarea")

        def iniciar_con_error(*args, **kwargs):
            instancia = iniciar_workflow(*args, **kwargs)
            instancia.estado = "ERROR"
            instancia.save()
            return instancia

        for efecto, error in ((iniciar_y_fallar, RuntimeError), (iniciar_con_error, ValidationError)):
            with self.subTest(error=error):
                with patch("apps.tickets.operaciones.iniciar_workflow", side_effect=efecto):
                    with self.assertRaises(error):
                        radicar_ticket(ticket, self.usuario)
                ticket.refresh_from_db()
                self.assertEqual(ticket.estado, "BORRADOR")
                self.assertIsNone(ticket.radicado)
                self.assertIsNone(ticket.radicado_en)
                self.assertIsNone(ticket.instancia_workflow_id)
                self.assertFalse(ticket.historial.exists())
                self.assertFalse(InstanciaWorkflow.objects.exists())
                self.assertFalse(InstanciaEtapa.objects.exists())
                self.assertFalse(Tarea.objects.exists())
                self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_fallo_posterior_al_inicio_revierte_radicacion_completa(self):
        from unittest.mock import patch
        from apps.workflows.models import InstanciaWorkflow

        area = Area.objects.create(nombre="Mercadeo", codigo="MER41")
        ServicioContextoAtencion.objects.create(servicio=self.servicio, tipo_alcance="AREA", area=area)
        ticket = crear_borrador(self.usuario, self.servicio)
        eventos = RegistroAuditoria.objects.count()
        with patch("apps.tickets.operaciones.historial.registrar", side_effect=RuntimeError("Fallo historial")):
            with self.assertRaises(RuntimeError):
                radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, "BORRADOR")
        self.assertIsNone(ticket.radicado)
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertFalse(ticket.contextos_atencion.exists())
        self.assertFalse(InstanciaWorkflow.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_no_se_puede_quitar_instancia_ni_borrarla(self):
        from django.db.models.deletion import ProtectedError

        ticket = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(ticket, self.usuario)
        instancia = ticket.instancia_workflow
        ticket.instancia_workflow = None
        with self.assertRaises(ValidationError):
            ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.instancia_workflow_id, instancia.pk)
        with self.assertRaises(ProtectedError):
            instancia.delete()


class _EscenarioEntregablesMixin:
    def _preparar_entregables(self):
        from apps.catalogo.models import DefinicionEntregable

        self.solicitante42 = Usuario.objects.create_user("solicitante42")
        self.responsable42 = Usuario.objects.create_user("responsable42")
        self.nuevo42 = Usuario.objects.create_user("nuevo42")
        self.miembro42 = Usuario.objects.create_user("miembro42")
        self.ajeno42 = Usuario.objects.create_user("ajeno42")
        self.equipo42 = Equipo.objects.create(nombre="Equipo entregables")
        for usuario in (self.responsable42, self.nuevo42, self.miembro42):
            MiembroEquipo.objects.create(equipo=self.equipo42, usuario=usuario)
            _otorgar_tickets_atender(usuario)
        self.servicio42, self.version42, _ = _crear_servicio_con_formulario(self.solicitante42, [])
        self.definiciones42 = {}
        for orden, tipo in enumerate(DefinicionEntregable.Tipo.values):
            self.definiciones42[tipo] = DefinicionEntregable.objects.create(
                servicio=self.servicio42, nombre=f"Salida {tipo}", tipo=tipo,
                descripcion=f"Instrucciones {tipo}", obligatorio=True, orden=orden,
            )
        self.ticket42 = crear_borrador(self.solicitante42, self.servicio42)
        radicar_ticket(self.ticket42, self.solicitante42)
        self.ticket42 = asignar_ticket(self.ticket42, self.responsable42, equipo=self.equipo42)
        self.ticket42 = tomar_ticket(self.ticket42, self.responsable42)

    def _entregable(self, tipo):
        return self.ticket42.entregables.get(tipo=tipo)


class EntregablesTicketTests(_MediaAisladaMixin, _EscenarioEntregablesMixin, TestCase):
    def setUp(self):
        self._preparar_entregables()

    def test_snapshot_y_materializacion_idempotente(self):
        from apps.tickets.entregables import materializar_entregables

        antes = list(self.ticket42.entregables.values())
        eventos = RegistroAuditoria.objects.count()
        materializar_entregables(self.ticket42)
        self.assertEqual(list(self.ticket42.entregables.values()), antes)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        for entregable in self.ticket42.entregables.all():
            definicion = self.definiciones42[entregable.tipo]
            for campo in ("nombre", "descripcion", "tipo", "obligatorio", "orden"):
                self.assertEqual(getattr(entregable, campo), getattr(definicion, campo))
        eventos_materializacion = RegistroAuditoria.objects.filter(modelo="tickets.ticket", object_id=self.ticket42.pk, datos_nuevos__has_key="entregables")
        self.assertEqual(eventos_materializacion.count(), 1)
        self.assertEqual(len(eventos_materializacion.get().datos_nuevos["entregables"]), 4)

    def test_modificar_retirar_y_agregar_definiciones_no_cambia_ticket_anterior(self):
        from apps.catalogo.models import DefinicionEntregable
        from apps.tickets.entregables import materializar_entregables

        antes = list(self.ticket42.entregables.values())
        definicion = self.definiciones42["TEXTO"]
        definicion.nombre = "Otra expectativa"
        definicion.tipo = "ENLACE"
        definicion.obligatorio = False
        definicion.orden = 20
        definicion.descripcion = "Otras instrucciones"
        definicion.save()
        self.definiciones42["ARCHIVO"].activo = False
        self.definiciones42["ARCHIVO"].save()
        nueva = DefinicionEntregable.objects.create(servicio=self.servicio42, nombre="Nueva", tipo="TEXTO")
        materializar_entregables(self.ticket42)
        self.assertEqual(list(self.ticket42.entregables.values()), antes)
        nuevo_ticket = crear_borrador(self.solicitante42, self.servicio42)
        actualizado = nuevo_ticket.entregables.get(definicion=definicion)
        self.assertEqual(actualizado.nombre, "Otra expectativa")
        self.assertEqual(actualizado.tipo, "ENLACE")
        self.assertFalse(actualizado.obligatorio)
        self.assertEqual(actualizado.orden, 20)
        self.assertFalse(nuevo_ticket.entregables.filter(definicion=self.definiciones42["ARCHIVO"]).exists())
        self.assertTrue(nuevo_ticket.entregables.filter(definicion=nueva).exists())

    def test_cero_expectativas_e_historicos_no_se_rellenan(self):
        from apps.catalogo.models import DefinicionEntregable
        from apps.tickets.entregables import materializar_entregables

        servicio, version, _ = _crear_servicio_con_formulario(self.solicitante42, [])
        vacio = crear_borrador(self.solicitante42, servicio)
        historico = Ticket.objects.create(solicitante=self.solicitante42)
        TicketServicio.objects.create(ticket=historico, servicio=servicio, formulario_version=version)
        DefinicionEntregable.objects.create(servicio=servicio, nombre="Posterior", tipo="TEXTO")
        self.assertEqual(materializar_entregables(vacio), [])
        self.assertEqual(materializar_entregables(historico), [])
        radicar_ticket(vacio, self.solicitante42)
        self.assertFalse(vacio.entregables.exists())
        self.assertEqual(crear_borrador(self.solicitante42, servicio).entregables.count(), 1)

    def test_snapshot_inmutable_y_definicion_utilizada_protegida(self):
        from django.db.models.deletion import ProtectedError

        entregable = self._entregable("TEXTO")
        entregable.nombre = "No permitido"
        with self.assertRaises(ValidationError):
            entregable.save()
        with self.assertRaises(ProtectedError):
            self.definiciones42["TEXTO"].delete()
        entregable.refresh_from_db()
        self.assertEqual(entregable.nombre, "Salida TEXTO")

    def test_texto_y_enlace_validacion_y_satisfaccion(self):
        from apps.tickets.entregables import registrar_resultado_entregable

        texto = self._entregable("TEXTO")
        enlace = self._entregable("ENLACE")
        self.assertFalse(texto.satisfecho)
        self.assertFalse(enlace.satisfecho)
        texto = registrar_resultado_entregable(texto, self.responsable42, "  ")
        self.assertFalse(texto.satisfecho)
        texto = registrar_resultado_entregable(texto, self.responsable42, " Listo para recoger ")
        self.assertTrue(texto.satisfecho)
        self.assertEqual(texto.texto, "Listo para recoger")
        for valor in ("no es enlace", "javascript:alert(1)"):
            with self.assertRaises(ValidationError):
                registrar_resultado_entregable(enlace, self.responsable42, valor)
        enlace = registrar_resultado_entregable(enlace, self.responsable42, "https://intranet.example.com/privado")
        self.assertTrue(enlace.satisfecho)
        enlace = registrar_resultado_entregable(enlace, self.responsable42, "")
        self.assertFalse(enlace.satisfecho)

    def test_confirmacion_explicita_con_actor_fecha_y_evento_unico(self):
        from apps.tickets.entregables import confirmar_entregable

        entregable = self._entregable("CONFIRMACION")
        self.assertFalse(entregable.satisfecho)
        eventos = RegistroAuditoria.objects.count()
        confirmado = confirmar_entregable(entregable, self.responsable42)
        self.assertTrue(confirmado.satisfecho)
        self.assertEqual(confirmado.confirmado_por, self.responsable42)
        self.assertIsNotNone(confirmado.confirmado_en)
        confirmar_entregable(entregable, self.responsable42)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 1)

    def test_responsable_individual_permitido_equipo_y_solicitante_rechazados(self):
        from apps.tickets.autorizacion import puede_escribir_entregables_finales
        from apps.tickets.entregables import registrar_resultado_entregable

        entregable = self._entregable("TEXTO")
        self.assertTrue(puede_escribir_entregables_finales(self.responsable42, self.ticket42))
        # Se mantiene la semántica anterior del helper general.
        self.assertTrue(es_responsable_actual(self.miembro42, self.ticket42))
        eventos = RegistroAuditoria.objects.count()
        for usuario in (self.miembro42, self.solicitante42, self.ajeno42):
            with self.subTest(usuario=usuario.pk):
                self.assertFalse(puede_escribir_entregables_finales(usuario, self.ticket42))
                with self.assertRaises(PermissionDenied):
                    registrar_resultado_entregable(entregable, usuario, "Intruso")
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)
        entregable.refresh_from_db()
        self.assertFalse(entregable.satisfecho)
        self.assertTrue(registrar_resultado_entregable(entregable, self.responsable42, "Resultado").satisfecho)

    def test_equipo_sin_usuario_individual_no_puede_escribir(self):
        from apps.tickets.entregables import registrar_resultado_entregable

        # Fixture: ticket con equipo y sin responsable individual.
        self.ticket42.usuario_responsable = None
        self.ticket42.save()
        for usuario in (self.miembro42, self.responsable42):
            with self.assertRaises(PermissionDenied):
                registrar_resultado_entregable(self._entregable("TEXTO"), usuario, "No permitido")

    def test_reasignacion_revoca_anterior_incluso_si_sigue_en_equipo(self):
        from apps.tickets.entregables import registrar_resultado_entregable

        entregable = self._entregable("TEXTO")  # Conserva referencias previas.
        registrar_resultado_entregable(entregable, self.responsable42, "Primero")
        reasignar_ticket(self.ticket42, self.responsable42, usuario=self.nuevo42)
        with self.assertRaises(PermissionDenied):
            registrar_resultado_entregable(entregable, self.responsable42, "Ya no puede")
        self.assertTrue(MiembroEquipo.objects.filter(equipo=self.equipo42, usuario=self.responsable42, activo=True).exists())
        resultado = registrar_resultado_entregable(entregable, self.nuevo42, "Continuación")
        self.assertEqual(resultado.texto, "Continuación")
        MiembroEquipo.objects.filter(equipo=self.equipo42, usuario=self.responsable42).update(activo=False)
        with self.assertRaises(PermissionDenied):
            registrar_resultado_entregable(entregable, self.responsable42, "Tampoco puede")

    def test_todas_las_mutaciones_exigen_responsable_y_tipo_correcto(self):
        from apps.tickets.entregables import adjuntar_archivo_entregable, confirmar_entregable, registrar_resultado_entregable, retirar_archivo_entregable

        archivo = self._entregable("ARCHIVO")
        adjunto = adjuntar_archivo_entregable(archivo, self.responsable42, SimpleUploadedFile("uno.txt", b"uno"))
        with self.assertRaises(PermissionDenied):
            confirmar_entregable(self._entregable("CONFIRMACION"), self.solicitante42)
        with self.assertRaises(PermissionDenied):
            adjuntar_archivo_entregable(archivo, self.miembro42, SimpleUploadedFile("dos.txt", b"dos"))
        with self.assertRaises(PermissionDenied):
            retirar_archivo_entregable(adjunto, self.solicitante42)
        with self.assertRaises(ValidationError):
            confirmar_entregable(self._entregable("TEXTO"), self.responsable42)
        with self.assertRaises(ValidationError):
            registrar_resultado_entregable(archivo, self.responsable42, "No es archivo")
        with self.assertRaises(ValidationError):
            adjuntar_archivo_entregable(self._entregable("TEXTO"), self.responsable42, SimpleUploadedFile("t.txt", b"t"))

    def test_archivos_multiples_retiro_y_descarga_protegida(self):
        from apps.tickets.entregables import adjuntar_archivo_entregable, retirar_archivo_entregable

        entregable = self._entregable("ARCHIVO")
        self.assertFalse(entregable.satisfecho)
        with self.assertRaises(ValidationError):
            adjuntar_archivo_entregable(entregable, self.responsable42, SimpleUploadedFile("vacio.txt", b""))
        archivos = [adjuntar_archivo_entregable(entregable, self.responsable42, SimpleUploadedFile(f"{i}.txt", b"contenido")) for i in range(2)]
        self.assertEqual(entregable.archivos.count(), 2)
        self.assertTrue(entregable.satisfecho)
        self.assertFalse(self._entregable("TEXTO").archivos.exists())
        for archivo in archivos:
            self.assertEqual(archivo.entregable_id, entregable.pk)
            self.assertEqual(archivo.ticket_relacionado, self.ticket42)
            url = reverse("tickets:descargar_adjunto", args=[archivo.pk])
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.force_login(self.ajeno42)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.client.force_login(self.solicitante42)
            respuesta = self.client.get(url)
            self.assertEqual(respuesta.status_code, 200)
            # El cliente de tests cierra la respuesta al agotar el streaming.
            # Un segundo close() emitiría request_finished fuera de su protección.
            self.assertEqual(b"".join(respuesta.streaming_content), b"contenido")
            self.assertTrue(respuesta.closed)
            self.client.logout()
        retirar_archivo_entregable(archivos[0], self.responsable42)
        self.assertTrue(entregable.satisfecho)
        retirar_archivo_entregable(archivos[1], self.responsable42)
        self.assertFalse(entregable.satisfecho)
        self.client.force_login(self.solicitante42)
        self.assertEqual(self.client.get(reverse("tickets:descargar_adjunto", args=[archivos[0].pk])).status_code, 404)
        self.assertTrue(archivos[0].archivo.storage.exists(archivos[0].archivo.name))

    def test_consulta_pendientes_y_completar_no_transiciona_ticket(self):
        from apps.catalogo.models import DefinicionEntregable
        from apps.tickets.entregables import adjuntar_archivo_entregable, confirmar_entregable, entregables_obligatorios_pendientes, entregables_para_ticket, registrar_resultado_entregable

        DefinicionEntregable.objects.create(servicio=self.servicio42, nombre="Opcional", tipo="TEXTO", obligatorio=False)
        otro = crear_borrador(self.solicitante42, self.servicio42)
        self.assertEqual(len(entregables_obligatorios_pendientes(otro, self.solicitante42)), 4)
        self.assertEqual(entregables_para_ticket(otro, self.solicitante42).count(), 5)
        with self.assertRaises(PermissionDenied):
            entregables_para_ticket(otro, self.ajeno42)
        registrar_resultado_entregable(self._entregable("TEXTO"), self.responsable42, "Listo")
        registrar_resultado_entregable(self._entregable("ENLACE"), self.responsable42, "https://example.com/final")
        adjuntar_archivo_entregable(self._entregable("ARCHIVO"), self.responsable42, SimpleUploadedFile("final.txt", b"final"))
        confirmar_entregable(self._entregable("CONFIRMACION"), self.responsable42)
        self.assertEqual(entregables_obligatorios_pendientes(self.ticket42, self.solicitante42), [])
        self.ticket42.refresh_from_db()
        self.assertEqual(self.ticket42.estado, "EN_ATENCION")

    def test_resultados_y_archivos_auditados_y_rollback_sin_efectos_parciales(self):
        from unittest.mock import patch
        from apps.tickets.entregables import adjuntar_archivo_entregable, registrar_resultado_entregable, retirar_archivo_entregable

        texto = self._entregable("TEXTO")
        eventos = RegistroAuditoria.objects.count()
        registrar_resultado_entregable(texto, self.responsable42, "Antes")
        registrar_resultado_entregable(texto, self.responsable42, "Después")
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 2)
        evento = RegistroAuditoria.objects.filter(modelo="tickets.entregableticket", object_id=texto.pk).latest("pk")
        self.assertEqual(evento.datos_anteriores, {"texto": "Antes"})
        self.assertEqual(evento.datos_nuevos, {"texto": "Después"})
        archivo = adjuntar_archivo_entregable(self._entregable("ARCHIVO"), self.responsable42, SimpleUploadedFile("a.txt", b"a"))
        eventos = RegistroAuditoria.objects.count()
        with patch("apps.tickets.entregables.registrar_evento", side_effect=RuntimeError("Auditoría")):
            with self.assertRaises(RuntimeError):
                registrar_resultado_entregable(texto, self.responsable42, "Fallido")
            with self.assertRaises(RuntimeError):
                retirar_archivo_entregable(archivo, self.responsable42)
        texto.refresh_from_db()
        archivo.refresh_from_db()
        self.assertEqual(texto.texto, "Después")
        self.assertIsNone(archivo.retirado_en)
        self.assertTrue(archivo.archivo.storage.exists(archivo.archivo.name))
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_ticket_resuelto_no_admite_escritura(self):
        from apps.tickets.entregables import registrar_resultado_entregable

        resolver_ticket(self.ticket42, self.responsable42, "Resolución explícita")
        with self.assertRaises(PermissionDenied):
            registrar_resultado_entregable(self._entregable("TEXTO"), self.responsable42, "Tardío")

    def test_fallo_materializacion_revierte_creacion_borrador(self):
        from unittest.mock import patch

        tickets = Ticket.objects.count()
        eventos = RegistroAuditoria.objects.count()
        with patch("apps.tickets.entregables.registrar_evento", side_effect=RuntimeError("Auditoría")):
            with self.assertRaises(RuntimeError):
                crear_borrador(self.solicitante42, self.servicio42)
        self.assertEqual(Ticket.objects.count(), tickets)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)


    def test_proceso_tambien_congela_entregables_al_crear_borrador(self):
        self.servicio42.tipo = "PROCESO"
        self.servicio42.save()
        ticket = crear_borrador(self.solicitante42, self.servicio42)
        self.assertEqual(ticket.tipo, "PROCESO")
        self.assertEqual(ticket.entregables.count(), 4)
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, self.version42.pk)

    def test_no_puede_reabrirse_la_materializacion(self):
        self.ticket42.entregables_materializados = False
        with self.assertRaises(ValidationError):
            self.ticket42.save()
        self.ticket42.refresh_from_db()
        self.assertTrue(self.ticket42.entregables_materializados)



class ConcurrenciaEntregablesTests(_EscenarioEntregablesMixin, TransactionTestCase):
    def setUp(self):
        self._preparar_entregables()

    def test_escritura_espera_reasignacion_y_revalida_responsable(self):
        from apps.tickets.entregables import registrar_resultado_entregable

        intentando_lock = threading.Event()
        resultado = []
        entregable = self._entregable("TEXTO")

        def observar_lock(execute, sql, params, many, context):
            if '"tickets_ticket"' in sql and "FOR UPDATE" in sql:
                intentando_lock.set()
            return execute(sql, params, many, context)

        def escribir_como_anterior():
            try:
                with connection.execute_wrapper(observar_lock):
                    registrar_resultado_entregable(entregable, self.responsable42, "No debe guardarse")
                resultado.append("permitido")
            except PermissionDenied:
                resultado.append("rechazado")
            except Exception as exc:
                resultado.append(exc)
            finally:
                connection.close()

        hilo = threading.Thread(target=escribir_como_anterior, daemon=True)
        try:
            with transaction.atomic():
                Ticket.objects.select_for_update().get(pk=self.ticket42.pk)
                hilo.start()
                self.assertTrue(intentando_lock.wait(timeout=10), "La escritura no llegó al lock del Ticket")
                reasignar_ticket(self.ticket42, self.responsable42, usuario=self.nuevo42)
        finally:
            hilo.join(timeout=15)
        self.assertFalse(hilo.is_alive(), "La escritura no terminó tras liberar el Ticket")
        self.assertEqual(resultado, ["rechazado"])
        entregable.refresh_from_db()
        self.assertEqual(entregable.texto, "")
        actualizado = registrar_resultado_entregable(entregable, self.nuevo42, "Nuevo responsable")
        self.assertEqual(actualizado.texto, "Nuevo responsable")

    def test_materializacion_concurrente_no_duplica_expectativas_ni_auditoria(self):
        from apps.tickets.entregables import materializar_entregables

        # Punto intermedio de crear_borrador, antes de su materialización.
        ticket = Ticket.objects.create(solicitante=self.solicitante42, entregables_materializados=False)
        TicketServicio.objects.create(ticket=ticket, servicio=self.servicio42, formulario_version=self.version42)
        barrera = threading.Barrier(2)
        resultados = []

        def materializar():
            try:
                barrera.wait(timeout=10)
                resultados.append([e.pk for e in materializar_entregables(ticket)])
            except Exception as exc:
                resultados.append(exc)
            finally:
                connection.close()

        hilos = [threading.Thread(target=materializar, daemon=True) for _ in range(2)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(timeout=15)
            self.assertFalse(hilo.is_alive())
        self.assertEqual(len(resultados), 2)
        self.assertTrue(all(isinstance(r, list) for r in resultados), resultados)
        self.assertEqual(resultados[0], resultados[1])
        self.assertEqual(ticket.entregables.count(), 4)
        self.assertEqual(RegistroAuditoria.objects.filter(modelo="tickets.ticket", object_id=ticket.pk).count(), 1)


class RadicacionEjecucionConfigurableTests(TestCase):
    """4.3: la configuración, no la clasificación, determina el uso del motor."""

    def setUp(self):
        RadicacionProcesoTests.setUp(self)
        self.servicio.tipo = "SERVICIO"
        self.servicio.save()

    def test_servicio_simple_y_ticket_historico_no_adquieren_instancia(self):
        from apps.workflows.models import InstanciaWorkflow

        self.servicio.workflow = None
        self.servicio.save()
        anterior = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(anterior, self.usuario)
        self.assertIsNone(anterior.instancia_workflow_id)
        self.servicio.workflow = self.workflow
        self.servicio.save()
        anterior.refresh_from_db()
        self.assertIsNone(anterior.instancia_workflow_id)
        with self.assertRaises(ValidationError):
            radicar_ticket(anterior, self.usuario)
        nuevo = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(nuevo, self.usuario)
        self.assertIsNotNone(nuevo.instancia_workflow_id)
        self.assertEqual(InstanciaWorkflow.objects.count(), 1)

    def test_servicio_asociado_sin_activa_o_con_estructura_invalida_no_radica(self):
        from apps.workflows.models import InstanciaWorkflow, WorkflowVersion

        ticket = crear_borrador(self.usuario, self.servicio)
        eventos = RegistroAuditoria.objects.count()
        for version in (None, WorkflowVersion.objects.create(workflow=self.workflow, numero=2, estado="ACTIVA")):
            with self.subTest(version=version):
                self.workflow.version_activa = version
                self.workflow.save()
                with self.assertRaises(ValidationError):
                    radicar_ticket(ticket, self.usuario)
                ticket.refresh_from_db()
                self.assertEqual(ticket.estado, "BORRADOR")
                self.assertIsNone(ticket.radicado)
                self.assertIsNone(ticket.instancia_workflow_id)
        self.assertFalse(InstanciaWorkflow.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_servicio_rollback_ante_fallo_despues_de_crear_instancia_y_tarea(self):
        from unittest.mock import patch
        from apps.tareas.models import Tarea
        from apps.workflows.models import InstanciaWorkflow
        from apps.workflows.motor import iniciar_workflow

        ticket = crear_borrador(self.usuario, self.servicio)
        eventos = RegistroAuditoria.objects.count()

        def fallar(*args, **kwargs):
            iniciar_workflow(*args, **kwargs)
            raise RuntimeError("Fallo tras iniciar")

        with patch("apps.tickets.operaciones.iniciar_workflow", side_effect=fallar):
            with self.assertRaises(RuntimeError):
                radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, "BORRADOR")
        self.assertIsNone(ticket.radicado)
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertFalse(InstanciaWorkflow.objects.exists())
        self.assertFalse(Tarea.objects.exists())
        self.assertFalse(ticket.historial.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_nueva_ejecucion_no_cambia_formulario_entregables_ni_instancia_anterior(self):
        from apps.catalogo.models import DefinicionEntregable
        from apps.workflows.versionamiento import crear_nueva_version as clonar, activar_version as activar

        definicion = DefinicionEntregable.objects.create(servicio=self.servicio, nombre="Antes", tipo="TEXTO")
        anterior = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(anterior, self.usuario)
        instancia_id = anterior.instancia_workflow_id
        nueva = clonar(self.workflow, self.usuario)
        activar(self.workflow, nueva, self.usuario)
        definicion.nombre = "Después"
        definicion.save()
        entrada_nueva = crear_nueva_version(self.servicio.formulario, self.usuario)
        activar_version(self.servicio.formulario, entrada_nueva, self.usuario)
        nuevo = crear_borrador(self.usuario, self.servicio)
        radicar_ticket(nuevo, self.usuario)
        anterior.refresh_from_db()
        self.assertEqual(anterior.instancia_workflow_id, instancia_id)
        self.assertEqual(anterior.instancia_workflow.workflow_version_id, self.version.pk)
        self.assertEqual(anterior.entregables.get().nombre, "Antes")
        self.assertEqual(anterior.detalle_servicio.formulario_version_id, self.formulario_version.pk)
        self.assertEqual(nuevo.instancia_workflow.workflow_version_id, nueva.pk)
        self.assertEqual(nuevo.entregables.get().nombre, "Después")
        self.assertEqual(nuevo.detalle_servicio.formulario_version_id, entrada_nueva.pk)


class ConcurrenciaRadicarWorkflowTests(TransactionTestCase):
    def setUp(self):
        RadicacionProcesoTests.setUp(self)

    def test_radicacion_concurrente_crea_una_instancia_para_servicio_y_proceso(self):
        from apps.workflows.models import InstanciaWorkflow, TareaWorkflow

        for tipo in ("SERVICIO", "PROCESO"):
            with self.subTest(tipo=tipo):
                self.servicio.tipo = tipo
                self.servicio.save()
                ticket = crear_borrador(self.usuario, self.servicio)
                barrera = threading.Barrier(2)
                resultados = []
                instancias = InstanciaWorkflow.objects.count()

                def radicar():
                    try:
                        copia = Ticket.objects.get(pk=ticket.pk)
                        barrera.wait(timeout=10)
                        radicar_ticket(copia, self.usuario)
                        resultados.append("radicado")
                    except ValidationError:
                        resultados.append("rechazado")
                    except Exception as exc:
                        resultados.append(exc)
                    finally:
                        connection.close()

                hilos = [threading.Thread(target=radicar, daemon=True) for _ in range(2)]
                for hilo in hilos:
                    hilo.start()
                for hilo in hilos:
                    hilo.join(timeout=15)
                    self.assertFalse(hilo.is_alive())
                self.assertCountEqual(resultados, ["radicado", "rechazado"])
                ticket.refresh_from_db()
                self.assertEqual(ticket.tipo, tipo)
                self.assertEqual(InstanciaWorkflow.objects.count(), instancias + 1)
                self.assertEqual(TareaWorkflow.objects.filter(instancia_etapa__instancia_workflow_id=ticket.instancia_workflow_id).count(), 1)
                self.assertEqual(ticket.historial.filter(tipo_evento="RADICADO").count(), 1)
                self.assertEqual(RegistroAuditoria.objects.filter(modelo="workflows.instanciaworkflow", object_id=ticket.instancia_workflow_id).count(), 1)


# ---------------------------------------------------------------------------
# Sprint 4.5 — Entrega formal al solicitante y cierre del Ticket.
# Entregable satisfecho != entrega formal != aceptación != Ticket cerrado.
# ---------------------------------------------------------------------------

PERIODO = Servicio.PoliticaEntrega.PERIODO_OBSERVACIONES
DIRECTO = Servicio.PoliticaEntrega.CIERRE_DIRECTO


class _EscenarioEntregaFormalMixin:
    """Servicio con política de entrega + 3 entregables (TEXTO obligatorio,
    ARCHIVO y ENLACE opcionales) y un Ticket EN_ATENCION tomado por un
    responsable individual (con un compañero de equipo que NO lo es)."""

    def _preparar_entrega_formal(self, politica=PERIODO, dias=3):
        from apps.catalogo.models import DefinicionEntregable

        self.solicitante45 = Usuario.objects.create_user("solicitante45", password=CLAVE_PRUEBA)
        self.responsable45 = Usuario.objects.create_user("responsable45", password=CLAVE_PRUEBA)
        self.companero45 = Usuario.objects.create_user("companero45", password=CLAVE_PRUEBA)
        self.ajeno45 = Usuario.objects.create_user("ajeno45", password=CLAVE_PRUEBA)
        self.equipo45 = Equipo.objects.create(nombre="Equipo 4.5")
        for usuario in (self.responsable45, self.companero45):
            MiembroEquipo.objects.create(equipo=self.equipo45, usuario=usuario)
            _otorgar_tickets_atender(usuario)
        self.servicio45, _, _ = _crear_servicio_con_formulario(self.solicitante45, [])
        Servicio.objects.filter(pk=self.servicio45.pk).update(
            politica_entrega=politica, dias_observacion=dias if politica == PERIODO else None
        )
        self.servicio45.refresh_from_db()
        for orden, (nombre, tipo, obligatorio) in enumerate(
            [("Resumen", "TEXTO", True), ("Archivo final", "ARCHIVO", False), ("Enlace", "ENLACE", False)]
        ):
            DefinicionEntregable.objects.create(
                servicio=self.servicio45, nombre=nombre, tipo=tipo, obligatorio=obligatorio, orden=orden
            )
        self.ticket45 = crear_borrador(self.solicitante45, self.servicio45)
        radicar_ticket(self.ticket45, self.solicitante45)
        self.ticket45 = asignar_ticket(self.ticket45, self.responsable45, equipo=self.equipo45)
        self.ticket45 = tomar_ticket(self.ticket45, self.responsable45)

    def _ent(self, tipo):
        return self.ticket45.entregables.get(tipo=tipo)

    def _listo(self, texto="Resultado v1"):
        registrar_resultado_entregable(self._ent("TEXTO"), self.responsable45, texto)

    def _entregar(self):
        return entregar_ticket(Ticket.objects.get(pk=self.ticket45.pk), self.responsable45)

    def _ticket(self):
        return Ticket.objects.get(pk=self.ticket45.pk)

    def _vencer(self, entrega):
        EntregaTicket.objects.filter(pk=entrega.pk).update(vence_en=timezone.now() - timedelta(minutes=1))

    def _historial(self, tipo):
        return HistorialTicket.objects.filter(ticket=self.ticket45, tipo_evento=tipo)


class EntregaPoliticaCongeladaTests(_EscenarioEntregaFormalMixin, TestCase):
    def test_ticket_congela_la_politica_del_servicio_al_crear_el_borrador(self):
        self._preparar_entrega_formal(PERIODO, 3)
        self.assertEqual(self.ticket45.entrega_politica, PERIODO)
        self.assertEqual(self.ticket45.entrega_dias_observacion, 3)

    def test_cambio_posterior_del_servicio_no_modifica_tickets_existentes(self):
        self._preparar_entrega_formal(PERIODO, 3)
        Servicio.objects.filter(pk=self.servicio45.pk).update(politica_entrega=DIRECTO, dias_observacion=None)
        ticket = self._ticket()
        self.assertEqual((ticket.entrega_politica, ticket.entrega_dias_observacion), (PERIODO, 3))
        nuevo = crear_borrador(self.solicitante45, Servicio.objects.get(pk=self.servicio45.pk))
        self.assertEqual((nuevo.entrega_politica, nuevo.entrega_dias_observacion), (DIRECTO, None))

    def test_la_politica_del_ticket_no_puede_modificarse_despues(self):
        self._preparar_entrega_formal(PERIODO, 3)
        ticket = self._ticket()
        ticket.entrega_politica = DIRECTO
        ticket.entrega_dias_observacion = None
        with self.assertRaises(ValidationError):
            ticket.save()
        self.assertEqual(self._ticket().entrega_politica, PERIODO)

    def test_ticket_sin_politica_conserva_el_flujo_resolver_y_cerrar(self):
        self._preparar_entrega_formal(politica="")
        self.assertEqual(self.ticket45.entrega_politica, "")
        with self.assertRaises(ValidationError):
            entregar_ticket(self._ticket(), self.responsable45)
        resolver_ticket(self._ticket(), self.responsable45, "Listo.")
        cerrar_ticket(self._ticket(), self.solicitante45)
        self.assertEqual(self._ticket().estado, Ticket.Estado.CERRADO)
        self.assertFalse(EntregaTicket.objects.exists())  # sin entregas ficticias

    def test_con_entrega_formal_no_se_puede_saltar_el_ciclo(self):
        self._preparar_entrega_formal()
        with self.assertRaises(ValidationError):
            resolver_ticket(self._ticket(), self.responsable45, "Intento de saltar la entrega.")
        self.assertFalse(puede_resolver_ticket(self.responsable45, self._ticket()))
        self._listo()
        self._entregar()
        ticket = self._ticket()
        with self.assertRaises(ValidationError):
            cerrar_ticket(ticket, self.solicitante45)
        with self.assertRaises(ValidationError):
            reabrir_ticket(ticket, self.responsable45, "Reabrir sin observaciones.")
        self.assertFalse(puede_cerrar_ticket(self.solicitante45, ticket))
        self.assertFalse(puede_reabrir_ticket(self.responsable45, ticket))
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)


class EntregarTicketTests(_MediaAisladaMixin, _EscenarioEntregaFormalMixin, TestCase):
    def setUp(self):
        self._preparar_entrega_formal()

    def test_solo_el_responsable_individual_actual_puede_entregar(self):
        self._listo()
        for usuario in (self.companero45, self.solicitante45, self.ajeno45):
            with self.assertRaises(PermissionDenied):
                entregar_ticket(self._ticket(), usuario)
        self.assertFalse(EntregaTicket.objects.exists())
        self.assertEqual(self._ticket().estado, Ticket.Estado.EN_ATENCION)

    def test_entregables_obligatorios_pendientes_bloquean_y_se_informan(self):
        with self.assertRaises(ValidationError) as contexto:
            self._entregar()
        self.assertIn("Resumen", str(contexto.exception))
        self.assertFalse(EntregaTicket.objects.exists())
        self.assertEqual(self._ticket().estado, Ticket.Estado.EN_ATENCION)

    def test_completar_entregables_no_equivale_a_entregar(self):
        self._listo()
        self.assertTrue(self._ent("TEXTO").satisfecho)
        self.assertEqual(self._ticket().estado, Ticket.Estado.EN_ATENCION)
        self.assertFalse(EntregaTicket.objects.exists())

    def test_entregar_crea_entrega_con_snapshot_historial_y_auditoria(self):
        self._listo("Resultado final")
        archivo = adjuntar_archivo_entregable(
            self._ent("ARCHIVO"), self.responsable45, SimpleUploadedFile("pieza.txt", b"contenido")
        )
        entrega = self._entregar()
        ticket = self._ticket()
        self.assertEqual(ticket.estado, Ticket.Estado.RESUELTO)
        self.assertEqual((entrega.numero, entrega.estado), (1, EntregaTicket.Estado.PENDIENTE))
        self.assertEqual(entrega.entregada_por, self.responsable45)
        self.assertEqual(entrega.vence_en, entrega.entregada_en + timedelta(days=3))
        resultados = {r.nombre: r for r in entrega.resultados.all()}
        self.assertEqual(set(resultados), {"Resumen", "Archivo final"})  # el ENLACE opcional no estaba listo
        self.assertEqual(resultados["Resumen"].texto, "Resultado final")
        self.assertEqual(list(resultados["Archivo final"].adjuntos.all()), [archivo])
        evento = self._historial(HistorialTicket.TipoEvento.ENTREGADO).get()
        self.assertEqual(evento.actor, self.responsable45)
        self.assertEqual(evento.datos["entrega_id"], entrega.pk)
        self.assertEqual(_auditorias_de_ticket(ticket).filter(datos_nuevos={"estado": "RESUELTO"}).count(), 1)
        auditoria_entrega = RegistroAuditoria.objects.filter(
            content_type=ContentType.objects.get_for_model(EntregaTicket), object_id=entrega.pk
        )
        self.assertEqual(auditoria_entrega.count(), 1)
        self.assertEqual(auditoria_entrega.get().accion, RegistroAuditoria.Accion.CREAR)
        self.assertEqual(auditoria_entrega.get().usuario, self.responsable45)

    def test_doble_entrega_se_rechaza_sin_duplicar(self):
        self._listo()
        self._entregar()
        with self.assertRaises(ValidationError):
            self._entregar()
        self.assertEqual(EntregaTicket.objects.filter(ticket=self.ticket45).count(), 1)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.ENTREGADO).count(), 1)

    def test_no_se_entrega_con_solicitudes_de_informacion_pendientes(self):
        self._listo()
        solicitar_informacion(self._ticket(), self.responsable45, "¿Puedes confirmar el formato?")
        with self.assertRaises(ValidationError):
            self._entregar()
        self.assertFalse(EntregaTicket.objects.exists())

    def test_no_se_entrega_mientras_el_trabajo_interno_sigue_en_curso(self):
        from unittest import mock

        self._listo()
        with mock.patch("apps.tickets.entregas._trabajo_interno_en_curso", return_value=True):
            with self.assertRaises(ValidationError):
                self._entregar()
        self.assertFalse(EntregaTicket.objects.exists())
        self.assertTrue(self._entregar())  # sin trabajo en curso, la entrega procede

    def test_sin_workflow_no_hay_trabajo_interno_pendiente(self):
        from apps.tickets.entregas import _trabajo_interno_en_curso

        self.assertIsNone(self._ticket().instancia_workflow_id)
        self.assertFalse(_trabajo_interno_en_curso(self._ticket()))

    def test_los_entregables_quedan_bloqueados_mientras_el_solicitante_responde(self):
        self._listo()
        self._entregar()
        with self.assertRaises(PermissionDenied):
            registrar_resultado_entregable(self._ent("TEXTO"), self.responsable45, "Cambio a escondidas")
        self.assertEqual(self._ent("TEXTO").texto, "Resultado v1")

    def test_politica_de_cierre_directo_entrega_y_cierra_sin_esperar(self):
        Servicio.objects.filter(pk=self.servicio45.pk).update(politica_entrega=DIRECTO, dias_observacion=None)
        ticket = crear_borrador(self.solicitante45, Servicio.objects.get(pk=self.servicio45.pk))
        radicar_ticket(ticket, self.solicitante45)
        ticket = tomar_ticket(ticket, self.responsable45)
        registrar_resultado_entregable(ticket.entregables.get(tipo="TEXTO"), self.responsable45, "Hecho")
        entrega = entregar_ticket(ticket, self.responsable45)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)
        self.assertEqual(entrega.estado, EntregaTicket.Estado.CERRADA_SIN_RESPUESTA)
        self.assertIsNone(entrega.vence_en)
        cierre = HistorialTicket.objects.get(ticket=ticket, tipo_evento=HistorialTicket.TipoEvento.CERRADO)
        self.assertEqual(cierre.datos["causa"], "CIERRE_DIRECTO")
        self.assertEqual(HistorialTicket.objects.filter(ticket=ticket, tipo_evento="ENTREGADO").count(), 1)

    def test_sin_entregables_configurados_la_entrega_formal_sigue_siendo_explicita(self):
        from apps.catalogo.models import DefinicionEntregable

        DefinicionEntregable.objects.filter(servicio=self.servicio45).update(activo=False)
        ticket = crear_borrador(self.solicitante45, Servicio.objects.get(pk=self.servicio45.pk))
        radicar_ticket(ticket, self.solicitante45)
        ticket = tomar_ticket(ticket, self.responsable45)
        self.assertFalse(ticket.entregables.exists())
        entrega = entregar_ticket(ticket, self.responsable45)
        self.assertEqual(entrega.resultados.count(), 0)


class RespuestaEntregaTests(_EscenarioEntregaFormalMixin, TestCase):
    def setUp(self):
        self._preparar_entrega_formal()
        self._listo()
        self.entrega = self._entregar()

    def test_aceptar_registra_actor_y_fecha_y_cierra_con_la_transicion_real(self):
        aceptar_entrega(self._ticket(), self.solicitante45)
        ticket, entrega = self._ticket(), EntregaTicket.objects.get(pk=self.entrega.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)
        self.assertEqual(entrega.estado, EntregaTicket.Estado.ACEPTADA)
        self.assertEqual(entrega.resuelta_por, self.solicitante45)
        self.assertIsNotNone(entrega.resuelta_en)
        cierre = self._historial(HistorialTicket.TipoEvento.CERRADO).get()
        self.assertEqual(cierre.actor, self.solicitante45)
        self.assertEqual(cierre.datos["causa"], "ACEPTACION_SOLICITANTE")
        self.assertEqual(cierre.datos["entrega_id"], entrega.pk)
        auditoria = _auditorias_de_ticket(ticket).get(datos_nuevos={"estado": "CERRADO"})
        self.assertEqual((auditoria.origen, auditoria.usuario), (RegistroAuditoria.Origen.USUARIO, self.solicitante45))

    def test_solo_el_solicitante_real_puede_responder(self):
        for usuario in (self.responsable45, self.companero45, self.ajeno45):
            with self.assertRaises(PermissionDenied):
                aceptar_entrega(self._ticket(), usuario)
            with self.assertRaises(PermissionDenied):
                observar_entrega(self._ticket(), usuario, "No soy el solicitante.")
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)
        self.assertEqual(EntregaTicket.objects.get(pk=self.entrega.pk).estado, EntregaTicket.Estado.PENDIENTE)

    def test_observar_exige_comentario(self):
        for comentario in ("", "   ", None):
            with self.assertRaises(ValidationError):
                observar_entrega(self._ticket(), self.solicitante45, comentario)
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)

    def test_observar_conserva_la_entrega_y_devuelve_el_ticket_a_atencion(self):
        observar_entrega(self._ticket(), self.solicitante45, "  Falta el logotipo.  ")
        ticket, entrega = self._ticket(), EntregaTicket.objects.get(pk=self.entrega.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual(ticket.usuario_responsable, self.responsable45)  # mismo responsable
        self.assertEqual(entrega.estado, EntregaTicket.Estado.OBSERVADA)
        self.assertEqual(entrega.observaciones, "Falta el logotipo.")
        self.assertEqual((entrega.resuelta_por, entrega.numero), (self.solicitante45, 1))
        self.assertEqual(entrega.resultados.get().texto, "Resultado v1")  # snapshot intacto
        evento = self._historial(HistorialTicket.TipoEvento.ENTREGA_OBSERVADA).get()
        self.assertEqual(evento.actor, self.solicitante45)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).count(), 0)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.REABIERTO).count(), 0)

    def test_responder_una_entrega_que_ya_no_esta_pendiente_falla(self):
        aceptar_entrega(self._ticket(), self.solicitante45)
        with self.assertRaises(ValidationError):
            aceptar_entrega(self._ticket(), self.solicitante45)
        with self.assertRaises(ValidationError):
            observar_entrega(self._ticket(), self.solicitante45, "Tarde.")
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).count(), 1)

    def test_no_se_puede_aceptar_tras_observar(self):
        observar_entrega(self._ticket(), self.solicitante45, "Ajustar.")
        with self.assertRaises(ValidationError):
            aceptar_entrega(self._ticket(), self.solicitante45)
        self.assertEqual(self._ticket().estado, Ticket.Estado.EN_ATENCION)

    def test_pasado_el_plazo_el_solicitante_ya_no_puede_responder(self):
        self._vencer(self.entrega)
        with self.assertRaises(ValidationError):
            aceptar_entrega(self._ticket(), self.solicitante45)
        with self.assertRaises(ValidationError):
            observar_entrega(self._ticket(), self.solicitante45, "Tarde.")
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)  # solo el Sistema cierra


class MultiplesCiclosEntregaTests(_MediaAisladaMixin, _EscenarioEntregaFormalMixin, TestCase):
    def setUp(self):
        self._preparar_entrega_formal()

    def test_entrega_observacion_ajuste_y_nueva_entrega_sin_destruir_la_historia(self):
        self._listo("Versión 1")
        archivo = adjuntar_archivo_entregable(
            self._ent("ARCHIVO"), self.responsable45, SimpleUploadedFile("v1.txt", b"uno")
        )
        primera = self._entregar()
        observar_entrega(self._ticket(), self.solicitante45, "Cambiar el texto y el archivo.")

        registrar_resultado_entregable(self._ent("TEXTO"), self.responsable45, "Versión 2")
        retirar_archivo_entregable(archivo, self.responsable45)
        nuevo = adjuntar_archivo_entregable(
            self._ent("ARCHIVO"), self.responsable45, SimpleUploadedFile("v2.txt", b"dos")
        )
        segunda = self._entregar()

        self.assertEqual((primera.numero, segunda.numero), (1, 2))
        primera.refresh_from_db()
        self.assertEqual(primera.estado, EntregaTicket.Estado.OBSERVADA)
        r1 = {r.nombre: r for r in primera.resultados.all()}
        r2 = {r.nombre: r for r in segunda.resultados.all()}
        self.assertEqual(r1["Resumen"].texto, "Versión 1")
        self.assertEqual(r2["Resumen"].texto, "Versión 2")
        self.assertEqual(list(r1["Archivo final"].adjuntos.all()), [archivo])
        self.assertEqual(list(r2["Archivo final"].adjuntos.all()), [nuevo])
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)
        aceptar_entrega(self._ticket(), self.solicitante45)
        self.assertEqual(self._ticket().estado, Ticket.Estado.CERRADO)
        self.assertEqual(EntregaTicket.objects.filter(ticket=self.ticket45).count(), 2)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.ENTREGADO).count(), 2)

    def test_un_archivo_retirado_despues_de_entregado_sigue_descargable_para_el_solicitante(self):
        self._listo()
        archivo = adjuntar_archivo_entregable(
            self._ent("ARCHIVO"), self.responsable45, SimpleUploadedFile("entregado.txt", b"contenido")
        )
        self._entregar()
        observar_entrega(self._ticket(), self.solicitante45, "Cambiar archivo.")
        retirar_archivo_entregable(archivo, self.responsable45)
        self.client.force_login(self.solicitante45)
        respuesta = self.client.get(reverse("tickets:descargar_adjunto", args=[archivo.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(b"".join(respuesta.streaming_content), b"contenido")

    def test_un_archivo_retirado_que_nunca_se_entrego_sigue_sin_descargarse(self):
        archivo = adjuntar_archivo_entregable(
            self._ent("ARCHIVO"), self.responsable45, SimpleUploadedFile("nunca.txt", b"x")
        )
        retirar_archivo_entregable(archivo, self.responsable45)
        self.client.force_login(self.solicitante45)
        self.assertEqual(self.client.get(reverse("tickets:descargar_adjunto", args=[archivo.pk])).status_code, 404)


class EntregaModeloTests(_EscenarioEntregaFormalMixin, TestCase):
    def setUp(self):
        self._preparar_entrega_formal()
        self._listo()
        self.entrega = self._entregar()

    def test_los_datos_de_una_entrega_son_inmutables_y_no_se_elimina(self):
        self.entrega.numero = 9
        with self.assertRaises(ValidationError):
            self.entrega.save()
        entrega = EntregaTicket.objects.get(pk=self.entrega.pk)
        with self.assertRaises(ValidationError):
            entrega.delete()

    def test_a_lo_sumo_una_entrega_pendiente_por_ticket(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            EntregaTicket.objects.create(
                ticket=self.ticket45, numero=2, entregada_por=self.responsable45, entregada_en=timezone.now(),
                politica=PERIODO, dias_observacion=3, vence_en=timezone.now() + timedelta(days=3),
            )

    def test_servicio_rechaza_periodo_sin_dias(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Servicio.objects.filter(pk=self.servicio45.pk).update(dias_observacion=None)


class CierreAutomaticoEntregaTests(_EscenarioEntregaFormalMixin, TestCase):
    def setUp(self):
        self._preparar_entrega_formal()
        self._listo()
        self.entrega = self._entregar()

    def test_el_vencimiento_cierra_con_actor_sistema_y_se_distingue_de_la_aceptacion(self):
        self._vencer(self.entrega)
        cerrada = cerrar_entrega_vencida(self.entrega)
        self.assertIsNotNone(cerrada)
        ticket, entrega = self._ticket(), EntregaTicket.objects.get(pk=self.entrega.pk)
        self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)
        self.assertEqual(entrega.estado, EntregaTicket.Estado.CERRADA_POR_VENCIMIENTO)
        self.assertIsNone(entrega.resuelta_por)
        cierre = self._historial(HistorialTicket.TipoEvento.CERRADO).get()
        self.assertIsNone(cierre.actor)
        self.assertEqual(cierre.datos["causa"], "VENCIMIENTO_SIN_RESPUESTA")
        auditoria = _auditorias_de_ticket(ticket).get(datos_nuevos={"estado": "CERRADO"})
        self.assertEqual((auditoria.origen, auditoria.usuario), (RegistroAuditoria.Origen.SISTEMA, None))
        auditoria_entrega = RegistroAuditoria.objects.get(
            content_type=ContentType.objects.get_for_model(EntregaTicket), object_id=entrega.pk,
            accion=RegistroAuditoria.Accion.ACTUALIZAR,
        )
        self.assertEqual(auditoria_entrega.origen, RegistroAuditoria.Origen.SISTEMA)

    def test_no_cierra_una_entrega_cuyo_plazo_no_ha_vencido(self):
        self.assertIsNone(cerrar_entrega_vencida(self.entrega))
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)

    def test_es_idempotente(self):
        self._vencer(self.entrega)
        self.assertIsNotNone(cerrar_entrega_vencida(self.entrega))
        self.assertIsNone(cerrar_entrega_vencida(self.entrega))
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).count(), 1)
        self.assertEqual(_auditorias_de_ticket(self.ticket45).filter(datos_nuevos={"estado": "CERRADO"}).count(), 1)

    def test_no_pisa_una_respuesta_ya_registrada(self):
        aceptar_entrega(self._ticket(), self.solicitante45)
        self._vencer(self.entrega)
        self.assertIsNone(cerrar_entrega_vencida(self.entrega))
        self.assertEqual(EntregaTicket.objects.get(pk=self.entrega.pk).estado, EntregaTicket.Estado.ACEPTADA)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).get().datos["causa"], "ACEPTACION_SOLICITANTE")

    def test_tras_el_cierre_automatico_el_solicitante_ya_no_puede_aceptar_ni_observar(self):
        self._vencer(self.entrega)
        cerrar_entrega_vencida(self.entrega)
        with self.assertRaises(ValidationError):
            aceptar_entrega(self._ticket(), self.solicitante45)
        with self.assertRaises(ValidationError):
            observar_entrega(self._ticket(), self.solicitante45, "Tarde.")

    def test_la_tarea_periodica_cierra_solo_las_vencidas_y_es_idempotente(self):
        self.assertEqual(cerrar_entregas_vencidas(), 0)  # nada vencido todavía
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)
        self._vencer(self.entrega)
        self.assertEqual(cerrar_entregas_vencidas(), 1)
        self.assertEqual(cerrar_entregas_vencidas(), 0)
        self.assertEqual(self._ticket().estado, Ticket.Estado.CERRADO)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).count(), 1)

    def test_la_tarea_no_toca_las_entregas_observadas(self):
        observar_entrega(self._ticket(), self.solicitante45, "Ajustar.")
        self._vencer(self.entrega)
        self.assertEqual(cerrar_entregas_vencidas(), 0)
        self.assertEqual(self._ticket().estado, Ticket.Estado.EN_ATENCION)

    def test_la_tarea_esta_programada_en_celery_beat(self):
        from django_celery_beat.models import PeriodicTask

        tarea = PeriodicTask.objects.get(name="tickets.cerrar_entregas_vencidas")
        self.assertEqual(tarea.task, "apps.tickets.tasks.cerrar_entregas_vencidas")

    def test_el_historial_acepta_actor_sistema_en_la_linea_de_tiempo(self):
        self._vencer(self.entrega)
        cerrar_entrega_vencida(self.entrega)
        self.client.force_login(self.solicitante45)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket45.pk]))
        self.assertContains(respuesta, "Sistema")
        self.assertContains(respuesta, "plazo vencido sin respuesta")


class EntregaVistasTests(_MediaAisladaMixin, _EscenarioEntregaFormalMixin, TestCase):
    def setUp(self):
        self._preparar_entrega_formal()

    def _detalle(self, usuario):
        self.client.force_login(usuario)
        return self.client.get(reverse("tickets:detalle", args=[self.ticket45.pk]))

    def test_el_responsable_ve_entregables_y_los_pendientes_bloquean_la_entrega(self):
        respuesta = self._detalle(self.responsable45)
        self.assertContains(respuesta, "Resultados que debes producir")
        self.assertContains(respuesta, "Faltan entregables obligatorios")
        self.assertNotContains(respuesta, "Sí, entregar ahora")

    def test_el_responsable_produce_entregables_y_entrega_con_confirmacion(self):
        self.client.force_login(self.responsable45)
        self.client.post(
            reverse("tickets:entregable_resultado", args=[self.ticket45.pk, self._ent("TEXTO").pk]),
            {"valor": "Texto final"},
        )
        self.assertEqual(self._ent("TEXTO").texto, "Texto final")
        self.assertContains(self._detalle(self.responsable45), "Sí, entregar ahora")
        # Sin confirmación explícita no se entrega.
        self.client.post(reverse("tickets:entregar", args=[self.ticket45.pk]))
        self.assertFalse(EntregaTicket.objects.exists())
        respuesta = self.client.post(reverse("tickets:entregar", args=[self.ticket45.pk]), {"confirmar": "1"})
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(EntregaTicket.objects.filter(ticket=self.ticket45).count(), 1)
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)

    def test_el_solicitante_no_puede_producir_entregables_ni_entregar(self):
        self.client.force_login(self.solicitante45)
        self.client.post(
            reverse("tickets:entregable_resultado", args=[self.ticket45.pk, self._ent("TEXTO").pk]), {"valor": "Intruso"}
        )
        self.assertEqual(self._ent("TEXTO").texto, "")
        self._listo()
        self.client.post(reverse("tickets:entregar", args=[self.ticket45.pk]), {"confirmar": "1"})
        self.assertFalse(EntregaTicket.objects.exists())

    def test_el_solicitante_no_ve_los_entregables_en_produccion_pero_si_la_entrega(self):
        self._listo("Resultado visible")
        self.assertNotContains(self._detalle(self.solicitante45), "Resultados que debes producir")
        self._entregar()
        respuesta = self._detalle(self.solicitante45)
        self.assertContains(respuesta, "Resultado entregado el")
        self.assertContains(respuesta, "Resultado visible")
        self.assertContains(respuesta, "Todo está correcto")
        self.assertContains(respuesta, "Tengo observaciones")
        self.assertContains(respuesta, "se cerrará automáticamente")

    def test_el_responsable_no_ve_las_acciones_del_solicitante(self):
        self._listo()
        self._entregar()
        respuesta = self._detalle(self.responsable45)
        self.assertNotContains(respuesta, "Todo está correcto")
        self.assertNotContains(respuesta, "Tengo observaciones")

    def test_el_solicitante_acepta_por_http(self):
        self._listo()
        self._entregar()
        self.client.force_login(self.solicitante45)
        self.client.post(reverse("tickets:aceptar_entrega", args=[self.ticket45.pk]))
        self.assertEqual(self._ticket().estado, Ticket.Estado.CERRADO)

    def test_el_solicitante_observa_por_http_y_exige_comentario(self):
        self._listo()
        self._entregar()
        self.client.force_login(self.solicitante45)
        self.client.post(reverse("tickets:observar_entrega", args=[self.ticket45.pk]), {"observaciones": " "})
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)
        self.client.post(reverse("tickets:observar_entrega", args=[self.ticket45.pk]), {"observaciones": "Ajustar color."})
        self.assertEqual(self._ticket().estado, Ticket.Estado.EN_ATENCION)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket45.pk]))
        self.assertContains(respuesta, "Ajustar color.")
        self.assertContains(respuesta, "Entrega 1")

    def test_un_ajeno_no_ve_el_ticket_ni_responde(self):
        self._listo()
        self._entregar()
        self.assertEqual(self._detalle(self.ajeno45).status_code, 403)
        self.client.post(reverse("tickets:aceptar_entrega", args=[self.ticket45.pk]))
        self.assertEqual(self._ticket().estado, Ticket.Estado.RESUELTO)

    def test_los_ticket_sin_politica_no_muestran_la_seccion_de_entrega(self):
        Servicio.objects.filter(pk=self.servicio45.pk).update(politica_entrega="", dias_observacion=None)
        ticket = crear_borrador(self.solicitante45, Servicio.objects.get(pk=self.servicio45.pk))
        radicar_ticket(ticket, self.solicitante45)
        ticket = tomar_ticket(ticket, self.responsable45)
        self.client.force_login(self.responsable45)
        respuesta = self.client.get(reverse("tickets:detalle", args=[ticket.pk]))
        self.assertNotContains(respuesta, "Entrega al solicitante")
        self.assertContains(respuesta, "Resolver")  # flujo anterior intacto


class ConcurrenciaEntregaFormalTests(_EscenarioEntregaFormalMixin, TransactionTestCase):
    def setUp(self):
        self._preparar_entrega_formal()

    def _correr(self, *tareas):
        barrera = threading.Barrier(len(tareas))
        resultados = {}

        def _envolver(clave, funcion):
            barrera.wait()
            try:
                funcion()
                resultados[clave] = "ok"
            except (PermissionDenied, ValidationError):
                resultados[clave] = "fallo"
            finally:
                connection.close()

        hilos = [threading.Thread(target=_envolver, args=(clave, f)) for clave, f in tareas]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        return list(resultados.values())

    def test_dos_entregas_concurrentes_solo_una_gana(self):
        self._listo()
        valores = self._correr(("a", self._entregar), ("b", self._entregar))
        self.assertEqual((valores.count("ok"), valores.count("fallo")), (1, 1))
        self.assertEqual(EntregaTicket.objects.filter(ticket=self.ticket45).count(), 1)
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.ENTREGADO).count(), 1)

    def test_aceptar_y_observar_concurrentes_solo_una_gana(self):
        self._listo()
        self._entregar()
        valores = self._correr(
            ("aceptar", lambda: aceptar_entrega(self._ticket(), self.solicitante45)),
            ("observar", lambda: observar_entrega(self._ticket(), self.solicitante45, "Ajustar.")),
        )
        self.assertEqual((valores.count("ok"), valores.count("fallo")), (1, 1))
        entrega = EntregaTicket.objects.get(ticket=self.ticket45)
        ticket = self._ticket()
        if entrega.estado == EntregaTicket.Estado.ACEPTADA:
            self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)
            self.assertEqual(self._historial(HistorialTicket.TipoEvento.ENTREGA_OBSERVADA).count(), 0)
        else:
            self.assertEqual(entrega.estado, EntregaTicket.Estado.OBSERVADA)
            self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
            self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).count(), 0)

    def test_dos_cierres_automaticos_concurrentes_solo_uno_cierra(self):
        self._listo()
        entrega = self._entregar()
        self._vencer(entrega)
        valores = self._correr(
            ("a", lambda: cerrar_entrega_vencida(entrega)), ("b", lambda: cerrar_entrega_vencida(entrega))
        )
        self.assertEqual(valores, ["ok", "ok"])  # el perdedor es un no-op, no un error
        self.assertEqual(self._historial(HistorialTicket.TipoEvento.CERRADO).count(), 1)
        self.assertEqual(self._ticket().estado, Ticket.Estado.CERRADO)
        self.assertEqual(_auditorias_de_ticket(self.ticket45).filter(datos_nuevos={"estado": "CERRADO"}).count(), 1)


# ---------------------------------------------------------------------------
# Fase visual V2 — experiencia de solicitud (descubrir → completar →
# previsualizar → revisar → enviar → confirmar). Solo comportamiento
# verificable del servidor: acceso, versión congelada, estado de los campos,
# errores por campo, revisión, radicación real y confirmación. Nada de
# proporciones, colores ni animaciones.
# ---------------------------------------------------------------------------


def _especificacion_solicitud():
    """Un formulario con un campo de cada familia visual."""
    T = Campo.TipoCampo
    return [
        {"tipo": T.TEXTO, "etiqueta": "Asunto", "obligatorio": True, "orden": 1},
        {"tipo": T.TEXTO_LARGO, "etiqueta": "Detalle", "orden": 2},
        {
            "tipo": T.LISTA, "etiqueta": "Prioridad", "orden": 3,
            "opciones": [("alta", "Alta"), ("media", "Media"), ("baja", "Baja")],
        },
        {
            "tipo": T.LISTA, "etiqueta": "Sede", "orden": 4,
            "opciones": [(f"s{i}", f"Sede {i}") for i in range(6)],
        },
        {
            "tipo": T.MULTILISTA, "etiqueta": "Canales", "orden": 5,
            "opciones": [("web", "Web"), ("app", "Aplicación"), ("tel", "Teléfono")],
        },
        {"tipo": T.BOOLEANO, "etiqueta": "Urgente", "orden": 6},
        {"tipo": T.FECHA, "etiqueta": "Fecha límite", "orden": 7},
        {"tipo": T.NUMERO, "etiqueta": "Cantidad", "orden": 8},
        {
            "tipo": T.ARCHIVO, "etiqueta": "Adjunto", "orden": 9,
            "configuracion": {"extensiones_permitidas": ["pdf"], "tamano_maximo_mb": 5},
        },
    ]


def _items_por_etiqueta(respuesta):
    return {item["campo"].etiqueta: item for item in respuesta.context["items"]}


class _EscenarioSolicitudMixin:
    """Usuario solicitante + servicio con el formulario de
    `_especificacion_solicitud` y un borrador listo."""

    def _preparar(self, especificacion=None, reglas_builder=None):
        self.usuario = Usuario.objects.create_user(username="sol_ana", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user(username="sol_ajeno", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, especificacion or _especificacion_solicitud(), reglas_builder
        )
        self.ticket = crear_borrador(self.usuario, self.servicio)
        self.client.login(username="sol_ana", password=CLAVE_PRUEBA)

    def _nombre(self, etiqueta):
        return f"campo_{self.campos[etiqueta].id}"

    def _url(self, nombre, *args):
        return reverse(f"tickets:{nombre}", args=args or [self.ticket.pk])

    def _respuesta(self, etiqueta):
        return RespuestaCampo.objects.filter(
            respuesta_formulario=self.ticket.respuesta_formulario, campo=self.campos[etiqueta]
        ).first()


class SolicitudEntradaTests(TestCase):
    """Entrar a una solicitud desde Inicio/Explorar: directo, sin ficha ni
    pregunta Servicio/Proceso, y sin dejar basura al navegar."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="ent_ana", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto"}]
        )
        self.client.login(username="ent_ana", password=CLAVE_PRUEBA)

    def _entrar(self, servicio=None):
        return self.client.get(reverse("tickets:solicitar", args=[(servicio or self.servicio).pk]))

    def test_entrar_crea_el_borrador_y_lleva_directo_al_formulario(self):
        respuesta = self._entrar()
        ticket = Ticket.objects.get(solicitante=self.usuario)
        self.assertRedirects(respuesta, reverse("tickets:borrador", args=[ticket.pk]))
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertEqual(ticket.detalle_servicio.servicio, self.servicio)

    def test_entrar_es_idempotente_mientras_el_borrador_siga_vacio(self):
        self._entrar()
        self._entrar()
        self._entrar()
        self.assertEqual(Ticket.objects.filter(solicitante=self.usuario).count(), 1)

    def test_un_borrador_con_respuestas_no_se_reutiliza(self):
        self._entrar()
        primero = Ticket.objects.get(solicitante=self.usuario)
        guardar_respuestas_borrador(primero, self.usuario, {self.campos["Texto"].id: "Ya empecé"})
        self._entrar()
        self.assertEqual(Ticket.objects.filter(solicitante=self.usuario).count(), 2)

    def test_un_borrador_de_otra_version_del_formulario_no_se_reutiliza(self):
        self._entrar()
        nueva = crear_nueva_version(self.servicio.formulario, actor=self.usuario)
        activar_version(self.servicio.formulario, nueva, actor=self.usuario)
        self._entrar()
        self.assertEqual(Ticket.objects.filter(solicitante=self.usuario).count(), 2)

    def test_el_borrador_de_otro_usuario_no_se_reutiliza(self):
        otro = Usuario.objects.create_user(username="ent_otro", password=CLAVE_PRUEBA)
        crear_borrador(otro, self.servicio)
        self._entrar()
        self.assertEqual(Ticket.objects.filter(solicitante=self.usuario).count(), 1)

    def test_un_proceso_entra_por_la_misma_experiencia(self):
        self.servicio.tipo = Servicio.Tipo.PROCESO
        self.servicio.save()
        entrada = self._entrar()
        ticket = Ticket.objects.get(solicitante=self.usuario)
        self.assertEqual(entrada.url, reverse("tickets:borrador", args=[ticket.pk]))
        formulario = self.client.get(entrada.url)
        self.assertTemplateUsed(formulario, "tickets/solicitud.html")
        self.assertContains(formulario, "Proceso")

    def test_servicio_no_visible_responde_404_y_no_crea_nada(self):
        self.servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.RESTRINGIDO
        self.servicio.save()
        self.assertEqual(self._entrar().status_code, 404)
        self.assertFalse(Ticket.objects.filter(solicitante=self.usuario).exists())

    def test_servicio_inactivo_responde_404(self):
        self.servicio.activo = False
        self.servicio.save()
        self.assertEqual(self._entrar().status_code, 404)

    def test_servicio_sin_formulario_activo_muestra_el_estado_no_disponible(self):
        otro = Servicio.objects.create(
            nombre="Sin formulario", categoria=self.servicio.categoria,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO,
        )
        respuesta = self._entrar(otro)
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud_no_disponible.html")
        self.assertFalse(Ticket.objects.filter(solicitante=self.usuario).exists())

    def test_exige_autenticacion_y_solo_acepta_get(self):
        self.assertEqual(self.client.post(reverse("tickets:solicitar", args=[self.servicio.pk])).status_code, 405)
        self.client.logout()
        respuesta = self._entrar()
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("core:login"), respuesta.url)


class SolicitudEspacioDeTrabajoTests(_EscenarioSolicitudMixin, TestCase):
    """Qué recibe el formulario: versión congelada, representación por tipo,
    progreso y vista previa humana."""

    def setUp(self):
        self._preparar()

    def _abrir(self):
        return self.client.get(self._url("borrador"))

    def test_renderiza_el_workspace_con_la_version_congelada_del_ticket(self):
        respuesta = self._abrir()
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud.html")
        self.assertEqual(len(respuesta.context["items"]), len(self.campos))

        # Una versión nueva y activa después no altera un borrador ya creado.
        nueva = crear_nueva_version(self.servicio.formulario, actor=self.usuario)
        Campo.objects.create(version=nueva, tipo=Campo.TipoCampo.TEXTO, etiqueta="Campo nuevo", orden=99)
        activar_version(self.servicio.formulario, nueva, actor=self.usuario)
        self.assertNotIn("Campo nuevo", _items_por_etiqueta(self._abrir()))

    def test_cada_campo_se_representa_segun_su_tipo_y_cantidad_de_opciones(self):
        items = _items_por_etiqueta(self._abrir())
        controles = {etiqueta: item["control"] for etiqueta, item in items.items()}
        self.assertEqual(
            controles,
            {
                "Asunto": "texto",
                "Detalle": "area",
                "Prioridad": "tarjetas",  # pocas opciones, selección única
                "Sede": "select",  # muchas opciones, selección única
                "Canales": "chips",  # pocas opciones, selección múltiple
                "Urgente": "switch",
                "Fecha límite": "fecha",
                "Cantidad": "numero",
                "Adjunto": "archivo",
            },
        )

    def test_los_campos_cortos_comparten_fila_y_los_amplios_ocupan_toda_la_fila(self):
        items = _items_por_etiqueta(self._abrir())
        for corto in ("Asunto", "Sede", "Urgente", "Fecha límite", "Cantidad"):
            self.assertEqual(items[corto]["ancho"], "medio", corto)
        for amplio in ("Detalle", "Prioridad", "Canales", "Adjunto"):
            self.assertEqual(items[amplio]["ancho"], "completo", amplio)

    def test_el_html_usa_controles_semanticos_reales(self):
        html = self._abrir().content.decode()
        self.assertIn('type="radio"', html)  # tarjetas de selección única
        self.assertIn('type="checkbox"', html)  # chips y switch
        self.assertIn('role="switch"', html)
        self.assertIn('type="file"', html)
        self.assertIn('enctype="multipart/form-data"', html)

    def test_el_progreso_cuenta_solo_obligatorios_completados(self):
        progreso = self._abrir().context["progreso"]
        self.assertEqual((progreso["completados"], progreso["total"]), (0, 1))
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Asunto"].id: "Listo"})
        progreso = self._abrir().context["progreso"]
        self.assertEqual((progreso["completados"], progreso["total"], progreso["porcentaje"]), (1, 1, 100))

    def test_la_vista_previa_muestra_etiquetas_humanas_no_valores_internos(self):
        guardar_respuestas_borrador(
            self.ticket,
            self.usuario,
            {
                self.campos["Asunto"].id: "Diseño de pieza",
                self.campos["Prioridad"].id: "alta",
                self.campos["Canales"].id: ["web", "tel"],
                self.campos["Fecha límite"].id: "2026-10-12",
            },
        )
        filas = {f["etiqueta"]: f for f in self._abrir().context["resumen"]}
        self.assertEqual(filas["Asunto"]["texto"], "Diseño de pieza")
        self.assertEqual(filas["Prioridad"]["texto"], "Alta")  # la etiqueta, no el valor "alta"
        self.assertEqual(filas["Canales"]["valores"], ["Web", "Teléfono"])
        self.assertIn("octubre", filas["Fecha límite"]["texto"])  # fecha legible, no ISO
        self.assertNotIn("Detalle", filas)  # sin responder: no se rellena con "Sin responder"
        self.assertNotIn("Sede", filas)

    def test_sin_respuestas_la_vista_previa_no_inventa_filas_de_texto(self):
        etiquetas = [f["etiqueta"] for f in self._abrir().context["resumen"]]
        self.assertEqual(etiquetas, ["Urgente"])  # un Sí/No siempre tiene valor: "No"

    def test_los_valores_guardados_vuelven_a_los_controles(self):
        guardar_respuestas_borrador(
            self.ticket,
            self.usuario,
            {
                self.campos["Asunto"].id: "Texto guardado",
                self.campos["Prioridad"].id: "media",
                self.campos["Canales"].id: ["app"],
                self.campos["Urgente"].id: True,
                self.campos["Cantidad"].id: "12",
            },
        )
        items = _items_por_etiqueta(self._abrir())
        self.assertEqual(items["Asunto"]["valor"], "Texto guardado")
        self.assertEqual([o["valor"] for o in items["Prioridad"]["opciones"] if o["seleccionada"]], ["media"])
        self.assertEqual(items["Canales"]["seleccion"], ["app"])
        self.assertTrue(items["Urgente"]["marcado"])
        self.assertEqual(items["Cantidad"]["valor"], "12")  # sin ceros decimales de más

    def test_solo_el_propietario_ve_su_borrador(self):
        self.client.logout()
        self.client.login(username="sol_ajeno", password=CLAVE_PRUEBA)
        self.assertEqual(self._abrir().status_code, 403)

    def test_un_ticket_ya_radicado_no_vuelve_al_workspace(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Asunto"].id: "x"})
        radicar_ticket(self.ticket, self.usuario)
        self.assertRedirects(self._abrir(), self._url("detalle"))

    def test_servicio_desactivado_se_avisa_en_el_borrador(self):
        self.servicio.activo = False
        self.servicio.save()
        respuesta = self._abrir()
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(respuesta.context["servicio_activo"])


class SolicitudCamposCondicionalesTests(_EscenarioSolicitudMixin, TestCase):
    """Las reglas las decide el servidor (`validaciones.calcular_estados_efectivos`);
    el workspace y el endpoint de estado solo las reflejan."""

    def setUp(self):
        def reglas(campos):
            for efecto in (ReglaCondicional.Efecto.MOSTRAR, ReglaCondicional.Efecto.REQUERIR):
                ReglaCondicional.objects.create(
                    campo_origen=campos["Urgente"], operador=ReglaCondicional.Operador.IGUAL_A,
                    valor="true", campo_objetivo=campos["Justificación"], efecto=efecto,
                )

        especificacion = [
            {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Asunto", "obligatorio": True, "orden": 1},
            {"tipo": Campo.TipoCampo.BOOLEANO, "etiqueta": "Urgente", "orden": 2},
            {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Justificación", "orden": 3},
        ]
        self._preparar(especificacion, reglas)

    def _estado(self, datos=None):
        return self.client.post(self._url("solicitud_estado"), datos or {})

    def test_el_campo_condicional_nace_oculto_y_el_origen_se_marca(self):
        items = _items_por_etiqueta(self.client.get(self._url("borrador")))
        self.assertFalse(items["Justificación"]["visible"])
        self.assertTrue(items["Urgente"]["es_origen"])
        self.assertFalse(items["Asunto"]["es_origen"])

    def test_el_html_de_un_campo_oculto_va_oculto_y_sin_enviarse(self):
        html = self.client.get(self._url("borrador")).content.decode()
        bloque = html.split(f'id="field_{self.campos["Justificación"].id}"')[1].split("</div>")[0]
        self.assertIn("hidden", bloque)
        self.assertIn("disabled", bloque)

    def test_el_endpoint_refleja_visibilidad_y_obligatoriedad_dinamica(self):
        justificacion = str(self.campos["Justificación"].id)
        apagado = self._estado({self._nombre("Asunto"): "x"}).json()["campos"][justificacion]
        self.assertEqual(apagado, {"visible": False, "requerido": False})
        encendido = self._estado({self._nombre("Urgente"): "on"}).json()["campos"][justificacion]
        self.assertEqual(encendido, {"visible": True, "requerido": True})

    def test_el_progreso_incorpora_el_campo_requerido_cuando_aparece(self):
        guardar_respuestas_borrador(
            self.ticket, self.usuario, {self.campos["Asunto"].id: "x", self.campos["Urgente"].id: True}
        )
        progreso = self.client.get(self._url("borrador")).context["progreso"]
        self.assertEqual((progreso["completados"], progreso["total"]), (1, 2))

    def test_el_endpoint_no_guarda_nada(self):
        antes = RespuestaCampo.objects.count()
        self._estado({self._nombre("Asunto"): "no se guarda", self._nombre("Urgente"): "on"})
        self.assertEqual(RespuestaCampo.objects.count(), antes)

    def test_el_endpoint_usa_lo_ya_guardado_igual_que_el_guardado_real(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Urgente"].id: True})
        justificacion = str(self.campos["Justificación"].id)
        # Sin enviar "Urgente" ahora, el servidor ve el booleano ausente como "no marcado".
        estado = self._estado({}).json()["campos"][justificacion]
        self.assertFalse(estado["visible"])

    def test_el_endpoint_solo_acepta_post_del_propietario_sobre_un_borrador(self):
        self.assertEqual(self.client.get(self._url("solicitud_estado")).status_code, 405)
        self.client.logout()
        self.client.login(username="sol_ajeno", password=CLAVE_PRUEBA)
        self.assertEqual(self._estado().status_code, 403)
        self.client.logout()
        self.assertEqual(self._estado().status_code, 302)  # a login

    def test_el_endpoint_rechaza_un_ticket_ya_radicado(self):
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Asunto"].id: "x"})
        radicar_ticket(self.ticket, self.usuario)
        self.assertEqual(self._estado().status_code, 403)

    def test_guardar_un_campo_oculto_no_lo_persiste(self):
        self.client.post(
            self._url("borrador"), {self._nombre("Asunto"): "x", self._nombre("Justificación"): "no aplica"}
        )
        self.assertIsNone(self._respuesta("Justificación"))

    def test_revisar_exige_el_campo_que_la_regla_volvio_requerido(self):
        respuesta = self.client.post(
            self._url("revisar"), {self._nombre("Asunto"): "x", self._nombre("Urgente"): "on"}
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud.html")
        items = _items_por_etiqueta(respuesta)
        self.assertTrue(items["Justificación"]["visible"])
        self.assertEqual(items["Justificación"]["errores"], ["Este campo es obligatorio."])


class SolicitudGuardadoYErroresTests(_MediaAisladaMixin, _EscenarioSolicitudMixin, TestCase):
    """Guardar borrador y revisar: errores junto a cada campo, sin perder el
    resto del envío."""

    def setUp(self):
        self._preparar()

    def test_guardar_borrador_valido_persiste_y_vuelve_al_formulario(self):
        respuesta = self.client.post(self._url("borrador"), {self._nombre("Asunto"): "Hola"})
        self.assertRedirects(respuesta, self._url("borrador"))
        self.assertEqual(self._respuesta("Asunto").valor_texto, "Hola")
        mensajes = [str(m) for m in get_messages(respuesta.wsgi_request)]
        self.assertIn("Borrador guardado.", mensajes)

    def test_un_valor_invalido_se_reporta_en_su_campo_y_no_hace_perder_el_resto(self):
        respuesta = self.client.post(
            self._url("borrador"), {self._nombre("Asunto"): "Sí se guarda", self._nombre("Cantidad"): "abc"}
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud.html")
        items = _items_por_etiqueta(respuesta)
        self.assertTrue(items["Cantidad"]["errores"])
        self.assertEqual(items["Cantidad"]["valor"], "abc")  # se conserva lo que escribió
        self.assertFalse(items["Asunto"]["errores"])
        self.assertEqual(self._respuesta("Asunto").valor_texto, "Sí se guarda")
        self.assertIsNone(self._respuesta("Cantidad"))

    def test_los_errores_se_resumen_con_enlaces_a_cada_campo(self):
        respuesta = self.client.post(self._url("borrador"), {self._nombre("Cantidad"): "abc"})
        self.assertEqual([i["campo"].etiqueta for i in respuesta.context["campos_con_error"]], ["Cantidad"])
        self.assertContains(respuesta, f'href="#campo_{self.campos["Cantidad"].id}"')
        self.assertContains(respuesta, f'id="err_{self.campos["Cantidad"].id}"')
        self.assertContains(respuesta, 'aria-invalid="true"')

    def test_opcion_invalida_en_lista_se_reporta_en_su_campo(self):
        respuesta = self.client.post(self._url("borrador"), {self._nombre("Prioridad"): "inexistente"})
        self.assertTrue(_items_por_etiqueta(respuesta)["Prioridad"]["errores"])

    def test_revisar_sin_obligatorios_no_avanza_y_señala_el_campo(self):
        respuesta = self.client.post(self._url("revisar"), {self._nombre("Detalle"): "Lo conservo"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud.html")
        items = _items_por_etiqueta(respuesta)
        self.assertEqual(items["Asunto"]["errores"], ["Este campo es obligatorio."])
        self.assertEqual(self._respuesta("Detalle").valor_texto, "Lo conservo")
        self.assertEqual(Ticket.objects.get(pk=self.ticket.pk).estado, Ticket.Estado.BORRADOR)

    def test_revisar_con_todo_en_orden_guarda_y_pasa_a_la_revision(self):
        respuesta = self.client.post(self._url("revisar"), {self._nombre("Asunto"): "Necesito algo"})
        self.assertRedirects(respuesta, self._url("revisar"))
        self.assertEqual(self._respuesta("Asunto").valor_texto, "Necesito algo")
        self.assertEqual(Ticket.objects.get(pk=self.ticket.pk).estado, Ticket.Estado.BORRADOR)  # aún no se envía

    def test_la_revision_muestra_solo_lo_aplicable_y_en_lenguaje_humano(self):
        self.client.post(
            self._url("revisar"),
            {self._nombre("Asunto"): "Necesito algo", self._nombre("Prioridad"): "baja"},
        )
        respuesta = self.client.get(self._url("revisar"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud_revision.html")
        filas = {f["etiqueta"]: f for f in respuesta.context["resumen"]}
        self.assertEqual(filas["Asunto"]["texto"], "Necesito algo")
        self.assertEqual(filas["Prioridad"]["texto"], "Baja")
        self.assertNotIn("Detalle", filas)
        self.assertContains(respuesta, "Revisa tu solicitud")
        self.assertContains(respuesta, "Volver y editar")
        self.assertContains(respuesta, "Solicitar")
        self.assertContains(respuesta, reverse("tickets:enviar", args=[self.ticket.pk]))
        self.assertNotContains(respuesta, "campo_")  # ni ids ni nombres internos

    def test_volver_y_editar_recupera_lo_escrito(self):
        self.client.post(self._url("revisar"), {self._nombre("Asunto"): "Se conserva"})
        items = _items_por_etiqueta(self.client.get(self._url("borrador")))
        self.assertEqual(items["Asunto"]["valor"], "Se conserva")

    def test_revisar_por_get_con_pendientes_vuelve_al_formulario_con_errores(self):
        respuesta = self.client.get(self._url("revisar"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud.html")
        self.assertTrue(_items_por_etiqueta(respuesta)["Asunto"]["errores"])

    def test_revisar_es_solo_del_propietario(self):
        self.client.logout()
        self.client.login(username="sol_ajeno", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(self._url("revisar")).status_code, 403)
        self.assertEqual(self.client.post(self._url("revisar"), {}).status_code, 403)

    # --- adjuntos ---

    def test_un_adjunto_valido_se_guarda_se_ve_y_llega_a_la_revision(self):
        archivo = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 contenido", content_type="application/pdf")
        respuesta = self.client.post(
            self._url("revisar"), {self._nombre("Asunto"): "Con adjunto", self._nombre("Adjunto"): archivo}
        )
        self.assertRedirects(respuesta, self._url("revisar"))
        self.assertEqual(self._respuesta("Adjunto").archivo.nombre_original, "brief.pdf")
        filas = {f["etiqueta"]: f for f in self.client.get(self._url("revisar")).context["resumen"]}
        self.assertEqual(filas["Adjunto"]["valores"], ["brief.pdf"])

    def test_un_adjunto_guardado_reaparece_con_su_descarga_en_el_formulario(self):
        archivo = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 contenido", content_type="application/pdf")
        self.client.post(self._url("borrador"), {self._nombre("Adjunto"): archivo})
        item = _items_por_etiqueta(self.client.get(self._url("borrador")))["Adjunto"]
        self.assertEqual(item["archivo"]["nombre"], "brief.pdf")
        self.assertEqual(
            item["archivo"]["url"], reverse("tickets:descargar_archivo", args=[self._respuesta("Adjunto").archivo.pk])
        )

    def test_un_adjunto_con_extension_no_permitida_se_rechaza_en_su_campo(self):
        archivo = SimpleUploadedFile("malo.exe", b"MZ", content_type="application/octet-stream")
        respuesta = self.client.post(
            self._url("borrador"), {self._nombre("Asunto"): "Sigue", self._nombre("Adjunto"): archivo}
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("Extensión no permitida", " ".join(_items_por_etiqueta(respuesta)["Adjunto"]["errores"]))
        self.assertIsNone(self._respuesta("Adjunto"))
        self.assertEqual(self._respuesta("Asunto").valor_texto, "Sigue")

    def test_un_adjunto_vacio_se_rechaza_en_su_campo(self):
        archivo = SimpleUploadedFile("vacio.pdf", b"", content_type="application/pdf")
        respuesta = self.client.post(self._url("borrador"), {self._nombre("Adjunto"): archivo})
        self.assertTrue(_items_por_etiqueta(respuesta)["Adjunto"]["errores"])
        self.assertIsNone(self._respuesta("Adjunto"))

    def test_quitar_un_adjunto_guardado(self):
        archivo = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 contenido", content_type="application/pdf")
        self.client.post(self._url("borrador"), {self._nombre("Adjunto"): archivo})
        self.client.post(self._url("borrador"), {f"{self._nombre('Adjunto')}__eliminar": "1"})
        self.assertIsNone(self._respuesta("Adjunto"))


class SolicitudPrecedenciaDeErroresTests(_EscenarioSolicitudMixin, TestCase):
    def setUp(self):
        self._preparar([{"tipo": Campo.TipoCampo.NUMERO, "etiqueta": "Monto", "obligatorio": True}])

    def test_el_error_de_formato_prevalece_sobre_el_de_obligatoriedad(self):
        # El campo quedó sin guardar, así que además figuraría como pendiente: se muestra el útil.
        respuesta = self.client.post(self._url("revisar"), {self._nombre("Monto"): "abc"})
        errores = _items_por_etiqueta(respuesta)["Monto"]["errores"]
        self.assertNotIn("Este campo es obligatorio.", errores)
        self.assertEqual(len(errores), 1)


class SolicitudEnvioTests(_EscenarioSolicitudMixin, TestCase):
    """Solicitar = la radicación REAL (`operaciones.radicar_ticket`) + confirmación."""

    def setUp(self):
        self._preparar()
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Asunto"].id: "Listo para solicitar"})

    def test_enviar_radica_de_verdad_y_lleva_a_la_confirmacion(self):
        respuesta = self.client.post(self._url("enviar"))
        self.assertRedirects(respuesta, self._url("enviada"))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsNotNone(self.ticket.radicado)
        self.assertIsNotNone(self.ticket.radicado_en)
        self.assertEqual(
            HistorialTicket.objects.filter(ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.RADICADO).count(), 1
        )

    def test_la_confirmacion_muestra_solo_datos_reales_del_ticket(self):
        self.client.post(self._url("enviar"))
        self.ticket.refresh_from_db()
        respuesta = self.client.get(self._url("enviada"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud_enviada.html")
        # 4.F2: confirmación con el código corto (no el UUID) y los dos destinos del solicitante.
        self.assertContains(respuesta, "Ticket creado")
        self.assertContains(respuesta, self.ticket.codigo)
        self.assertNotContains(respuesta, str(self.ticket.radicado))
        self.assertContains(respuesta, self.servicio.nombre)
        self.assertContains(respuesta, reverse("tickets:seguimiento", args=[self.ticket.pk]))
        self.assertContains(respuesta, reverse("tickets:mis_tickets"))

    def test_un_doble_envio_no_falla_ni_radica_dos_veces(self):
        self.client.post(self._url("enviar"))
        segundo = self.client.post(self._url("enviar"))
        self.assertRedirects(segundo, self._url("enviada"))
        self.assertEqual(
            HistorialTicket.objects.filter(ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.RADICADO).count(), 1
        )

    def test_enviar_con_obligatorios_pendientes_no_radica_y_vuelve_al_formulario(self):
        self._respuesta("Asunto").delete()
        respuesta = self.client.post(self._url("enviar"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/solicitud.html")
        self.assertEqual(_items_por_etiqueta(respuesta)["Asunto"]["errores"], ["Este campo es obligatorio."])
        self.assertEqual(Ticket.objects.get(pk=self.ticket.pk).estado, Ticket.Estado.BORRADOR)

    def test_enviar_con_el_servicio_desactivado_no_radica_y_explica_por_que(self):
        self.servicio.activo = False
        self.servicio.save()
        respuesta = self.client.post(self._url("enviar"))
        self.assertRedirects(respuesta, self._url("revisar"))
        self.assertEqual(Ticket.objects.get(pk=self.ticket.pk).estado, Ticket.Estado.BORRADOR)
        self.assertIn("ya no está activo", " ".join(str(m) for m in get_messages(respuesta.wsgi_request)))
        pantalla = self.client.get(self._url("revisar"))
        self.assertFalse(pantalla.context["servicio_activo"])

    def test_enviar_solo_acepta_post(self):
        self.assertEqual(self.client.get(self._url("enviar")).status_code, 405)

    def test_un_ajeno_no_puede_enviar_ni_ver_la_confirmacion(self):
        self.client.post(self._url("enviar"))
        self.client.logout()
        self.client.login(username="sol_ajeno", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.post(self._url("enviar")).status_code, 403)
        self.assertEqual(self.client.get(self._url("enviada")).status_code, 403)

    def test_la_confirmacion_de_un_borrador_vuelve_al_formulario(self):
        self.assertRedirects(self.client.get(self._url("enviada")), self._url("borrador"))

    def test_el_endpoint_directo_de_radicar_conserva_su_contrato(self):
        respuesta = self.client.post(self._url("radicar"), {self._nombre("Asunto"): "Por el camino directo"})
        self.assertRedirects(respuesta, self._url("detalle"))
        self.assertEqual(Ticket.objects.get(pk=self.ticket.pk).estado, Ticket.Estado.RADICADO)

    def test_el_detalle_de_un_borrador_sigue_llevando_al_formulario(self):
        self.assertRedirects(self.client.get(self._url("detalle")), self._url("borrador"))

    def test_un_ticket_ya_solicitado_no_vuelve_a_editarse_como_borrador(self):
        self.client.post(self._url("enviar"))
        self.assertRedirects(self.client.get(self._url("borrador")), self._url("detalle"))
        self.assertRedirects(self.client.get(self._url("revisar")), self._url("enviada"))


class SolicitudEnvioPlantillaFasesTests(TestCase):
    """La acción final de solicitud usa el runtime nuevo de fases."""

    def setUp(self):
        from apps.catalogo.configuracion_ejecucion import (
            activar_configuracion_ejecucion,
            agregar_bloque_operativo,
            crear_nueva_version_configuracion,
        )
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version as activar_workflow

        self.usuario = Usuario.objects.create_user(username="sol_fases", password=CLAVE_PRUEBA)
        self.admin = Usuario.objects.create_user(username="admin_fases", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin, "catalogo.administrar")
        self.servicio, self.formulario_version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Asunto", "obligatorio": True}]
        )
        self.workflow = fases_ops.crear_plantilla_fases(self.admin, nombre="Plantilla de diseño")
        self.workflow_version = self.workflow.versiones.get(numero=1)
        self.planeacion = fases_ops.agregar_fase(self.workflow_version, self.admin, nombre="Planeación")
        self.proceso = fases_ops.agregar_fase(self.workflow_version, self.admin, nombre="Proceso")
        self.revision = fases_ops.agregar_fase(self.workflow_version, self.admin, nombre="Revisión")
        self.gaceta = fases_ops.agregar_fase(self.workflow_version, self.admin, nombre="Gaceta")
        self.terminada = fases_ops.agregar_fase(self.workflow_version, self.admin, nombre="Terminada")
        fases_ops.conectar_fases(self.planeacion, self.proceso, self.admin)
        fases_ops.conectar_fases(self.proceso, self.revision, self.admin)
        fases_ops.conectar_fases(self.revision, self.gaceta, self.admin)
        fases_ops.conectar_fases(self.gaceta, self.terminada, self.admin)
        activar_workflow(self.workflow, self.workflow_version, self.admin)
        self.servicio.workflow = self.workflow
        self.servicio.save(update_fields=["workflow", "actualizado_en"])
        self.configuracion = crear_nueva_version_configuracion(self.servicio, self.admin)
        self.bloque_inicial = agregar_bloque_operativo(
            self.configuracion,
            self.admin,
            fase=self.planeacion,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Revisión de documentación",
            configuracion={"tipo_actor": "RESPONSABLE_TICKET"},
        )
        self.bloque_proceso = agregar_bloque_operativo(
            self.configuracion,
            self.admin,
            fase=self.proceso,
            tipo=BloqueOperativo.Tipo.ACTIVIDAD,
            nombre="Preparar el diseño",
            configuracion={"tipo_actor": "SOLICITANTE"},
        )
        activar_configuracion_ejecucion(self.servicio, self.configuracion, self.admin)
        self.ticket = crear_borrador(self.usuario, self.servicio)
        guardar_respuestas_borrador(self.ticket, self.usuario, {self.campos["Asunto"].id: "Diseño de piezas"})
        self.client.login(username="sol_fases", password=CLAVE_PRUEBA)

    def _url(self, nombre):
        return reverse(f"tickets:{nombre}", args=[self.ticket.pk])

    def test_solicitar_congela_versiones_e_inicia_primera_fase_y_bloque(self):
        from apps.workflows.models import InstanciaWorkflow, TareaWorkflow

        respuesta = self.client.post(self._url("enviar"))
        self.assertRedirects(respuesta, self._url("enviada"))
        self.ticket.refresh_from_db()
        instancia = self.ticket.instancia_workflow
        ejecucion = instancia.ejecuciones_etapa.get(orden=1)
        vinculo_tarea = TareaWorkflow.objects.get(instancia_etapa=ejecucion)

        self.assertEqual(self.ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(instancia.workflow_version_id, self.workflow_version.pk)
        self.assertEqual(instancia.configuracion_ejecucion_version_id, self.configuracion.pk)
        self.assertEqual(ejecucion.fase_workflow_id, self.planeacion.pk)
        self.assertEqual(ejecucion.bloque_operativo_id, self.bloque_inicial.pk)
        self.assertEqual(ejecucion.estado, "EN_ESPERA")
        self.assertEqual(vinculo_tarea.tarea.titulo, "Revisión de documentación")
        self.assertIsNone(vinculo_tarea.tarea.usuario_responsable_id)
        self.assertIsNone(vinculo_tarea.tarea.equipo_responsable_id)

        segundo = self.client.post(self._url("enviar"))
        self.assertRedirects(segundo, self._url("enviada"))
        self.assertEqual(InstanciaWorkflow.objects.count(), 1)
        self.assertEqual(TareaWorkflow.objects.count(), 1)
        self.assertEqual(
            HistorialTicket.objects.filter(ticket=self.ticket, tipo_evento=HistorialTicket.TipoEvento.RADICADO).count(),
            1,
        )

    def test_solicitar_exige_configuracion_activa_compatible(self):
        self.configuracion.estado = ConfiguracionEjecucionVersion.Estado.HISTORICA
        self.configuracion.save(update_fields=["estado", "actualizado_en"])
        self.servicio.configuracion_ejecucion_activa = None
        self.servicio.save(update_fields=["configuracion_ejecucion_activa", "actualizado_en"])

        respuesta = self.client.post(self._url("enviar"))
        self.assertRedirects(respuesta, self._url("revisar"))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.BORRADOR)
        self.assertIsNone(self.ticket.instancia_workflow_id)


class SolicitudControlesTests(_EscenarioSolicitudMixin, TestCase):
    """V2.2 — el renderer entrega a cada control lo que el dominio realmente
    configura (restricciones, ayuda, relaciones activas) y marca lo obligatorio
    de forma accesible. Nada de estilos."""

    def setUp(self):
        T = Campo.TipoCampo
        self.area_zeta = Area.objects.create(nombre="Zeta", codigo="SOL-ZETA")
        self.area_alfa = Area.objects.create(nombre="Alfa", codigo="SOL-ALFA")
        self.area_inactiva = Area.objects.create(nombre="Inactiva", codigo="SOL-INACTIVA", activo=False)
        self.inactivo = Usuario.objects.create_user(username="zz_inactivo", password=CLAVE_PRUEBA, is_active=False)
        self._preparar(
            [
                {"tipo": T.NUMERO, "etiqueta": "Monto", "obligatorio": True, "orden": 1,
                 "configuracion": {"minimo": 1, "maximo": 10, "permite_decimales": False}},
                {"tipo": T.NUMERO, "etiqueta": "Precio", "orden": 2,
                 "configuracion": {"permite_decimales": True, "minimo": 0.5}},
                {"tipo": T.FECHA, "etiqueta": "Entrega", "orden": 3,
                 "configuracion": {"fecha_minima": "2026-10-01", "fecha_maxima": "2026-12-31"}},
                {"tipo": T.FECHA_HORA, "etiqueta": "Reunión", "orden": 4,
                 "configuracion": {"fecha_minima": "2026-10-01"}},
                {"tipo": T.CORREO, "etiqueta": "Contacto", "orden": 5},
                {"tipo": T.URL, "etiqueta": "Enlace", "orden": 6},
                {"tipo": T.TEXTO_LARGO, "etiqueta": "Resumen", "ayuda": "Cuéntanos qué necesitas", "orden": 7,
                 "configuracion": {"longitud_maxima": 200}},
                {"tipo": T.TEXTO, "etiqueta": "Título", "orden": 8,
                 "configuracion": {"longitud_maxima": 50, "placeholder": "Ej.: campaña de verano"}},
                {"tipo": T.USUARIO, "etiqueta": "Responsable", "orden": 9},
                {"tipo": T.AREA, "etiqueta": "Área", "orden": 10},
                {"tipo": T.ARCHIVO, "etiqueta": "Adjunto", "orden": 11,
                 "configuracion": {"extensiones_permitidas": ["pdf", "png"], "tamano_maximo_mb": 10}},
                {"tipo": T.LISTA, "etiqueta": "Tipo", "orden": 12,
                 "opciones": [("a", "Uno"), ("b", "Dos"), ("c", "Tres"), ("d", "Cuatro")]},
                {"tipo": T.LISTA, "etiqueta": "Sí o no", "orden": 13, "opciones": [("si", "Sí"), ("no", "No")]},
                {"tipo": T.MULTILISTA, "etiqueta": "Canal", "orden": 14,
                 "opciones": [("x", "X"), ("y", "Y"), ("z", "Z")],
                 "configuracion": {"minimo_selecciones": 1, "maximo_selecciones": 2}},
            ]
        )

    def _abrir(self):
        return self.client.get(self._url("borrador"))

    def test_los_numeros_llevan_sus_restricciones_reales_y_sin_localizar(self):
        respuesta = self._abrir()
        items = _items_por_etiqueta(respuesta)
        monto = items["Monto"]
        self.assertEqual((monto["atributos"]["min"], monto["atributos"]["max"], monto["atributos"]["step"]), ("1", "10", "1"))
        self.assertEqual(monto["pista"], "Entre 1 y 10 · Sin decimales")
        precio = items["Precio"]
        self.assertEqual((precio["atributos"]["min"], precio["atributos"]["step"]), ("0.5", "any"))  # punto, no coma
        self.assertEqual(precio["pista"], "Mínimo 0.5")
        self.assertContains(respuesta, 'min="0.5"')
        self.assertContains(respuesta, 'step="any"')

    def test_las_fechas_llevan_sus_limites_reales(self):
        items = _items_por_etiqueta(self._abrir())
        self.assertEqual((items["Entrega"]["atributos"]["min"], items["Entrega"]["atributos"]["max"]), ("2026-10-01", "2026-12-31"))
        self.assertEqual(items["Entrega"]["pista"], "Entre el 1 de octubre de 2026 y el 31 de diciembre de 2026")
        self.assertEqual(items["Reunión"]["atributos"]["min"], "2026-10-01T00:00")  # igual que lo lee el dominio

    def test_correo_y_url_usan_controles_semanticos_con_aviso_asociado(self):
        respuesta = self._abrir()
        items = _items_por_etiqueta(respuesta)
        self.assertEqual((items["Contacto"]["input_type"], items["Enlace"]["input_type"]), ("email", "url"))
        for etiqueta in ("Contacto", "Enlace", "Monto", "Entrega"):
            self.assertTrue(items[etiqueta]["aviso_vivo"], etiqueta)
            self.assertIn(f"live_{items[etiqueta]['id']}", items[etiqueta]["describedby"])
        self.assertContains(respuesta, f'id="live_{self.campos["Contacto"].id}"')

    def test_el_texto_largo_con_limite_muestra_contador_y_no_usa_maxlength(self):
        respuesta = self._abrir()
        items = _items_por_etiqueta(respuesta)
        resumen, titulo = items["Resumen"], items["Título"]
        self.assertEqual(resumen["limite"], 200)
        self.assertNotIn("maxlength", resumen["atributos"])  # el salto de línea cuenta distinto en navegador y servidor
        self.assertContains(respuesta, 'data-sol-limite="200"')
        self.assertContains(respuesta, f'id="count_{resumen["id"]}"')
        self.assertEqual(titulo["atributos"]["maxlength"], 50)  # una línea: sin ambigüedad
        self.assertEqual(titulo["atributos"]["placeholder"], "Ej.: campaña de verano")
        self.assertIsNone(titulo["limite"])

    def test_la_ayuda_y_la_pista_se_asocian_al_control_solo_si_existen(self):
        items = _items_por_etiqueta(self._abrir())
        self.assertEqual(items["Resumen"]["describedby"], f"help_{items['Resumen']['id']} count_{items['Resumen']['id']}")
        self.assertEqual(items["Título"]["describedby"], "")
        self.assertEqual(items["Monto"]["describedby"], f"hint_{items['Monto']['id']} live_{items['Monto']['id']}")

    def test_lo_obligatorio_se_comunica_con_texto_y_con_aria(self):
        respuesta = self._abrir()
        self.assertContains(respuesta, "Obligatorio")
        self.assertNotContains(respuesta, 'aria-hidden="true">*')
        html = respuesta.content.decode()
        bloque = html.split(f'id="campo_{self.campos["Monto"].id}"')[1].split(">")[0]
        self.assertIn('aria-required="true"', bloque)
        self.assertEqual(_items_por_etiqueta(respuesta)["Monto"]["requerido"], True)

    def test_un_campo_obligatorio_completo_se_marca_como_completado(self):
        guardar_respuestas_borrador(
            self.ticket, self.usuario, {self.campos["Monto"].id: "5", self.campos["Título"].id: "Algo"}
        )
        items = _items_por_etiqueta(self._abrir())
        self.assertTrue(items["Monto"]["completado"])
        self.assertFalse(items["Título"]["completado"])  # completo pero no obligatorio

    def test_las_tarjetas_se_reparten_segun_la_cantidad_de_opciones(self):
        respuesta = self._abrir()
        items = _items_por_etiqueta(respuesta)
        self.assertEqual((items["Tipo"]["control"], items["Tipo"]["columnas"]), ("tarjetas", 2))  # 4 → 2×2
        self.assertEqual(items["Sí o no"]["columnas"], 2)
        self.assertContains(respuesta, "sol-choices--c2")

    def test_las_opciones_multiples_muestran_su_rango_real(self):
        items = _items_por_etiqueta(self._abrir())
        self.assertEqual((items["Canal"]["control"], items["Canal"]["pista"]), ("chips", "Elige entre 1 y 2"))

    def test_el_adjunto_solo_anuncia_las_restricciones_configuradas(self):
        respuesta = self._abrir()
        item = _items_por_etiqueta(respuesta)["Adjunto"]
        self.assertEqual(item["atributos"]["accept"], ".pdf,.png")
        self.assertContains(respuesta, "PDF, PNG · máximo 10 MB")
        self.assertContains(respuesta, "Arrastra tu archivo aquí")  # un solo archivo: el campo no admite varios
        self.assertNotContains(respuesta, " multiple")

    def test_las_referencias_solo_listan_registros_activos_y_ordenados(self):
        items = _items_por_etiqueta(self._abrir())
        areas = [o["etiqueta"] for o in items["Área"]["opciones"]]
        self.assertNotIn("Inactiva", areas)
        self.assertLessEqual({"Alfa", "Zeta"}, set(areas))
        self.assertEqual(areas, sorted(areas, key=str.casefold))
        usuarios = [o["etiqueta"] for o in items["Responsable"]["opciones"]]
        self.assertNotIn("zz_inactivo", usuarios)
        self.assertEqual(usuarios, sorted(usuarios, key=str.casefold))

    def test_una_referencia_inactiva_se_rechaza_en_su_campo(self):
        respuesta = self.client.post(self._url("borrador"), {self._nombre("Área"): str(self.area_inactiva.pk)})
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(_items_por_etiqueta(respuesta)["Área"]["errores"])
        self.assertIsNone(self._respuesta("Área"))

    def test_los_valores_fuera_de_rango_o_con_formato_invalido_se_rechazan_en_su_campo(self):
        for etiqueta, valor in (("Monto", "11"), ("Monto", "5.5"), ("Monto", "0"), ("Contacto", "no-es-correo"), ("Enlace", "sin-esquema")):
            respuesta = self.client.post(self._url("borrador"), {self._nombre(etiqueta): valor})
            item = _items_por_etiqueta(respuesta)[etiqueta]
            self.assertTrue(item["errores"], (etiqueta, valor))
            self.assertEqual(item["valor"], valor)  # se conserva lo escrito
            self.assertIsNone(self._respuesta(etiqueta))

    def test_un_valor_valido_de_cada_familia_se_guarda_y_vuelve_al_control(self):
        self.client.post(
            self._url("borrador"),
            {
                self._nombre("Monto"): "7",
                self._nombre("Precio"): "12.5",
                self._nombre("Entrega"): "2026-11-15",
                self._nombre("Contacto"): "prueba@ejemplo.com",
                self._nombre("Enlace"): "https://ejemplo.com/ref",
                self._nombre("Tipo"): "b",
                self._nombre("Canal"): ["x", "z"],
                self._nombre("Área"): str(self.area_alfa.pk),
            },
        )
        items = _items_por_etiqueta(self._abrir())
        self.assertEqual(items["Monto"]["valor"], "7")
        self.assertEqual(items["Precio"]["valor"], "12.5")
        self.assertEqual(items["Entrega"]["valor"], "2026-11-15")
        self.assertEqual(items["Contacto"]["valor"], "prueba@ejemplo.com")
        self.assertEqual([o["valor"] for o in items["Tipo"]["opciones"] if o["seleccionada"]], ["b"])
        self.assertEqual(items["Canal"]["seleccion"], ["x", "z"])
        self.assertEqual([o["etiqueta"] for o in items["Área"]["opciones"] if o["seleccionada"]], ["Alfa"])


# --- 4.A1 — compromiso temporal ------------------------------------------------


def _bogota():
    import zoneinfo

    return timezone.override(zoneinfo.ZoneInfo("America/Bogota"))


def _local(anio, mes, dia, hora=0, minuto=0):
    import datetime
    import zoneinfo

    return datetime.datetime(anio, mes, dia, hora, minuto, tzinfo=zoneinfo.ZoneInfo("America/Bogota"))


class TiemposCalculoTests(TestCase):
    """`apps.tickets.tiempos` — hábil = lunes a viernes, sin festivos. Octubre
    2026: 5 lun · 9 vie · 10 sáb · 11 dom · 12 lun · 13 mar."""

    def setUp(self):
        from apps.tickets import tiempos

        self.t = tiempos
        contexto = _bogota()
        contexto.__enter__()
        self.addCleanup(contexto.__exit__, None, None, None)

    def _calc(self, desde, cantidad, unidad, habiles):
        return self.t.calcular_fecha_objetivo(desde, cantidad, unidad, habiles)

    def test_dias_habiles_dentro_de_la_semana(self):
        self.assertEqual(self._calc(_local(2026, 10, 5, 9, 30), 3, "DIAS", True), _local(2026, 10, 8, 9, 30))

    def test_dias_habiles_cruzan_el_fin_de_semana(self):
        self.assertEqual(self._calc(_local(2026, 10, 9, 10), 1, "DIAS", True), _local(2026, 10, 12, 10))
        self.assertEqual(self._calc(_local(2026, 10, 7, 16, 45), 3, "DIAS", True), _local(2026, 10, 12, 16, 45))
        self.assertEqual(self._calc(_local(2026, 10, 5, 9), 5, "DIAS", True), _local(2026, 10, 12, 9))
        self.assertEqual(self._calc(_local(2026, 10, 5, 9), 10, "DIAS", True), _local(2026, 10, 19, 9))

    def test_inicio_en_fin_de_semana_cuenta_desde_el_lunes(self):
        self.assertEqual(self._calc(_local(2026, 10, 10, 10), 1, "DIAS", True), _local(2026, 10, 12, 10))
        self.assertEqual(self._calc(_local(2026, 10, 11, 15), 2, "DIAS", True), _local(2026, 10, 13, 15))

    def test_dias_corridos_no_saltan_el_fin_de_semana(self):
        self.assertEqual(self._calc(_local(2026, 10, 9, 10), 2, "DIAS", False), _local(2026, 10, 11, 10))

    def test_horas_corridas_son_horas_reloj(self):
        self.assertEqual(self._calc(_local(2026, 10, 9, 22), 5, "HORAS", False), _local(2026, 10, 10, 3))

    def test_horas_habiles_no_corren_sabado_ni_domingo(self):
        self.assertEqual(self._calc(_local(2026, 10, 5, 8), 4, "HORAS", True), _local(2026, 10, 5, 12))
        self.assertEqual(self._calc(_local(2026, 10, 9, 22), 5, "HORAS", True), _local(2026, 10, 12, 3))
        self.assertEqual(self._calc(_local(2026, 10, 9, 12), 48, "HORAS", True), _local(2026, 10, 13, 12))

    def test_horas_habiles_con_inicio_en_fin_de_semana_arrancan_el_lunes(self):
        self.assertEqual(self._calc(_local(2026, 10, 10, 10), 2, "HORAS", True), _local(2026, 10, 12, 2))
        self.assertEqual(self._calc(_local(2026, 10, 11, 23), 1, "HORAS", True), _local(2026, 10, 12, 1))

    def test_el_resultado_es_aware_y_en_la_zona_activa(self):
        resultado = self._calc(_local(2026, 10, 9, 10), 1, "DIAS", True)
        self.assertTrue(timezone.is_aware(resultado))
        self.assertEqual(timezone.localtime(resultado).hour, 10)

    def test_datos_invalidos_se_rechazan(self):
        desde = _local(2026, 10, 5, 9)
        for cantidad, unidad in ((0, "DIAS"), (-1, "DIAS"), (1000, "DIAS"), (True, "DIAS"), (1.5, "DIAS"), (3, "SEMANAS"), (3, "")):
            with self.subTest(cantidad=cantidad, unidad=unidad), self.assertRaises(ValueError):
                self.t.calcular_fecha_objetivo(desde, cantidad, unidad, True)
        import datetime

        with self.assertRaises(ValueError):
            self.t.calcular_fecha_objetivo(datetime.datetime(2026, 10, 5, 9), 1, "DIAS", False)

    def test_fecha_objetivo_de_sin_compromiso_es_none(self):
        class _SinCompromiso:
            tiempo_objetivo_cantidad = None

        self.assertIsNone(self.t.fecha_objetivo_de(_SinCompromiso(), _local(2026, 10, 5, 9)))


class CompromisoTemporalTicketTests(TestCase):
    """4.A1 — snapshot del tiempo objetivo, fechas original/vigente y su
    protección, para Servicio y Proceso."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user("tiempo41", password=CLAVE_PRUEBA)
        self.servicio, self.version, self.campos = _crear_servicio_con_formulario(
            self.usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Texto", "obligatorio": True}]
        )

    def _configurar(self, servicio=None, **tiempo):
        servicio = servicio or self.servicio
        for campo, valor in tiempo.items():
            setattr(servicio, f"tiempo_objetivo_{campo}", valor)
        servicio.save()

    def _radicado(self, servicio=None):
        ticket = crear_borrador(self.usuario, servicio or self.servicio)
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        return ticket

    def test_servicio_sin_tiempo_radica_sin_fechas_objetivo(self):
        ticket = self._radicado()
        self.assertEqual(
            (ticket.tiempo_objetivo_cantidad, ticket.tiempo_objetivo_unidad, ticket.tiempo_objetivo_habiles),
            (None, "", False),
        )
        self.assertIsNone(ticket.fecha_objetivo_original)
        self.assertIsNone(ticket.fecha_objetivo_vigente)

    def test_el_borrador_congela_el_tiempo_del_servicio_y_aun_no_tiene_fechas(self):
        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        ticket = crear_borrador(self.usuario, self.servicio)
        ticket.refresh_from_db()
        self.assertEqual(
            (ticket.tiempo_objetivo_cantidad, ticket.tiempo_objetivo_unidad, ticket.tiempo_objetivo_habiles),
            (3, "DIAS", True),
        )
        self.assertIsNone(ticket.fecha_objetivo_original)
        self.assertIsNone(ticket.fecha_objetivo_vigente)

    def test_radicar_fija_original_y_vigente_con_el_calculo_de_dominio(self):
        from apps.tickets import tiempos

        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        with _bogota():
            ticket = self._radicado()
            esperado = tiempos.calcular_fecha_objetivo(ticket.radicado_en, 3, "DIAS", True)
        self.assertEqual(ticket.fecha_objetivo_original, esperado)
        self.assertEqual(ticket.fecha_objetivo_vigente, esperado)
        self.assertGreater(ticket.fecha_objetivo_original, ticket.radicado_en)

    def test_horas_corridas_se_suman_al_instante_de_radicacion(self):
        self._configurar(cantidad=4, unidad="HORAS", habiles=False)
        ticket = self._radicado()
        self.assertEqual(ticket.fecha_objetivo_original, ticket.radicado_en + timedelta(hours=4))

    def test_cambiar_el_servicio_despues_no_altera_tickets_existentes(self):
        self._configurar(cantidad=2, unidad="DIAS", habiles=True)
        borrador = crear_borrador(self.usuario, self.servicio)
        radicado = self._radicado()
        original = radicado.fecha_objetivo_original
        self._configurar(cantidad=30, unidad="HORAS", habiles=False)
        borrador.refresh_from_db()
        radicado.refresh_from_db()
        self.assertEqual((borrador.tiempo_objetivo_cantidad, borrador.tiempo_objetivo_unidad), (2, "DIAS"))
        self.assertEqual((radicado.tiempo_objetivo_cantidad, radicado.tiempo_objetivo_unidad), (2, "DIAS"))
        self.assertEqual(radicado.fecha_objetivo_original, original)
        # El borrador existente se radica con SU snapshot, no con el nuevo.
        _completar_texto(borrador, self.usuario, self.campos)
        radicar_ticket(borrador, self.usuario)
        borrador.refresh_from_db()
        self.assertEqual((borrador.tiempo_objetivo_cantidad, borrador.tiempo_objetivo_unidad), (2, "DIAS"))
        self.assertIsNotNone(borrador.fecha_objetivo_original)

    def test_quitar_el_tiempo_del_servicio_no_borra_el_de_un_ticket_existente(self):
        self._configurar(cantidad=2, unidad="DIAS", habiles=False)
        ticket = self._radicado()
        self._configurar(cantidad=None, unidad="", habiles=False)
        ticket.refresh_from_db()
        self.assertEqual(ticket.tiempo_objetivo_cantidad, 2)
        self.assertIsNotNone(ticket.fecha_objetivo_original)

    def test_ticket_historico_sin_snapshot_no_se_recalcula_ni_inventa_fechas(self):
        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        ticket = crear_borrador(self.usuario, self.servicio)
        # Simula un Ticket anterior a 4.A1: sin snapshot aunque el Servicio hoy tenga tiempo.
        Ticket.objects.filter(pk=ticket.pk).update(
            tiempo_objetivo_cantidad=None, tiempo_objetivo_unidad="", tiempo_objetivo_habiles=False
        )
        ticket.refresh_from_db()
        _completar_texto(ticket, self.usuario, self.campos)
        radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsNone(ticket.tiempo_objetivo_cantidad)
        self.assertIsNone(ticket.fecha_objetivo_original)
        self.assertIsNone(ticket.fecha_objetivo_vigente)
        ticket.save()  # guardar un histórico sigue siendo posible
        ticket.refresh_from_db()
        self.assertIsNone(ticket.fecha_objetivo_original)

    def test_la_fecha_original_no_se_puede_modificar_ni_borrar(self):
        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        ticket = self._radicado()
        original = ticket.fecha_objetivo_original
        ticket.fecha_objetivo_original = original + timedelta(days=1)
        with self.assertRaises(ValidationError):
            ticket.save()
        ticket.fecha_objetivo_original = None
        ticket.fecha_objetivo_vigente = None
        with self.assertRaises(ValidationError):
            ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.fecha_objetivo_original, original)

    def test_solo_la_vigente_puede_moverse_y_la_original_se_conserva(self):
        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        ticket = self._radicado()
        original = ticket.fecha_objetivo_original
        ticket.fecha_objetivo_vigente = original + timedelta(days=2)
        ticket.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.fecha_objetivo_original, original)
        self.assertEqual(ticket.fecha_objetivo_vigente, original + timedelta(days=2))

    def test_el_snapshot_temporal_no_se_puede_modificar(self):
        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        ticket = crear_borrador(self.usuario, self.servicio)
        for campo, valor in (
            ("tiempo_objetivo_cantidad", 9), ("tiempo_objetivo_unidad", "HORAS"), ("tiempo_objetivo_habiles", False),
        ):
            with self.subTest(campo=campo):
                ticket.refresh_from_db()
                setattr(ticket, campo, valor)
                with self.assertRaises(ValidationError):
                    ticket.save()

    def test_la_base_rechaza_fechas_incoherentes(self):
        self._configurar(cantidad=3, unidad="DIAS", habiles=True)
        ticket = self._radicado()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.filter(pk=ticket.pk).update(fecha_objetivo_vigente=None)
        sin_compromiso = Ticket.objects.create(solicitante=self.usuario)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.filter(pk=sin_compromiso.pk).update(
                fecha_objetivo_original=timezone.now(), fecha_objetivo_vigente=timezone.now()
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.filter(pk=sin_compromiso.pk).update(tiempo_objetivo_cantidad=0, tiempo_objetivo_unidad="DIAS")

    def test_un_proceso_congela_y_calcula_igual_que_un_servicio(self):
        from apps.workflows.models import ConfiguracionEtapaTarea, Etapa, TransicionEtapa, Workflow, WorkflowVersion
        from apps.workflows.versionamiento import activar_version as activar_workflow

        workflow = Workflow.objects.create(nombre="Ejecución 4.A1")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo="INICIO", nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo="TAREA", nombre="Trabajar")
        fin = Etapa.objects.create(version=version, tipo="FIN", nombre="Fin")
        ConfiguracionEtapaTarea.objects.create(etapa=tarea)
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        activar_workflow(workflow, version, self.usuario)
        self.servicio.tipo = Servicio.Tipo.PROCESO
        self.servicio.workflow = workflow
        self._configurar(cantidad=2, unidad="DIAS", habiles=True)
        ticket = self._radicado()
        self.assertEqual(ticket.tipo, "PROCESO")
        self.assertEqual((ticket.tiempo_objetivo_cantidad, ticket.tiempo_objetivo_unidad), (2, "DIAS"))
        self.assertTrue(ticket.tiempo_objetivo_habiles)
        self.assertIsNotNone(ticket.fecha_objetivo_original)
        self.assertEqual(ticket.fecha_objetivo_original, ticket.fecha_objetivo_vigente)


# --- 4.A2 — prórrogas de la fecha objetivo -------------------------------------


class _EscenarioProrrogaMixin:
    """Servicio con tiempo objetivo (5 días corridos) y política de prórroga, y un
    Ticket EN_ATENCION: responsable individual + un compañero de su equipo; un
    gestor con `tickets.atender` GLOBAL que NO es responsable del ticket; un
    solicitante y un ajeno."""

    def _preparar_prorroga(self, politica="SIN_APROBACION", aprobador="usuario", tiempo=True):
        self.solicitante_p = Usuario.objects.create_user("solicitante_p", password=CLAVE_PRUEBA)
        self.responsable_p = Usuario.objects.create_user("responsable_p", password=CLAVE_PRUEBA)
        self.companero_p = Usuario.objects.create_user("companero_p", password=CLAVE_PRUEBA)
        self.gestor_p = Usuario.objects.create_user("gestor_p", password=CLAVE_PRUEBA)
        self.ajeno_p = Usuario.objects.create_user("ajeno_p", password=CLAVE_PRUEBA)
        self.aprobador_p = Usuario.objects.create_user("aprobador_p", password=CLAVE_PRUEBA)
        self.miembro_aprobador_p = Usuario.objects.create_user("miembro_aprobador_p", password=CLAVE_PRUEBA)
        self.equipo_p = Equipo.objects.create(nombre="Equipo prórroga")
        self.equipo_aprobador_p = Equipo.objects.create(nombre="Equipo aprobador prórroga")
        MiembroEquipo.objects.create(equipo=self.equipo_aprobador_p, usuario=self.miembro_aprobador_p)
        for usuario in (self.responsable_p, self.companero_p):
            MiembroEquipo.objects.create(equipo=self.equipo_p, usuario=usuario)
            _otorgar_tickets_atender(usuario)
        _otorgar_tickets_atender(self.gestor_p)

        self.servicio_p, _, _ = _crear_servicio_con_formulario(self.solicitante_p, [])
        self._configurar_servicio_p(politica=politica, aprobador=aprobador, tiempo=tiempo)
        self.ticket_p = crear_borrador(self.solicitante_p, self.servicio_p)
        radicar_ticket(self.ticket_p, self.solicitante_p)
        self.ticket_p = asignar_ticket(self.ticket_p, self.responsable_p, equipo=self.equipo_p)
        self.ticket_p = tomar_ticket(self.ticket_p, self.responsable_p)

    def _configurar_servicio_p(self, *, politica, aprobador="usuario", tiempo=True):
        Servicio.objects.filter(pk=self.servicio_p.pk).update(
            tiempo_objetivo_cantidad=5 if tiempo else None,
            tiempo_objetivo_unidad="DIAS" if tiempo else "",
            tiempo_objetivo_habiles=False,
            politica_prorroga=politica,
            prorroga_aprobador_usuario=self.aprobador_p if politica == "CON_APROBACION" and aprobador == "usuario" else None,
            prorroga_aprobador_equipo=(
                self.equipo_aprobador_p if politica == "CON_APROBACION" and aprobador == "equipo" else None
            ),
        )
        self.servicio_p.refresh_from_db()

    def _ticket(self):
        return Ticket.objects.get(pk=self.ticket_p.pk)

    def _nueva_fecha(self, dias=3, base=None):
        return (base or self._ticket().fecha_objetivo_vigente) + timedelta(days=dias)

    def _solicitar(self, actor=None, dias=3, motivo="Necesito más tiempo para terminar"):
        from apps.tickets import prorrogas

        return prorrogas.solicitar_prorroga(
            self._ticket(), actor or self.responsable_p, nueva_fecha=self._nueva_fecha(dias), motivo=motivo
        )

    def _aprobacion(self, prorroga):
        return prorroga.esquema_aprobacion.participaciones.get()

    def _resolver(self, prorroga, actor=None, decision="APROBADA", observacion=""):
        from apps.tickets import prorrogas

        return prorrogas.resolver_prorroga_por_aprobacion(
            self._aprobacion(prorroga), actor or self.aprobador_p, decision=decision, observacion=observacion
        )


class ProrrogaSnapshotTests(_EscenarioProrrogaMixin, TestCase):
    def test_el_ticket_congela_politica_y_aprobador_al_crear_el_borrador(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        ticket = self._ticket()
        self.assertEqual(ticket.prorroga_politica, "CON_APROBACION")
        self.assertEqual(ticket.prorroga_aprobador_usuario_id, self.aprobador_p.pk)
        self.assertIsNone(ticket.prorroga_aprobador_equipo_id)

    def test_cambiar_el_servicio_despues_no_altera_tickets_existentes(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        self._configurar_servicio_p(politica="NO_PERMITE")
        ticket = self._ticket()
        self.assertEqual(ticket.prorroga_politica, "CON_APROBACION")
        self.assertEqual(ticket.prorroga_aprobador_usuario_id, self.aprobador_p.pk)
        # ...y sigue pudiendo pedir prórroga con la política con que nació.
        self.assertEqual(self._solicitar().estado, "PENDIENTE")

    def test_un_servicio_sin_politica_deja_el_ticket_sin_politica_y_no_permite_prorrogas(self):
        from apps.tickets.autorizacion import motivo_no_elegible_para_prorroga

        self._preparar_prorroga(politica="")
        self.assertEqual(self._ticket().prorroga_politica, "")
        self.assertIn("no permite prórrogas", motivo_no_elegible_para_prorroga(self._ticket()))

    def test_la_politica_congelada_no_se_puede_modificar(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        ticket = self._ticket()
        ticket.prorroga_politica = "NO_PERMITE"
        with self.assertRaises(ValidationError):
            ticket.save()
        ticket.refresh_from_db()
        ticket.prorroga_aprobador_usuario = self.aprobador_p
        with self.assertRaises(ValidationError):
            ticket.save()

    def test_la_base_rechaza_politicas_incoherentes_en_el_ticket(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        for cambios in (
            {"prorroga_politica": "CON_APROBACION"},
            {"prorroga_politica": "SIN_APROBACION", "prorroga_aprobador_usuario": self.aprobador_p},
            {
                "prorroga_politica": "CON_APROBACION",
                "prorroga_aprobador_usuario": self.aprobador_p, "prorroga_aprobador_equipo": self.equipo_aprobador_p,
            },
        ):
            with self.subTest(cambios=cambios), self.assertRaises(IntegrityError), transaction.atomic():
                Ticket.objects.filter(pk=self.ticket_p.pk).update(**cambios)

    def test_ticket_historico_sin_politica_sigue_funcionando(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        Ticket.objects.filter(pk=self.ticket_p.pk).update(prorroga_politica="")
        ticket = self._ticket()
        ticket.save()  # guardar un histórico sigue siendo posible
        self.assertEqual(ticket.prorrogas.count(), 0)
        with self.assertRaises(ValidationError):
            self._solicitar()


class SolicitarProrrogaTests(_EscenarioProrrogaMixin, TestCase):
    """Reglas de solicitud con política SIN_APROBACION (la más simple)."""

    def setUp(self):
        self._preparar_prorroga(politica="SIN_APROBACION")

    def test_el_responsable_solicita_y_se_aplica_sin_tocar_la_original(self):
        antes = self._ticket()
        prorroga = self._solicitar(dias=3)
        despues = self._ticket()
        self.assertEqual(prorroga.estado, ProrrogaTicket.Estado.APROBADA)
        self.assertEqual(prorroga.politica, "SIN_APROBACION")
        self.assertEqual(prorroga.numero, 1)
        self.assertIsNone(prorroga.resuelta_por)
        self.assertIsNotNone(prorroga.resuelta_en)
        self.assertIsNone(prorroga.esquema_aprobacion)
        self.assertEqual(prorroga.fecha_objetivo_vigente_al_solicitar, antes.fecha_objetivo_vigente)
        self.assertEqual(despues.fecha_objetivo_original, antes.fecha_objetivo_original)
        self.assertEqual(despues.fecha_objetivo_vigente, antes.fecha_objetivo_vigente + timedelta(days=3))

    def test_un_miembro_del_equipo_responsable_tambien_puede(self):
        prorroga = self._solicitar(actor=self.companero_p)
        self.assertEqual(prorroga.solicitada_por, self.companero_p)

    def test_quien_no_atiende_el_ticket_no_puede(self):
        from apps.tickets import prorrogas

        for usuario in (self.solicitante_p, self.ajeno_p, self.gestor_p):
            with self.subTest(usuario=usuario.username), self.assertRaises(PermissionDenied):
                prorrogas.solicitar_prorroga(
                    self._ticket(), usuario, nueva_fecha=self._nueva_fecha(), motivo="Quiero más tiempo"
                )
        self.assertEqual(ProrrogaTicket.objects.count(), 0)

    def test_solo_se_solicita_con_el_ticket_en_atencion(self):
        for estado in (
            Ticket.Estado.RADICADO, Ticket.Estado.RESUELTO, Ticket.Estado.CERRADO, Ticket.Estado.CANCELADO
        ):
            with self.subTest(estado=estado):
                Ticket.objects.filter(pk=self.ticket_p.pk).update(estado=estado)
                with self.assertRaises(ValidationError) as contexto:
                    self._solicitar()
                self.assertIn("en atención", contexto.exception.messages[0])
        self.assertEqual(ProrrogaTicket.objects.count(), 0)

    def test_un_ticket_sin_compromiso_temporal_no_admite_prorroga(self):
        Ticket.objects.filter(pk=self.ticket_p.pk).update(
            fecha_objetivo_original=None, fecha_objetivo_vigente=None,
            tiempo_objetivo_cantidad=None, tiempo_objetivo_unidad="",
        )
        with self.assertRaises(ValidationError) as contexto:
            from apps.tickets import prorrogas

            prorrogas.solicitar_prorroga(
                self._ticket(), self.responsable_p, nueva_fecha=timezone.now() + timedelta(days=9), motivo="Más tiempo"
            )
        self.assertIn("fecha objetivo", contexto.exception.messages[0])

    def test_la_politica_no_permite_bloquea(self):
        for politica in ("NO_PERMITE", ""):
            with self.subTest(politica=politica):
                Ticket.objects.filter(pk=self.ticket_p.pk).update(prorroga_politica=politica)
                with self.assertRaises(ValidationError):
                    self._solicitar()

    def test_la_nueva_fecha_debe_ser_posterior_a_la_vigente(self):
        from apps.tickets import prorrogas

        vigente = self._ticket().fecha_objetivo_vigente
        for nueva in (vigente, vigente - timedelta(hours=1), timezone.now() - timedelta(days=30)):
            with self.subTest(nueva=nueva), self.assertRaises(ValidationError):
                prorrogas.solicitar_prorroga(self._ticket(), self.responsable_p, nueva_fecha=nueva, motivo="Más tiempo")
        with self.assertRaises(ValidationError):
            prorrogas.solicitar_prorroga(
                self._ticket(), self.responsable_p, nueva_fecha=datetime_ingenua(), motivo="Más tiempo"
            )
        with self.assertRaises(ValidationError):
            prorrogas.solicitar_prorroga(self._ticket(), self.responsable_p, nueva_fecha=None, motivo="Más tiempo")
        self.assertEqual(self._ticket().fecha_objetivo_vigente, vigente)

    def test_el_motivo_es_obligatorio(self):
        for motivo in ("", "   ", None):
            with self.subTest(motivo=motivo), self.assertRaises(ValidationError):
                self._solicitar(motivo=motivo)

    def test_servicio_sin_workflow_tambien_admite_prorroga(self):
        self.assertIsNone(self.servicio_p.workflow_id)
        self.assertIsNone(self._ticket().instancia_workflow_id)
        self.assertEqual(self._solicitar().estado, "APROBADA")

    def test_proceso_funciona_igual_que_servicio(self):
        from apps.workflows.models import ConfiguracionEtapaTarea, Etapa, TransicionEtapa, Workflow, WorkflowVersion
        from apps.workflows.versionamiento import activar_version as activar_workflow

        workflow = Workflow.objects.create(nombre="Ejecución prórroga")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo="INICIO", nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo="TAREA", nombre="Trabajar")
        fin = Etapa.objects.create(version=version, tipo="FIN", nombre="Fin")
        ConfiguracionEtapaTarea.objects.create(etapa=tarea)
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        activar_workflow(workflow, version, self.solicitante_p)
        Servicio.objects.filter(pk=self.servicio_p.pk).update(tipo="PROCESO", workflow=workflow)
        ticket = crear_borrador(self.solicitante_p, Servicio.objects.get(pk=self.servicio_p.pk))
        radicar_ticket(ticket, self.solicitante_p)
        ticket = asignar_ticket(ticket, self.responsable_p, equipo=self.equipo_p)
        ticket = tomar_ticket(ticket, self.responsable_p)
        self.assertEqual(ticket.tipo, "PROCESO")
        from apps.tickets import prorrogas

        prorroga = prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), self.responsable_p,
            nueva_fecha=ticket.fecha_objetivo_vigente + timedelta(days=2), motivo="Proceso largo",
        )
        self.assertEqual(prorroga.estado, "APROBADA")
        self.assertEqual(
            Ticket.objects.get(pk=ticket.pk).fecha_objetivo_vigente, ticket.fecha_objetivo_vigente + timedelta(days=2)
        )


def datetime_ingenua():
    import datetime

    return datetime.datetime(2099, 1, 1, 9, 0)


class ProrrogaConAprobacionTests(_EscenarioProrrogaMixin, TestCase):
    def setUp(self):
        self._preparar_prorroga(politica="CON_APROBACION")

    def test_queda_pendiente_sin_mover_la_fecha_y_crea_la_aprobacion_del_aprobador(self):
        antes = self._ticket()
        prorroga = self._solicitar()
        despues = self._ticket()
        self.assertEqual(prorroga.estado, "PENDIENTE")
        self.assertIsNone(prorroga.resuelta_en)
        self.assertEqual(despues.fecha_objetivo_vigente, antes.fecha_objetivo_vigente)
        aprobacion = self._aprobacion(prorroga)
        self.assertEqual(aprobacion.estado, "PENDIENTE")
        self.assertEqual(aprobacion.aprobador_usuario, self.aprobador_p)
        self.assertEqual(prorroga.esquema_aprobacion.modo, "SECUENCIAL")

    def test_no_hay_una_segunda_pendiente(self):
        self._solicitar()
        with self.assertRaises(ValidationError) as contexto:
            self._solicitar(dias=6)
        self.assertIn("pendiente", contexto.exception.messages[0])
        self.assertEqual(ProrrogaTicket.objects.filter(ticket=self.ticket_p).count(), 1)

    def test_aprobar_actualiza_solo_la_vigente(self):
        antes = self._ticket()
        prorroga = self._solicitar(dias=3)
        resuelta = self._resolver(prorroga, observacion="De acuerdo")
        despues = self._ticket()
        self.assertEqual(resuelta.estado, "APROBADA")
        self.assertEqual(resuelta.resuelta_por, self.aprobador_p)
        self.assertEqual(resuelta.observaciones_resolucion, "De acuerdo")
        self.assertEqual(despues.fecha_objetivo_vigente, antes.fecha_objetivo_vigente + timedelta(days=3))
        self.assertEqual(despues.fecha_objetivo_original, antes.fecha_objetivo_original)
        self.assertEqual(self._aprobacion(prorroga).estado, "APROBADA")
        from apps.aprobaciones.models import EsquemaAprobacion

        self.assertEqual(EsquemaAprobacion.objects.get(pk=prorroga.esquema_aprobacion_id).resultado, "APROBADA")

    def test_rechazar_no_mueve_la_fecha_y_exige_observacion(self):
        antes = self._ticket()
        prorroga = self._solicitar()
        with self.assertRaises(ValidationError):
            self._resolver(prorroga, decision="RECHAZADA", observacion="")
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")
        resuelta = self._resolver(prorroga, decision="RECHAZADA", observacion="No hay margen")
        self.assertEqual(resuelta.estado, "RECHAZADA")
        self.assertEqual(resuelta.resuelta_por, self.aprobador_p)
        self.assertEqual(self._ticket().fecha_objetivo_vigente, antes.fecha_objetivo_vigente)
        self.assertEqual(self._ticket().fecha_objetivo_original, antes.fecha_objetivo_original)

    def test_devolver_no_existe_para_una_prorroga(self):
        prorroga = self._solicitar()
        with self.assertRaises(ValidationError):
            self._resolver(prorroga, decision="DEVUELTA", observacion="Ajusta")
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")
        self.assertEqual(self._aprobacion(prorroga).estado, "PENDIENTE")

    def test_una_segunda_resolucion_se_rechaza_claramente(self):
        prorroga = self._solicitar()
        self._resolver(prorroga)
        vigente = self._ticket().fecha_objetivo_vigente
        for decision in ("APROBADA", "RECHAZADA"):
            with self.subTest(decision=decision), self.assertRaises(ValidationError) as contexto:
                self._resolver(prorroga, decision=decision, observacion="otra vez")
            self.assertIn("ya fue resuelta", contexto.exception.messages[0])
        self.assertEqual(self._ticket().fecha_objetivo_vigente, vigente)

    def test_solo_el_aprobador_designado_decide(self):
        from apps.tickets import prorrogas

        prorroga = self._solicitar()
        for usuario in (self.responsable_p, self.solicitante_p, self.ajeno_p, self.gestor_p):
            with self.subTest(usuario=usuario.username), self.assertRaises(PermissionDenied):
                prorrogas.resolver_prorroga_por_aprobacion(
                    self._aprobacion(prorroga), usuario, decision="APROBADA", observacion=""
                )
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")

    def test_un_equipo_aprobador_lo_resuelve_cualquier_miembro_activo(self):
        self._configurar_servicio_p(politica="CON_APROBACION", aprobador="equipo")
        ticket = crear_borrador(self.solicitante_p, self.servicio_p)
        radicar_ticket(ticket, self.solicitante_p)
        ticket = asignar_ticket(ticket, self.responsable_p, equipo=self.equipo_p)
        ticket = tomar_ticket(ticket, self.responsable_p)
        from apps.tickets import prorrogas

        prorroga = prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), self.responsable_p,
            nueva_fecha=ticket.fecha_objetivo_vigente + timedelta(days=2), motivo="Más tiempo",
        )
        aprobacion = prorroga.esquema_aprobacion.participaciones.get()
        self.assertEqual(aprobacion.aprobador_equipo, self.equipo_aprobador_p)
        resuelta = prorrogas.resolver_prorroga_por_aprobacion(
            aprobacion, self.miembro_aprobador_p, decision="APROBADA", observacion=""
        )
        self.assertEqual(resuelta.resuelta_por, self.miembro_aprobador_p)

    def test_aprobar_exige_que_el_ticket_siga_en_atencion_pero_rechazar_siempre_se_puede(self):
        prorroga = self._solicitar()
        Ticket.objects.filter(pk=self.ticket_p.pk).update(estado=Ticket.Estado.RESUELTO)
        vigente = self._ticket().fecha_objetivo_vigente
        with self.assertRaises(ValidationError) as contexto:
            self._resolver(prorroga, decision="APROBADA")
        self.assertIn("ya no está en atención", contexto.exception.messages[0])
        self.assertEqual(self._ticket().fecha_objetivo_vigente, vigente)
        self.assertEqual(self._resolver(prorroga, decision="RECHAZADA", observacion="Ya se resolvió").estado, "RECHAZADA")

    def test_la_prorroga_no_se_aprueba_si_la_fecha_ya_no_es_posterior(self):
        prorroga = self._solicitar(dias=1)
        Ticket.objects.filter(pk=self.ticket_p.pk).update(
            fecha_objetivo_vigente=prorroga.nueva_fecha_solicitada + timedelta(days=1)
        )
        with self.assertRaises(ValidationError):
            self._resolver(prorroga)

    def test_aprobar_por_la_pantalla_de_aprobaciones_aplica_la_nueva_fecha(self):
        prorroga = self._solicitar(dias=3)
        antes = self._ticket()
        aprobacion = self._aprobacion(prorroga)
        self.client.login(username="aprobador_p", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[aprobacion.pk]))
        self.assertContains(respuesta, "Prórroga #1")
        self.assertContains(respuesta, "Necesito más tiempo para terminar")
        self.assertNotContains(respuesta, "Devolver")
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[aprobacion.pk]), {"decision": "APROBADA", "observacion": ""}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "APROBADA")
        self.assertEqual(self._ticket().fecha_objetivo_vigente, antes.fecha_objetivo_vigente + timedelta(days=3))

    def test_devolver_por_la_pantalla_se_rechaza_sin_cambios(self):
        prorroga = self._solicitar()
        aprobacion = self._aprobacion(prorroga)
        self.client.login(username="aprobador_p", password=CLAVE_PRUEBA)
        self.client.post(reverse("aprobaciones:decidir", args=[aprobacion.pk]), {"decision": "DEVUELTA", "observacion": "x"})
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")

    def test_rechazar_por_la_pantalla_deja_la_fecha_intacta(self):
        prorroga = self._solicitar()
        antes = self._ticket()
        self.client.login(username="aprobador_p", password=CLAVE_PRUEBA)
        self.client.post(
            reverse("aprobaciones:decidir", args=[self._aprobacion(prorroga).pk]),
            {"decision": "RECHAZADA", "observacion": "Sin margen"},
        )
        resuelta = ProrrogaTicket.objects.get(pk=prorroga.pk)
        self.assertEqual((resuelta.estado, resuelta.observaciones_resolucion), ("RECHAZADA", "Sin margen"))
        self.assertEqual(self._ticket().fecha_objetivo_vigente, antes.fecha_objetivo_vigente)

    def test_quien_no_es_el_aprobador_no_puede_decidir_por_la_pantalla(self):
        prorroga = self._solicitar()
        self.client.login(username="ajeno_p", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self._aprobacion(prorroga).pk]),
            {"decision": "APROBADA", "observacion": ""},
        )
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")

    def test_aprobaciones_ajenas_a_prorroga_siguen_funcionando_igual(self):
        # Una aprobación independiente (sin prórroga ni workflow) conserva su contrato.
        from apps.aprobaciones.operaciones import crear_esquema_aprobacion, resolver_aprobacion

        esquema = crear_esquema_aprobacion(modo="SECUENCIAL", participantes=[("USUARIO", self.aprobador_p)])
        aprobacion = esquema.participaciones.get()
        self.client.login(username="aprobador_p", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[aprobacion.pk]))
        self.assertContains(respuesta, "Devolver")
        self.assertNotContains(respuesta, "Prórroga #")
        _, esquema = resolver_aprobacion(aprobacion, self.aprobador_p, decision="DEVUELTA", observacion="x")
        self.assertEqual(esquema.resultado, "DEVUELTA")


class CancelarProrrogaTests(_EscenarioProrrogaMixin, TestCase):
    def setUp(self):
        self._preparar_prorroga(politica="CON_APROBACION")

    def _cancelar(self, prorroga, actor=None, motivo=""):
        from apps.tickets import prorrogas

        return prorrogas.cancelar_prorroga(prorroga, actor or self.responsable_p, motivo=motivo)

    def test_el_solicitante_cancela_una_pendiente_sin_mover_la_fecha(self):
        from apps.aprobaciones.consultas import aprobaciones_pendientes_para

        antes = self._ticket()
        prorroga = self._solicitar()
        self.assertEqual(aprobaciones_pendientes_para(self.aprobador_p).count(), 1)
        cancelada = self._cancelar(prorroga, motivo="Ya no la necesito")
        self.assertEqual(cancelada.estado, "CANCELADA")
        self.assertEqual(cancelada.resuelta_por, self.responsable_p)
        self.assertIsNotNone(cancelada.resuelta_en)
        self.assertEqual(cancelada.observaciones_resolucion, "Ya no la necesito")
        self.assertEqual(self._ticket().fecha_objetivo_vigente, antes.fecha_objetivo_vigente)
        # La aprobación se retira: el aprobador ya no la ve ni puede decidirla.
        self.assertEqual(aprobaciones_pendientes_para(self.aprobador_p).count(), 0)
        self.assertEqual(self._aprobacion(prorroga).estado, "NO_REQUERIDA")
        with self.assertRaises(ValidationError):
            self._resolver(prorroga)

    def test_no_se_cancela_una_prorroga_ya_resuelta(self):
        aprobada = self._solicitar()
        self._resolver(aprobada)
        with self.assertRaises(ValidationError):
            self._cancelar(aprobada)
        rechazada = self._solicitar(dias=2)
        self._resolver(rechazada, decision="RECHAZADA", observacion="No")
        with self.assertRaises(ValidationError):
            self._cancelar(rechazada)
        cancelada = self._solicitar(dias=2)
        self._cancelar(cancelada)
        with self.assertRaises(ValidationError):
            self._cancelar(cancelada)

    def test_solo_quien_la_solicito_puede_cancelar(self):
        prorroga = self._solicitar()
        for usuario in (self.companero_p, self.aprobador_p, self.solicitante_p, self.gestor_p, self.ajeno_p):
            with self.subTest(usuario=usuario.username), self.assertRaises(PermissionDenied):
                self._cancelar(prorroga, actor=usuario)
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")

    def test_despues_de_cancelar_se_puede_solicitar_otra(self):
        self._cancelar(self._solicitar())
        segunda = self._solicitar(dias=4)
        self.assertEqual((segunda.numero, segunda.estado), (2, "PENDIENTE"))


class MultiplesProrrogasTests(_EscenarioProrrogaMixin, TestCase):
    def test_dos_prorrogas_aprobadas_conservan_original_y_historial(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        original = self._ticket().fecha_objetivo_original
        primera = self._solicitar(dias=3)
        segunda = self._solicitar(dias=3)
        ticket = self._ticket()
        self.assertEqual(ticket.fecha_objetivo_original, original)
        self.assertEqual(ticket.fecha_objetivo_vigente, original + timedelta(days=6))
        self.assertEqual([p.numero for p in ticket.prorrogas.all()], [1, 2])
        # La segunda toma como base la vigente de entonces, no la original.
        self.assertEqual(primera.fecha_objetivo_vigente_al_solicitar, original)
        self.assertEqual(segunda.fecha_objetivo_vigente_al_solicitar, original + timedelta(days=3))
        self.assertEqual(segunda.nueva_fecha_solicitada, original + timedelta(days=6))

    def test_la_segunda_debe_superar_la_vigente_actual_no_la_original(self):
        from apps.tickets import prorrogas

        self._preparar_prorroga(politica="SIN_APROBACION")
        original = self._ticket().fecha_objetivo_original
        self._solicitar(dias=5)
        with self.assertRaises(ValidationError):
            prorrogas.solicitar_prorroga(
                self._ticket(), self.responsable_p, nueva_fecha=original + timedelta(days=2), motivo="Atrasada"
            )

    def test_con_aprobacion_encadena_pendiente_aprobada_rechazada(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        original = self._ticket().fecha_objetivo_original
        self._resolver(self._solicitar(dias=3))
        self._resolver(self._solicitar(dias=2), decision="RECHAZADA", observacion="Demasiado")
        tercera = self._solicitar(dias=1)
        ticket = self._ticket()
        self.assertEqual(ticket.fecha_objetivo_vigente, original + timedelta(days=3))
        self.assertEqual(tercera.fecha_objetivo_vigente_al_solicitar, original + timedelta(days=3))
        self.assertEqual(
            [(p.numero, p.estado) for p in ticket.prorrogas.all()],
            [(1, "APROBADA"), (2, "RECHAZADA"), (3, "PENDIENTE")],
        )


class ProrrogaHistorialAuditoriaTests(_EscenarioProrrogaMixin, TestCase):
    def _eventos(self, tipo):
        return HistorialTicket.objects.filter(ticket=self.ticket_p, tipo_evento=tipo)

    def _auditorias(self, modelo, objeto_id):
        return RegistroAuditoria.objects.filter(modelo=modelo, object_id=objeto_id)

    def test_sin_aprobacion_registra_solicitud_y_aprobacion_automatica(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        antes = self._ticket()
        prorroga = self._solicitar(dias=3, motivo="Cambió el alcance")
        solicitada = self._eventos("PRORROGA_SOLICITADA").get()
        self.assertEqual(solicitada.actor, self.responsable_p)
        self.assertEqual(solicitada.datos["prorroga_id"], prorroga.pk)
        self.assertEqual(solicitada.datos["motivo"], "Cambió el alcance")
        self.assertEqual(solicitada.datos["numero"], 1)
        self.assertEqual(solicitada.datos["fecha_objetivo_anterior"], antes.fecha_objetivo_vigente.isoformat())
        self.assertEqual(solicitada.datos["nueva_fecha"], prorroga.nueva_fecha_solicitada.isoformat())
        aprobada = self._eventos("PRORROGA_APROBADA").get()
        self.assertIsNone(aprobada.actor)  # la resolvió el Sistema por política
        self.assertEqual(aprobada.datos["causa"], "POLITICA_SIN_APROBACION")
        # Auditoría: la prórroga (CREAR) y el cambio de la fecha vigente del ticket.
        creada = self._auditorias("tickets.prorrogaticket", prorroga.pk).get(accion=RegistroAuditoria.Accion.CREAR)
        self.assertEqual(creada.usuario, self.responsable_p)
        self.assertEqual(creada.datos_nuevos["estado"], "APROBADA")
        cambio = self._auditorias("tickets.ticket", self.ticket_p.pk).get(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, datos_nuevos__prorroga_id=prorroga.pk
        )
        self.assertEqual(cambio.usuario, self.responsable_p)
        self.assertEqual(cambio.datos_anteriores, {"fecha_objetivo_vigente": antes.fecha_objetivo_vigente.isoformat()})
        self.assertEqual(
            cambio.datos_nuevos["fecha_objetivo_vigente"], prorroga.nueva_fecha_solicitada.isoformat()
        )

    def test_con_aprobacion_aprobar_registra_al_aprobador(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        prorroga = self._solicitar()
        self.assertEqual(self._eventos("PRORROGA_SOLICITADA").get().actor, self.responsable_p)
        self.assertFalse(self._eventos("PRORROGA_APROBADA").exists())
        self.assertFalse(
            self._auditorias("tickets.ticket", self.ticket_p.pk).filter(datos_nuevos__prorroga_id=prorroga.pk).exists()
        )
        self._resolver(prorroga, observacion="Adelante")
        evento = self._eventos("PRORROGA_APROBADA").get()
        self.assertEqual(evento.actor, self.aprobador_p)
        self.assertEqual(evento.datos["observaciones"], "Adelante")
        actualizacion = self._auditorias("tickets.prorrogaticket", prorroga.pk).get(
            accion=RegistroAuditoria.Accion.ACTUALIZAR
        )
        self.assertEqual(actualizacion.usuario, self.aprobador_p)
        self.assertEqual(actualizacion.datos_anteriores, {"estado": "PENDIENTE"})
        self.assertEqual(actualizacion.datos_nuevos["estado"], "APROBADA")
        self.assertTrue(
            self._auditorias("tickets.ticket", self.ticket_p.pk).filter(datos_nuevos__prorroga_id=prorroga.pk).exists()
        )

    def test_rechazar_y_cancelar_registran_su_evento_y_no_tocan_la_fecha_en_auditoria(self):
        from apps.tickets import prorrogas

        self._preparar_prorroga(politica="CON_APROBACION")
        rechazada = self._solicitar()
        self._resolver(rechazada, decision="RECHAZADA", observacion="Sin margen")
        evento = self._eventos("PRORROGA_RECHAZADA").get()
        self.assertEqual((evento.actor, evento.datos["observaciones"]), (self.aprobador_p, "Sin margen"))
        cancelada = self._solicitar(dias=2)
        prorrogas.cancelar_prorroga(cancelada, self.responsable_p, motivo="Desistí")
        evento = self._eventos("PRORROGA_CANCELADA").get()
        self.assertEqual((evento.actor, evento.datos["prorroga_id"]), (self.responsable_p, cancelada.pk))
        cierre = self._auditorias("tickets.prorrogaticket", cancelada.pk).get(accion=RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual(cierre.datos_nuevos["estado"], "CANCELADA")
        # Ninguna de las dos movió la fecha vigente del ticket.
        self.assertFalse(
            self._auditorias("tickets.ticket", self.ticket_p.pk)
            .filter(datos_nuevos__has_key="fecha_objetivo_vigente")
            .exists()
        )

    def test_los_eventos_aparecen_en_la_linea_de_tiempo_del_detalle(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        self._solicitar()
        self.client.login(username="responsable_p", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.ticket_p.pk]))
        self.assertContains(respuesta, "Prórroga solicitada")
        self.assertContains(respuesta, "Prórroga aprobada")
        self.assertContains(respuesta, "automática, sin aprobación")


class ProrrogaInmutabilidadTests(_EscenarioProrrogaMixin, TestCase):
    def setUp(self):
        self._preparar_prorroga(politica="CON_APROBACION")

    def test_una_prorroga_resuelta_no_se_modifica_ni_se_elimina(self):
        prorroga = self._solicitar()
        self._resolver(prorroga)
        resuelta = ProrrogaTicket.objects.get(pk=prorroga.pk)
        resuelta.motivo = "Otro motivo"
        with self.assertRaises(ValidationError):
            resuelta.save()
        resuelta = ProrrogaTicket.objects.get(pk=prorroga.pk)
        resuelta.estado = ProrrogaTicket.Estado.RECHAZADA
        with self.assertRaises(ValidationError):
            resuelta.save()
        with self.assertRaises(ValidationError):
            resuelta.delete()
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "APROBADA")

    def test_los_datos_de_la_solicitud_no_cambian_ni_estando_pendiente(self):
        prorroga = self._solicitar()
        for campo, valor in (
            ("motivo", "Otro"), ("nueva_fecha_solicitada", prorroga.nueva_fecha_solicitada + timedelta(days=1)),
            ("solicitada_por", self.ajeno_p), ("numero", 9),
        ):
            with self.subTest(campo=campo):
                pendiente = ProrrogaTicket.objects.get(pk=prorroga.pk)
                setattr(pendiente, campo, valor)
                with self.assertRaises(ValidationError):
                    pendiente.save()

    def test_la_base_protege_la_unicidad_de_la_pendiente_y_la_coherencia(self):
        from apps.aprobaciones.operaciones import crear_esquema_aprobacion

        prorroga = self._solicitar()
        otro_esquema = crear_esquema_aprobacion(modo="SECUENCIAL", participantes=[("USUARIO", self.aprobador_p)])
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProrrogaTicket.objects.create(
                ticket=self._ticket(), numero=2, politica="CON_APROBACION", solicitada_por=self.responsable_p,
                solicitada_en=timezone.now(), fecha_objetivo_vigente_al_solicitar=prorroga.nueva_fecha_solicitada,
                nueva_fecha_solicitada=prorroga.nueva_fecha_solicitada + timedelta(days=1), motivo="Otra",
                esquema_aprobacion=otro_esquema,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProrrogaTicket.objects.filter(pk=prorroga.pk).update(nueva_fecha_solicitada=prorroga.fecha_objetivo_vigente_al_solicitar)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProrrogaTicket.objects.filter(pk=prorroga.pk).update(estado="RECHAZADA")  # sin resuelta_en


class ProrrogaConcurrenciaTests(_EscenarioProrrogaMixin, TransactionTestCase):
    def setUp(self):
        self._preparar_prorroga(politica="CON_APROBACION")

    def _correr(self, tareas):
        resultados = {}
        barrera = threading.Barrier(len(tareas))

        def _hilo(clave, funcion):
            barrera.wait()
            try:
                funcion()
                resultados[clave] = "ok"
            except (ValidationError, PermissionDenied):
                resultados[clave] = "rechazada"
            finally:
                connection.close()

        hilos = [threading.Thread(target=_hilo, args=(clave, funcion)) for clave, funcion in tareas.items()]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        return resultados

    def test_dos_solicitudes_simultaneas_dejan_una_sola_pendiente(self):
        from apps.tickets import prorrogas

        def _pedir(actor):
            return lambda: prorrogas.solicitar_prorroga(
                Ticket.objects.get(pk=self.ticket_p.pk), actor,
                nueva_fecha=Ticket.objects.get(pk=self.ticket_p.pk).fecha_objetivo_vigente + timedelta(days=3),
                motivo="Más tiempo",
            )

        resultados = self._correr({"a": _pedir(self.responsable_p), "b": _pedir(self.companero_p)})
        self.assertEqual(sorted(resultados.values()), ["ok", "rechazada"])
        self.assertEqual(ProrrogaTicket.objects.filter(ticket=self.ticket_p).count(), 1)
        self.assertEqual(ProrrogaTicket.objects.filter(ticket=self.ticket_p, estado="PENDIENTE").count(), 1)

    def test_aprobar_y_cancelar_a_la_vez_dejan_un_resultado_coherente(self):
        from apps.tickets import prorrogas

        prorroga = self._solicitar()
        original = self._ticket().fecha_objetivo_vigente
        aprobacion = self._aprobacion(prorroga)
        resultados = self._correr({
            "aprobar": lambda: prorrogas.resolver_prorroga_por_aprobacion(
                aprobacion, self.aprobador_p, decision="APROBADA", observacion=""
            ),
            "cancelar": lambda: prorrogas.cancelar_prorroga(
                ProrrogaTicket.objects.get(pk=prorroga.pk), self.responsable_p
            ),
        })
        self.assertEqual(sorted(resultados.values()), ["ok", "rechazada"])
        final = ProrrogaTicket.objects.get(pk=prorroga.pk)
        ticket = self._ticket()
        if resultados["aprobar"] == "ok":
            self.assertEqual(final.estado, "APROBADA")
            self.assertEqual(ticket.fecha_objetivo_vigente, prorroga.nueva_fecha_solicitada)
        else:
            self.assertEqual(final.estado, "CANCELADA")
            self.assertEqual(ticket.fecha_objetivo_vigente, original)
        self.assertEqual(ticket.fecha_objetivo_original, original)

    def test_dos_aprobaciones_simultaneas_aplican_la_fecha_una_sola_vez(self):
        from apps.tickets import prorrogas

        prorroga = self._solicitar(dias=3)
        original = self._ticket().fecha_objetivo_vigente
        aprobacion = self._aprobacion(prorroga)
        decidir = lambda: prorrogas.resolver_prorroga_por_aprobacion(  # noqa: E731
            aprobacion, self.aprobador_p, decision="APROBADA", observacion=""
        )
        resultados = self._correr({"a": decidir, "b": decidir})
        self.assertEqual(sorted(resultados.values()), ["ok", "rechazada"])
        self.assertEqual(self._ticket().fecha_objetivo_vigente, original + timedelta(days=3))
        self.assertEqual(self._eventos_aprobada(), 1)

    def _eventos_aprobada(self):
        return HistorialTicket.objects.filter(ticket=self.ticket_p, tipo_evento="PRORROGA_APROBADA").count()


class ProrrogaVistasTests(_EscenarioProrrogaMixin, TestCase):
    def _detalle(self, username):
        self.client.login(username=username, password=CLAVE_PRUEBA)
        return self.client.get(reverse("tickets:detalle", args=[self.ticket_p.pk]))

    def _post(self, nombre, *args, **datos):
        return self.client.post(reverse(f"tickets:{nombre}", args=args), datos)

    def _datos_solicitud(self, dias=3):
        nueva = timezone.localtime(self._nueva_fecha(dias))
        return {"nueva_fecha": nueva.strftime("%Y-%m-%dT%H:%M"), "motivo": "Necesito más tiempo"}

    def test_el_responsable_ve_el_compromiso_y_el_boton_sin_aprobacion(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        respuesta = self._detalle("responsable_p")
        self.assertContains(respuesta, "Compromiso de atención")
        self.assertContains(respuesta, "Fecha objetivo original")
        self.assertContains(respuesta, "Fecha objetivo vigente")
        self.assertContains(respuesta, "Solicitar prórroga")
        self.assertContains(respuesta, "se aplicará directamente")
        self.assertNotContains(respuesta, "Ampliada")

    def test_con_aprobacion_el_formulario_avisa_que_se_envia_a_aprobacion(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        respuesta = self._detalle("responsable_p")
        self.assertContains(respuesta, "se enviará para aprobación")
        self.assertContains(respuesta, "Enviar para aprobación")

    def test_el_boton_solo_aparece_cuando_corresponde(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        self.assertNotContains(self._detalle("solicitante_p"), "Solicitar prórroga")
        self.client.logout()
        self.assertEqual(self._detalle("ajeno_p").status_code, 403)
        self.client.logout()
        self.assertNotContains(self._detalle("gestor_p"), "Solicitar prórroga")
        self.client.logout()
        for politica in ("NO_PERMITE", ""):
            self._configurar_servicio_p(politica="SIN_APROBACION")
            Ticket.objects.filter(pk=self.ticket_p.pk).update(prorroga_politica=politica)
            self.client.logout()
            respuesta = self._detalle("responsable_p")
            self.assertNotContains(respuesta, "Solicitar prórroga")
            self.assertContains(respuesta, "no permite prórrogas")
        Ticket.objects.filter(pk=self.ticket_p.pk).update(prorroga_politica="SIN_APROBACION", estado="RESUELTO")
        self.client.logout()
        self.assertNotContains(self._detalle("responsable_p"), "Solicitar prórroga")

    def test_con_una_pendiente_no_se_ofrece_otra_y_se_ofrece_cancelar_al_solicitante(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        self._solicitar()
        respuesta = self._detalle("responsable_p")
        self.assertNotContains(respuesta, "Solicitar prórroga")
        self.assertContains(respuesta, "Ya hay una prórroga pendiente")
        self.assertContains(respuesta, "Cancelar solicitud")
        self.client.logout()
        # Otro responsable del ticket ve la pendiente, pero no puede cancelarla.
        self.assertNotContains(self._detalle("companero_p"), "Cancelar solicitud")

    def test_el_historial_muestra_fecha_solicitante_motivo_estado_y_resolucion(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        self._resolver(self._solicitar(motivo="Dependo de un proveedor"), decision="RECHAZADA", observacion="Sin margen")
        self._configurar_servicio_p(politica="CON_APROBACION")
        respuesta = self._detalle("responsable_p")
        self.assertContains(respuesta, "Historial de prórrogas (1)")
        self.assertContains(respuesta, "Prórroga 1")
        self.assertContains(respuesta, "Rechazada")
        self.assertContains(respuesta, "Dependo de un proveedor")
        self.assertContains(respuesta, "Solicitada por responsable_p")
        self.assertContains(respuesta, "por aprobador_p")
        self.assertContains(respuesta, "Sin margen")

    def test_si_la_vigente_difiere_de_la_original_se_marca_ampliada(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        self._solicitar(dias=2)
        respuesta = self._detalle("responsable_p")
        self.assertContains(respuesta, "Ampliada")
        self.assertContains(respuesta, "Aprobada automáticamente")

    def test_el_solicitante_del_ticket_tambien_consulta_el_historial(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        self._solicitar(motivo="Motivo visible")
        respuesta = self._detalle("solicitante_p")
        self.assertContains(respuesta, "Motivo visible")
        self.assertNotContains(respuesta, "Solicitar prórroga")

    def test_el_aprobador_con_acceso_al_ticket_ve_el_enlace_para_resolver(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        _otorgar_tickets_atender(self.aprobador_p)
        prorroga = self._solicitar()
        respuesta = self._detalle("aprobador_p")
        self.assertContains(respuesta, "Resolver prórroga")
        self.assertContains(respuesta, reverse("aprobaciones:detalle", args=[self._aprobacion(prorroga).pk]))

    def test_post_solicitar_sin_aprobacion_aplica_la_fecha(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        antes = self._ticket()
        self.client.login(username="responsable_p", password=CLAVE_PRUEBA)
        respuesta = self._post("solicitar_prorroga", self.ticket_p.pk, **self._datos_solicitud(3))
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket_p.pk]))
        self.assertEqual(ProrrogaTicket.objects.get().estado, "APROBADA")
        self.assertEqual(self._ticket().fecha_objetivo_original, antes.fecha_objetivo_original)
        self.assertGreater(self._ticket().fecha_objetivo_vigente, antes.fecha_objetivo_vigente)

    def test_post_solicitar_con_aprobacion_queda_pendiente(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        antes = self._ticket()
        self.client.login(username="responsable_p", password=CLAVE_PRUEBA)
        self._post("solicitar_prorroga", self.ticket_p.pk, **self._datos_solicitud(3))
        self.assertEqual(ProrrogaTicket.objects.get().estado, "PENDIENTE")
        self.assertEqual(self._ticket().fecha_objetivo_vigente, antes.fecha_objetivo_vigente)

    def test_post_sin_permiso_o_con_datos_invalidos_no_crea_nada(self):
        self._preparar_prorroga(politica="SIN_APROBACION")
        self.client.login(username="ajeno_p", password=CLAVE_PRUEBA)
        self._post("solicitar_prorroga", self.ticket_p.pk, **self._datos_solicitud())
        self.client.logout()
        self.client.login(username="solicitante_p", password=CLAVE_PRUEBA)
        self._post("solicitar_prorroga", self.ticket_p.pk, **self._datos_solicitud())
        self.client.logout()
        self.client.login(username="responsable_p", password=CLAVE_PRUEBA)
        self._post("solicitar_prorroga", self.ticket_p.pk, nueva_fecha="no es una fecha", motivo="x")
        self._post("solicitar_prorroga", self.ticket_p.pk, nueva_fecha=self._datos_solicitud()["nueva_fecha"], motivo="")
        pasada = timezone.localtime(self._ticket().fecha_objetivo_vigente - timedelta(days=1))
        self._post("solicitar_prorroga", self.ticket_p.pk, nueva_fecha=pasada.strftime("%Y-%m-%dT%H:%M"), motivo="Atrasada")
        self.assertEqual(ProrrogaTicket.objects.count(), 0)

    def test_los_endpoints_son_solo_post_y_exigen_sesion(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        prorroga = self._solicitar()
        self.client.login(username="responsable_p", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("tickets:solicitar_prorroga", args=[self.ticket_p.pk])).status_code, 405)
        self.assertEqual(
            self.client.get(reverse("tickets:cancelar_prorroga", args=[self.ticket_p.pk, prorroga.pk])).status_code, 405
        )
        self.client.logout()
        self.assertEqual(self._post("solicitar_prorroga", self.ticket_p.pk, **self._datos_solicitud()).status_code, 302)
        self.assertEqual(ProrrogaTicket.objects.count(), 1)

    def test_post_cancelar_solo_por_quien_la_solicito(self):
        self._preparar_prorroga(politica="CON_APROBACION")
        prorroga = self._solicitar()
        self.client.login(username="companero_p", password=CLAVE_PRUEBA)
        self._post("cancelar_prorroga", self.ticket_p.pk, prorroga.pk)
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "PENDIENTE")
        self.client.logout()
        self.client.login(username="responsable_p", password=CLAVE_PRUEBA)
        self._post("cancelar_prorroga", self.ticket_p.pk, prorroga.pk, motivo="Desistí")
        self.assertEqual(ProrrogaTicket.objects.get(pk=prorroga.pk).estado, "CANCELADA")
        # Una prórroga de otro ticket no se alcanza por una URL cruzada.
        self.assertEqual(self._post("cancelar_prorroga", self.ticket_p.pk + 999, prorroga.pk).status_code, 404)


class ProrrogaRegresionTests(_EscenarioProrrogaMixin, TestCase):
    def test_solicitar_y_resolver_no_tocan_el_workflow_del_ticket(self):
        from apps.workflows.models import (
            ConfiguracionEtapaTarea, Etapa, InstanciaEtapa, InstanciaWorkflow, TransicionEtapa, Workflow, WorkflowVersion,
        )
        from apps.workflows.versionamiento import activar_version as activar_workflow

        self._preparar_prorroga(politica="CON_APROBACION")
        workflow = Workflow.objects.create(nombre="Ejecución regresión prórroga")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo="INICIO", nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo="TAREA", nombre="Trabajar")
        fin = Etapa.objects.create(version=version, tipo="FIN", nombre="Fin")
        ConfiguracionEtapaTarea.objects.create(etapa=tarea)
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        activar_workflow(workflow, version, self.solicitante_p)
        Servicio.objects.filter(pk=self.servicio_p.pk).update(workflow=workflow)
        ticket = crear_borrador(self.solicitante_p, Servicio.objects.get(pk=self.servicio_p.pk))
        radicar_ticket(ticket, self.solicitante_p)
        ticket = asignar_ticket(ticket, self.responsable_p, equipo=self.equipo_p)
        ticket = tomar_ticket(ticket, self.responsable_p)
        self.ticket_p = ticket
        instancia = ticket.instancia_workflow

        def _foto():
            actual = InstanciaWorkflow.objects.get(pk=instancia.pk)
            ejecuciones = list(
                InstanciaEtapa.objects.filter(instancia_workflow=actual).order_by("orden").values_list(
                    "orden", "estado", "etapa_id", "transicion_tomada_id"
                )
            )
            return actual.estado, actual.workflow_version_id, ejecuciones, dict(actual.contexto)

        antes = _foto()
        self._resolver(self._solicitar(dias=3))
        self._resolver(self._solicitar(dias=2), decision="RECHAZADA", observacion="No")
        self.assertEqual(_foto(), antes)

    def test_la_entrega_formal_no_cambia_por_una_prorroga(self):
        from apps.tickets import prorrogas

        class _Escenario(_EscenarioEntregaFormalMixin):
            pass

        escenario = _Escenario()
        escenario._preparar_entrega_formal()
        ticket = Ticket.objects.get(pk=escenario.ticket45.pk)
        vigente = timezone.now() + timedelta(days=5)
        Ticket.objects.filter(pk=ticket.pk).update(
            tiempo_objetivo_cantidad=5, tiempo_objetivo_unidad="DIAS", prorroga_politica="SIN_APROBACION",
            fecha_objetivo_original=vigente, fecha_objetivo_vigente=vigente,
        )
        politica_antes = (ticket.entrega_politica, ticket.entrega_dias_observacion)
        prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), escenario.responsable45,
            nueva_fecha=vigente + timedelta(days=4), motivo="Más tiempo",
        )
        ticket = Ticket.objects.get(pk=ticket.pk)
        self.assertEqual((ticket.entrega_politica, ticket.entrega_dias_observacion), politica_antes)
        self.assertEqual(EntregaTicket.objects.filter(ticket=ticket).count(), 0)
        escenario.ticket45 = ticket
        escenario._listo()
        entrega = escenario._entregar()
        # El plazo de observaciones sigue su propia semántica: entrega + N días.
        self.assertEqual(entrega.vence_en, entrega.entregada_en + timedelta(days=entrega.dias_observacion))
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).fecha_objetivo_vigente, vigente + timedelta(days=4))

    def test_un_ticket_creado_antes_de_4a2_sigue_su_ciclo_completo(self):
        self._preparar_prorroga(politica="")
        Ticket.objects.filter(pk=self.ticket_p.pk).update(
            tiempo_objetivo_cantidad=None, tiempo_objetivo_unidad="", fecha_objetivo_original=None, fecha_objetivo_vigente=None,
        )
        resolver_ticket(self._ticket(), self.responsable_p, "Listo")
        cerrar_ticket(self._ticket(), self.solicitante_p)
        ticket = self._ticket()
        self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)
        self.assertEqual(ticket.prorrogas.count(), 0)


class TareaATicketNavegacionTests(TestCase):
    """4.A2 — Mi trabajo: navegar de una Tarea al Ticket que la originó. La
    prórroga sigue perteneciendo al Ticket."""

    def setUp(self):
        from apps.workflows.models import ConfiguracionEtapaTarea, Etapa, TareaWorkflow, TransicionEtapa, Workflow, WorkflowVersion
        from apps.workflows.versionamiento import activar_version as activar_workflow

        self.solicitante = Usuario.objects.create_user("nav_solicitante", password=CLAVE_PRUEBA)
        self.sin_ticket = Usuario.objects.create_user("nav_sin_ticket", password=CLAVE_PRUEBA)
        for usuario in (self.solicitante, self.sin_ticket):
            _otorgar_permiso(usuario, "tareas.consultar", nombre_rol=f"Rol tareas {usuario.username}")
        self.servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        workflow = Workflow.objects.create(nombre="Flujo navegación")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo="INICIO", nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo="TAREA", nombre="Preparar entrega")
        fin = Etapa.objects.create(version=version, tipo="FIN", nombre="Fin")
        ConfiguracionEtapaTarea.objects.create(etapa=tarea)
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        activar_workflow(workflow, version, self.solicitante)
        Servicio.objects.filter(pk=self.servicio.pk).update(workflow=workflow)
        self.ticket = crear_borrador(self.solicitante, Servicio.objects.get(pk=self.servicio.pk))
        radicar_ticket(self.ticket, self.solicitante)
        self.tarea = TareaWorkflow.objects.get(instancia_etapa__instancia_workflow=self.ticket.instancia_workflow).tarea

    def _enlace(self):
        return reverse("tickets:detalle", args=[self.ticket.pk])

    def test_el_detalle_de_la_tarea_enlaza_al_ticket_si_el_usuario_puede_verlo(self):
        self.client.login(username="nav_solicitante", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:detalle", args=[self.tarea.pk]))
        self.assertContains(respuesta, "Parte del ticket")
        self.assertContains(respuesta, self._enlace())

    def test_la_vista_previa_de_mi_trabajo_tambien_enlaza_al_ticket(self):
        self.client.login(username="nav_solicitante", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:vista_previa", args=[self.tarea.pk]))
        self.assertContains(respuesta, self._enlace())

    def test_sin_acceso_al_ticket_no_se_ofrece_el_enlace(self):
        self.client.login(username="nav_sin_ticket", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:detalle", args=[self.tarea.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, self._enlace())

    def test_una_subtarea_enlaza_al_ticket_de_su_tarea_principal(self):
        from apps.tareas.models import Tarea

        subtarea = Tarea.objects.create(titulo="Detalle", tarea_padre=self.tarea)
        self.client.login(username="nav_solicitante", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tareas:detalle", args=[subtarea.pk])), self._enlace())

    def test_una_tarea_independiente_no_tiene_ticket(self):
        from apps.tareas.models import Tarea

        suelta = Tarea.objects.create(titulo="Sin ticket")
        self.client.login(username="nav_solicitante", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:detalle", args=[suelta.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "Parte del ticket")


# --- 4.C1 — Ticket General y configuración base --------------------------------


class _EscenarioTicketGeneralMixin:
    """Un Servicio interno con formulario genérico de tres campos configurables
    (asunto, descripción, adjunto), designado y —según se pida— habilitado; más un
    Servicio y un Proceso ordinarios con su propio formulario."""

    NOMBRE_INTERNO = "Servicio interno reservado"

    def _preparar_general(self, *, habilitar=True, designar=True):
        from apps.catalogo import ticket_general as general

        self.general = general
        self.admin_g = Usuario.objects.create_user("admin_g", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin_g, "catalogo.administrar")
        self.usuario_g = Usuario.objects.create_user("usuario_g", password=CLAVE_PRUEBA)
        self.atiende_g = Usuario.objects.create_user("atiende_g", password=CLAVE_PRUEBA)
        _otorgar_tickets_atender(self.atiende_g)
        self.servicio_g, self.version_g, self.campos_g = _crear_servicio_con_formulario(
            self.admin_g,
            [
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Asunto", "obligatorio": True},
                {"tipo": Campo.TipoCampo.TEXTO_LARGO, "etiqueta": "Descripción"},
                {"tipo": Campo.TipoCampo.ARCHIVO, "etiqueta": "Adjuntos"},
            ],
        )
        Servicio.objects.filter(pk=self.servicio_g.pk).update(nombre=self.NOMBRE_INTERNO)
        self.servicio_g.refresh_from_db()
        self._preparar_destino_general()
        if designar:
            general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
            self.servicio_g.refresh_from_db()
            if habilitar:
                general.configurar_habilitacion(self.admin_g, habilitado=True)

    def _preparar_destino_general(self):
        """4.C2 — radicar un Ticket General exige un destino. Base de todos los
        escenarios: un destino EQUIPO (con un miembro que atiende) como destino de
        reserva, para que los tickets creados sin elegir destino sigan radicándose
        igual que en 4.C1. Se fija directo en la configuración (sin auditoría) para no
        alterar los eventos que los tests de 4.C1 cuentan."""
        from apps.catalogo import destinos_ticket_general as destinos
        from apps.catalogo.models import ConfiguracionTicketGeneral

        self.destinos = destinos
        self.equipo_g = Equipo.objects.create(nombre="Equipo general")
        MiembroEquipo.objects.create(equipo=self.equipo_g, usuario=self.atiende_g)
        self.destino_g = destinos.crear_destino(self.admin_g, tipo="EQUIPO", objeto=self.equipo_g)
        ConfiguracionTicketGeneral.objects.update_or_create(
            pk=ConfiguracionTicketGeneral.PK_UNICA, defaults={"destino_predeterminado": self.destino_g}
        )

    def _preparar_catalogo_normal(self):
        self.servicio_n, _, self.campos_n = _crear_servicio_con_formulario(
            self.admin_g, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Motivo"}]
        )
        Servicio.objects.filter(pk=self.servicio_n.pk).update(nombre="Servicio ordinario visible")
        self.proceso_n, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        Servicio.objects.filter(pk=self.proceso_n.pk).update(nombre="Proceso ordinario visible", tipo="PROCESO")
        self.servicio_n.refresh_from_db()
        self.proceso_n.refresh_from_db()

    def _configurar_interno(self, **campos):
        Servicio.objects.filter(pk=self.servicio_g.pk).update(**campos)
        self.servicio_g.refresh_from_db()

    def _ticket_general(self, usuario=None):
        from apps.tickets.operaciones import crear_borrador_ticket_general

        return crear_borrador_ticket_general(usuario or self.usuario_g)

    def _radicar_general(self, usuario=None, asunto="Necesito apoyo con un tema nuevo"):
        usuario = usuario or self.usuario_g
        ticket = self._ticket_general(usuario)
        guardar_respuestas_borrador(ticket, usuario, {self.campos_g["Asunto"].id: asunto})
        radicar_ticket(ticket, usuario)
        ticket.refresh_from_db()
        return ticket


class TicketGeneralConfiguracionTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general(habilitar=False)

    def _config(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        return ConfiguracionTicketGeneral

    def test_sin_configuracion_guardada_esta_deshabilitado_y_consultar_no_escribe(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        ConfiguracionTicketGeneral.objects.all().delete()  # el setUp ya designó el servicio (crea la fila)
        self.assertFalse(ConfiguracionTicketGeneral.actual().habilitado)
        self.assertEqual(ConfiguracionTicketGeneral.objects.count(), 0)

    def test_hay_una_sola_configuracion_efectiva(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        ConfiguracionTicketGeneral(habilitado=True).save()
        ConfiguracionTicketGeneral(habilitado=False).save()
        self.assertEqual(ConfiguracionTicketGeneral.objects.count(), 1)
        self.assertEqual(ConfiguracionTicketGeneral.objects.get().pk, ConfiguracionTicketGeneral.PK_UNICA)
        self.assertFalse(ConfiguracionTicketGeneral.actual().habilitado)

    def test_habilitar_y_deshabilitar(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.assertTrue(self._config().actual().habilitado)
        self.general.configurar_habilitacion(self.admin_g, habilitado=False)
        self.assertFalse(self._config().actual().habilitado)

    def test_para_habilitar_hace_falta_un_servicio_interno_publicado_y_valido(self):
        Servicio.objects.filter(pk=self.servicio_g.pk).update(es_ticket_general=False)
        with self.assertRaises(ValidationError) as contexto:
            self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.assertIn("servicio interno", contexto.exception.messages[0])
        Servicio.objects.filter(pk=self.servicio_g.pk).update(es_ticket_general=True, activo=False)
        with self.assertRaises(ValidationError) as contexto:
            self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.assertIn("publica", contexto.exception.messages[0])
        Servicio.objects.filter(pk=self.servicio_g.pk).update(activo=True)
        Formulario.objects.filter(pk=self.servicio_g.formulario_id).update(version_activa=None)
        with self.assertRaises(ValidationError):
            self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.assertFalse(self._config().actual().habilitado)

    def test_audita_los_cambios_y_no_duplica_si_no_cambia(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        eventos = RegistroAuditoria.objects.filter(modelo="catalogo.configuracionticketgeneral")
        self.assertEqual(eventos.count(), 1)
        evento = eventos.get()
        self.assertEqual(evento.accion, RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual((evento.datos_anteriores, evento.datos_nuevos), ({"habilitado": False}, {"habilitado": True}))
        self.assertEqual(evento.usuario, self.admin_g)

    def test_todas_las_operaciones_exigen_catalogo_administrar(self):
        categoria = Categoria.objects.create(nombre="Otra")
        for llamada in (
            lambda: self.general.configurar_habilitacion(self.usuario_g, habilitado=True),
            lambda: self.general.designar_servicio_ticket_general(self.servicio_g, self.usuario_g),
            lambda: self.general.crear_servicio_ticket_general(self.usuario_g, categoria=categoria),
        ):
            with self.assertRaises(PermissionDenied):
                llamada()

    def test_las_vistas_de_administracion_exigen_permiso_y_solo_post(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        for nombre in ("ticket_general_habilitar", "ticket_general_crear", "ticket_general_designar"):
            self.assertEqual(self.client.post(reverse(f"catalogo:{nombre}"), {}).status_code, 403)
        self.client.logout()
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        for nombre in ("ticket_general_habilitar", "ticket_general_crear", "ticket_general_designar"):
            self.assertEqual(self.client.get(reverse(f"catalogo:{nombre}")).status_code, 405)

    def test_habilitar_por_la_pantalla(self):
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("catalogo:ticket_general_habilitar"), {"habilitado": "1"})
        self.assertRedirects(respuesta, reverse("core:disenador_servicios"))
        self.assertTrue(self._config().actual().habilitado)
        self.client.post(reverse("catalogo:ticket_general_habilitar"), {"habilitado": "0"})
        self.assertFalse(self._config().actual().habilitado)

    def test_la_tarjeta_de_administracion_muestra_el_estado_y_enlaza_al_studio_del_servicio(self):
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:disenador_servicios"))
        self.assertContains(respuesta, "Ticket general")
        self.assertContains(respuesta, self.NOMBRE_INTERNO)
        self.assertContains(respuesta, reverse("core:disenador_servicio", args=[self.servicio_g.pk]))
        self.assertContains(respuesta, "Habilitar ticket general")


class TicketGeneralServicioInternoTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general(habilitar=False, designar=False)

    def test_designar_marca_un_unico_servicio(self):
        self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
        self.assertEqual(self.general.servicio_ticket_general(), Servicio.objects.get(pk=self.servicio_g.pk))
        self.assertEqual(Servicio.objects.filter(es_ticket_general=True).count(), 1)
        evento = RegistroAuditoria.objects.get(modelo="catalogo.servicio", object_id=self.servicio_g.pk)
        self.assertEqual(evento.datos_nuevos, {"es_ticket_general": True})

    def test_la_base_impide_dos_servicios_marcados(self):
        otro, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Servicio.objects.filter(pk=otro.pk).update(es_ticket_general=True)

    def test_el_servicio_interno_no_puede_ser_un_proceso(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Servicio.objects.filter(pk=self.servicio_g.pk).update(es_ticket_general=True, tipo="PROCESO")
        Servicio.objects.filter(pk=self.servicio_g.pk).update(tipo="PROCESO")
        self.servicio_g.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)

    def test_cambiar_el_tipo_del_servicio_interno_a_proceso_se_rechaza_en_la_edicion(self):
        from apps.catalogo.operaciones import editar_servicio_general

        self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
        with self.assertRaises(ValidationError):
            editar_servicio_general(
                self.servicio_g, self.admin_g, nombre="X", descripcion="", categoria=self.servicio_g.categoria,
                tipo="PROCESO", instrucciones="", alcance_visibilidad=self.servicio_g.alcance_visibilidad,
            )

    def test_la_marca_no_es_editable_por_admin(self):
        from apps.catalogo.admin import ServicioAdmin

        self.assertIn("es_ticket_general", ServicioAdmin.readonly_fields)

    def test_designar_otro_servicio_mueve_la_marca_y_deshabilita(self):
        otro, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.general.designar_servicio_ticket_general(otro, self.admin_g)
        self.assertEqual(list(Servicio.objects.filter(es_ticket_general=True)), [Servicio.objects.get(pk=otro.pk)])
        self.assertFalse(self.general.ConfiguracionTicketGeneral.actual().habilitado)

    def test_un_servicio_con_tickets_no_se_convierte_en_general_ni_deja_de_serlo(self):
        servicio_con_tickets, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        crear_borrador(self.usuario_g, servicio_con_tickets)
        with self.assertRaises(ValidationError) as contexto:
            self.general.designar_servicio_ticket_general(servicio_con_tickets, self.admin_g)
        self.assertIn("ya tiene tickets", contexto.exception.messages[0])
        self.assertFalse(Servicio.objects.get(pk=servicio_con_tickets.pk).es_ticket_general)
        # El interno con tickets conserva su marca.
        self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self._ticket_general()
        otro, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        with self.assertRaises(ValidationError):
            self.general.designar_servicio_ticket_general(otro, self.admin_g)
        self.assertTrue(Servicio.objects.get(pk=self.servicio_g.pk).es_ticket_general)
        self.assertNotIn(servicio_con_tickets, self.general.servicios_candidatos())

    def test_crear_el_servicio_interno_desde_cero(self):
        Servicio.objects.filter(pk=self.servicio_g.pk).delete()
        categoria = Categoria.objects.create(nombre="Interna")
        servicio = self.general.crear_servicio_ticket_general(self.admin_g, categoria=categoria)
        self.assertTrue(servicio.es_ticket_general)
        self.assertEqual((servicio.tipo, servicio.activo), ("SERVICIO", False))
        self.assertEqual(servicio.alcance_visibilidad, "PUBLICO_INTERNO")
        self.assertEqual(servicio.nombre, "Ticket general")
        self.assertIsNone(servicio.formulario_id)  # el formulario se construye en Studio, no se siembra
        with self.assertRaises(ValidationError):
            self.general.crear_servicio_ticket_general(self.admin_g, categoria=categoria)
        self.assertEqual(Servicio.objects.filter(es_ticket_general=True).count(), 1)

    def test_crear_exige_una_categoria_activa(self):
        Servicio.objects.filter(pk=self.servicio_g.pk).delete()
        inactiva = Categoria.objects.create(nombre="Apagada", activo=False)
        for categoria in (None, inactiva):
            with self.assertRaises(ValidationError):
                self.general.crear_servicio_ticket_general(self.admin_g, categoria=categoria)
        self.assertFalse(Servicio.objects.filter(es_ticket_general=True).exists())

    def test_crear_y_elegir_por_la_pantalla(self):
        Servicio.objects.filter(pk=self.servicio_g.pk).delete()
        categoria = Categoria.objects.create(nombre="Interna")
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("catalogo:ticket_general_crear"), {"categoria": categoria.pk, "nombre": "Pedidos libres"}
        )
        interno = Servicio.objects.get(es_ticket_general=True)
        self.assertRedirects(
            respuesta, reverse("core:disenador_servicio", args=[interno.pk]), fetch_redirect_response=False
        )
        self.assertEqual(interno.nombre, "Pedidos libres")

    def test_nada_se_crea_sin_una_accion_explicita(self):
        # Importar, migrar o abrir pantallas no crea el servicio interno ni la configuración.
        configuraciones = self.general.ConfiguracionTicketGeneral.objects.count()
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        self.client.get(reverse("core:disenador_servicios"))
        self.client.get(reverse("core:inicio"))
        self.assertFalse(Servicio.objects.filter(es_ticket_general=True).exists())
        self.assertEqual(self.general.ConfiguracionTicketGeneral.objects.count(), configuraciones)


class TicketGeneralVisibilidadTests(_EscenarioTicketGeneralMixin, TestCase):
    """El Servicio interno está ACTIVO y aun así no es un Servicio catalogado."""

    def setUp(self):
        self._preparar_general(habilitar=True)
        self._preparar_catalogo_normal()

    def test_esta_activo_pero_fuera_del_catalogo(self):
        from apps.catalogo.visibilidad import servicios_accesibles_para, servicios_visibles_para

        self.assertTrue(self.servicio_g.activo)
        self.assertNotIn(self.servicio_g, servicios_visibles_para(self.usuario_g))
        self.assertIn(self.servicio_g, servicios_accesibles_para(self.usuario_g))

    def test_servicios_y_procesos_ordinarios_siguen_visibles(self):
        from apps.catalogo.visibilidad import servicios_visibles_para

        visibles = servicios_visibles_para(self.usuario_g)
        self.assertIn(self.servicio_n, visibles)
        self.assertIn(self.proceso_n, visibles)

    def test_no_aparece_en_el_catalogo_ni_se_abre_por_su_url(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("catalogo:lista"))
        self.assertContains(respuesta, "Servicio ordinario visible")
        self.assertContains(respuesta, "Proceso ordinario visible")
        self.assertNotContains(respuesta, self.NOMBRE_INTERNO)
        self.assertEqual(self.client.get(reverse("catalogo:detalle", args=[self.servicio_g.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("tickets:solicitar", args=[self.servicio_g.pk])).status_code, 404)

    def test_no_aparece_en_el_explorador_ni_en_la_busqueda_ordinaria(self):
        from apps.core import inicio

        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:explorar"), {"q": "interno"})
        self.assertNotContains(respuesta, self.NOMBRE_INTERNO)
        respuesta = self.client.get(reverse("core:explorar"))
        self.assertContains(respuesta, "Servicio ordinario visible")
        self.assertNotContains(respuesta, self.NOMBRE_INTERNO)
        resultados, _ = inicio.buscar_servicios(self.usuario_g, "interno")
        self.assertEqual(resultados, [])
        resultados, _ = inicio.buscar_servicios(self.usuario_g, "")
        self.assertNotIn(self.servicio_g, resultados)
        self.assertEqual(
            self.client.get(reverse("catalogo:lista"), {"q": "interno"}).context["servicios"].count(), 0
        )

    def test_no_cuenta_en_las_categorias_de_inicio(self):
        from apps.core import inicio

        categorias = inicio.categorias_con_servicios(self.usuario_g)
        total = sum(c["n"] for c in categorias)
        self.assertEqual(total, 2)  # solo el servicio y el proceso ordinarios

    def test_no_aparece_en_frecuentes_ni_recientes_aunque_se_use_mucho(self):
        from apps.core import inicio

        for _ in range(3):
            self._ticket_general()
        usados = inicio.servicios_usados(self.usuario_g)
        self.assertNotIn(self.servicio_g.pk, [s.pk for s in usados["frecuentes"] + usados["recientes"]])
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:inicio"))
        # Las tarjetas de servicios usados no lo ofrecen; sus tickets sí aparecen, como
        # cualquier ticket propio, en "Tus tickets recientes".
        self.assertNotIn(self.servicio_g.pk, [s.pk for s in respuesta.context["frecuentes"]])
        self.assertNotIn(self.servicio_g.pk, [s.pk for s in respuesta.context["recientes"]])
        self.assertFalse(respuesta.context["tiene_usados"])
        self.assertEqual(len(respuesta.context["tickets_recientes"]), 3)

    def test_la_via_normal_de_crear_tickets_lo_rechaza(self):
        with self.assertRaises(PermissionDenied):
            crear_borrador(self.usuario_g, self.servicio_g)
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.client.get(reverse("tickets:iniciar", args=[self.servicio_g.pk]))
        self.assertEqual(Ticket.objects.count(), 0)

    def test_para_el_administrador_si_aparece_en_el_diseñador(self):
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:disenador_servicios"))
        self.assertContains(respuesta, self.NOMBRE_INTERNO)
        self.assertContains(respuesta, "designer-status designer-status--neutral\">Ticket general")


class TicketGeneralFormularioTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general()

    def test_reutiliza_el_formulario_versionado_de_siempre(self):
        ticket = self._ticket_general()
        self.assertEqual(ticket.detalle_servicio.servicio, self.servicio_g)
        self.assertEqual(ticket.detalle_servicio.formulario_version, self.version_g)
        self.assertEqual(ticket.respuesta_formulario.formulario_version, self.version_g)
        self.assertEqual(
            sorted(c.etiqueta for c in self.version_g.campos.all()), ["Adjuntos", "Asunto", "Descripción"]
        )
        self.assertEqual(
            {c.tipo for c in self.version_g.campos.all()},
            {Campo.TipoCampo.TEXTO, Campo.TipoCampo.TEXTO_LARGO, Campo.TipoCampo.ARCHIVO},
        )

    def test_una_version_nueva_sigue_las_reglas_de_siempre(self):
        antiguo = self._ticket_general()
        nueva = crear_nueva_version(self.servicio_g.formulario, actor=self.admin_g)
        Campo.objects.create(version=nueva, tipo=Campo.TipoCampo.TEXTO, etiqueta="Urgencia")
        activar_version(self.servicio_g.formulario, nueva, actor=self.admin_g)
        otro_usuario = Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA)
        reciente = self._ticket_general(otro_usuario)
        self.assertEqual(reciente.detalle_servicio.formulario_version_id, nueva.pk)
        antiguo.refresh_from_db()
        self.assertEqual(antiguo.detalle_servicio.formulario_version_id, self.version_g.pk)

    def test_no_hay_campos_en_el_ticket_ni_otro_motor(self):
        # Solo columnas propias del Ticket: `adjuntos` existe como relación inversa del
        # modelo Adjunto (2.4), no como campo.
        nombres = {campo.name for campo in Ticket._meta.concrete_fields}
        for prohibido in ("asunto", "descripcion", "adjuntos", "adjunto"):
            self.assertNotIn(prohibido, nombres)
        ticket = self._radicar_general(asunto="Asunto guardado como respuesta de formulario")
        respuesta = ticket.respuesta_formulario.respuestas_campo.get(campo=self.campos_g["Asunto"])
        self.assertEqual(respuesta.valor, "Asunto guardado como respuesta de formulario")


class TicketGeneralCreacionTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general()

    def test_crea_un_ticket_normal_sobre_el_servicio_interno(self):
        ticket = self._ticket_general()
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertEqual(ticket.tipo, "SERVICIO")
        self.assertEqual(ticket.solicitante, self.usuario_g)
        self.assertEqual(TicketServicio.objects.filter(ticket=ticket).count(), 1)
        self.assertTrue(ticket.detalle_servicio.servicio.es_ticket_general)
        self.assertIsNone(ticket.instancia_workflow_id)

    def test_congela_tiempo_prorroga_y_entrega_del_servicio_interno(self):
        self._configurar_interno(
            tiempo_objetivo_cantidad=2, tiempo_objetivo_unidad="DIAS", tiempo_objetivo_habiles=True,
            politica_prorroga="CON_APROBACION", prorroga_aprobador_usuario=self.admin_g,
            politica_entrega="PERIODO_OBSERVACIONES", dias_observacion=4,
        )
        ticket = self._ticket_general()
        self.assertEqual(
            (ticket.tiempo_objetivo_cantidad, ticket.tiempo_objetivo_unidad, ticket.tiempo_objetivo_habiles),
            (2, "DIAS", True),
        )
        self.assertEqual(
            (ticket.prorroga_politica, ticket.prorroga_aprobador_usuario_id), ("CON_APROBACION", self.admin_g.pk)
        )
        self.assertEqual((ticket.entrega_politica, ticket.entrega_dias_observacion), ("PERIODO_OBSERVACIONES", 4))
        # Cambiar el servicio después no altera el ticket ya creado.
        self._configurar_interno(
            tiempo_objetivo_cantidad=30, politica_prorroga="NO_PERMITE", prorroga_aprobador_usuario=None
        )
        ticket.refresh_from_db()
        self.assertEqual((ticket.tiempo_objetivo_cantidad, ticket.prorroga_politica), (2, "CON_APROBACION"))

    def test_materializa_los_entregables_del_servicio_interno(self):
        from apps.catalogo.models import DefinicionEntregable

        DefinicionEntregable.objects.create(servicio=self.servicio_g, nombre="Respuesta", tipo="TEXTO", obligatorio=True)
        ticket = self._ticket_general()
        self.assertEqual([e.nombre for e in ticket.entregables.all()], ["Respuesta"])

    def test_deshabilitado_la_operacion_se_rechaza_y_no_crea_nada(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=False)
        with self.assertRaises(ValidationError) as contexto:
            self._ticket_general()
        self.assertIn("no está habilitado", contexto.exception.messages[0])
        self.assertEqual(Ticket.objects.count(), 0)

    def test_exige_visibilidad_normal_del_servicio_interno(self):
        self._configurar_interno(alcance_visibilidad="RESTRINGIDO")
        with self.assertRaises(PermissionDenied):
            self._ticket_general()
        from apps.catalogo.models import ServicioVisibilidad

        ServicioVisibilidad.objects.create(
            servicio=self.servicio_g, tipo_alcance="USUARIO", usuario=self.usuario_g
        )
        self.assertEqual(self._ticket_general().detalle_servicio.servicio, self.servicio_g)
        otro = Usuario.objects.create_user("sin_acceso_g", password=CLAVE_PRUEBA)
        with self.assertRaises(PermissionDenied):
            self._ticket_general(otro)

    def test_exige_un_servicio_interno_activo_y_con_formulario_activo(self):
        self._configurar_interno(activo=False)
        with self.assertRaises(PermissionDenied):
            self._ticket_general()
        self._configurar_interno(activo=True)
        Formulario.objects.filter(pk=self.servicio_g.formulario_id).update(version_activa=None)
        with self.assertRaises(ValidationError) as contexto:
            self._ticket_general()
        self.assertIn("formulario activo", contexto.exception.messages[0])

    def test_sin_servicio_interno_configurado_se_rechaza(self):
        Servicio.objects.filter(pk=self.servicio_g.pk).update(es_ticket_general=False)
        with self.assertRaises(ValidationError):
            self._ticket_general()

    def test_el_origen_se_deduce_del_servicio_sin_campos_extra(self):
        self._preparar_catalogo_normal()
        general = self._ticket_general()
        normal = crear_borrador(self.usuario_g, self.servicio_n)
        generales = Ticket.objects.filter(detalle_servicio__servicio__es_ticket_general=True)
        self.assertEqual(list(generales), [general])
        self.assertNotIn(normal, generales)


class TicketGeneralRadicacionTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general()
        self._configurar_interno(
            tiempo_objetivo_cantidad=3, tiempo_objetivo_unidad="DIAS", tiempo_objetivo_habiles=False,
            politica_prorroga="SIN_APROBACION",
        )

    def test_radica_con_el_dominio_existente_y_calcula_la_fecha_objetivo(self):
        ticket = self._radicar_general()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsNotNone(ticket.radicado)
        self.assertEqual(ticket.fecha_objetivo_original, ticket.fecha_objetivo_vigente)
        self.assertIsNotNone(ticket.fecha_objetivo_original)
        self.assertEqual(ticket.historial.filter(tipo_evento="RADICADO").count(), 1)

    def test_no_crea_ningun_workflow_artificial(self):
        from apps.workflows.models import InstanciaWorkflow, Workflow

        antes = (Workflow.objects.count(), InstanciaWorkflow.objects.count())
        ticket = self._radicar_general()
        self.assertEqual((Workflow.objects.count(), InstanciaWorkflow.objects.count()), antes)
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertIsNone(self.servicio_g.workflow_id)

    def test_valida_su_formulario_al_radicar(self):
        ticket = self._ticket_general()
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario_g)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.BORRADOR)

    def test_sigue_el_ciclo_normal_hasta_cerrarse_y_admite_prorroga(self):
        from apps.tickets import prorrogas

        ticket = self._radicar_general()
        ticket = tomar_ticket(ticket, self.atiende_g)
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        prorroga = prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), self.atiende_g,
            nueva_fecha=ticket.fecha_objetivo_vigente + timedelta(days=2), motivo="Más tiempo",
        )
        self.assertEqual(prorroga.estado, "APROBADA")
        resolver_ticket(Ticket.objects.get(pk=ticket.pk), self.atiende_g, "Resuelto")
        cerrar_ticket(Ticket.objects.get(pk=ticket.pk), self.usuario_g)
        final = Ticket.objects.get(pk=ticket.pk)
        self.assertEqual(final.estado, Ticket.Estado.CERRADO)
        self.assertEqual(final.fecha_objetivo_original, ticket.fecha_objetivo_original)

    def test_entra_a_la_cola_normal_de_atencion(self):
        ticket = self._radicar_general()
        self.assertTrue(puede_ver_en_cola(self.atiende_g, ticket))
        self.client.login(username="atiende_g", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tickets:cola")), self.NOMBRE_INTERNO)

    def test_radicar_no_exige_que_siga_habilitado(self):
        # Habilitar/deshabilitar gobierna la ENTRADA (crear el borrador); un borrador
        # ya creado conserva su derecho a radicarse, como con cualquier Servicio.
        ticket = self._ticket_general()
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Pendiente"})
        self.general.configurar_habilitacion(self.admin_g, habilitado=False)
        radicar_ticket(ticket, self.usuario_g)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RADICADO)


class TicketGeneralVistasTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general(habilitar=True)

    def _inicio(self, username="usuario_g"):
        self.client.login(username=username, password=CLAVE_PRUEBA)
        return self.client.get(reverse("core:inicio"))

    def test_habilitado_se_ofrece_la_entrada_en_inicio(self):
        respuesta = self._inicio()
        self.assertContains(respuesta, "Crear ticket general")
        self.assertContains(respuesta, reverse("tickets:ticket_general"))

    def test_deshabilitado_no_hay_cta_y_la_url_directa_se_rechaza(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=False)
        self.assertNotContains(self._inicio(), "Crear ticket general")
        self.assertEqual(self.client.get(reverse("tickets:ticket_general")).status_code, 404)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_no_se_ofrece_si_el_servicio_no_tiene_formulario_activo_o_no_es_accesible(self):
        self._configurar_interno(alcance_visibilidad="RESTRINGIDO")
        self.assertNotContains(self._inicio(), "Crear ticket general")
        self.assertEqual(self.client.get(reverse("tickets:ticket_general")).status_code, 404)
        self._configurar_interno(alcance_visibilidad="PUBLICO_INTERNO")
        Formulario.objects.filter(pk=self.servicio_g.formulario_id).update(version_activa=None)
        self.assertNotContains(self._inicio(), "Crear ticket general")
        respuesta = self.client.get(reverse("tickets:ticket_general"))
        self.assertContains(respuesta, "aún no está disponible")

    def test_exige_sesion(self):
        self.client.logout()
        respuesta = self.client.get(reverse("tickets:ticket_general"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("login", respuesta.url)

    def test_entrar_crea_el_borrador_y_muestra_el_formulario_generico(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:ticket_general"))
        ticket = Ticket.objects.get()
        self.assertRedirects(respuesta, reverse("tickets:borrador", args=[ticket.pk]))
        pagina = self.client.get(reverse("tickets:borrador", args=[ticket.pk]))
        self.assertContains(pagina, "Asunto")
        self.assertContains(pagina, "Descripción")
        self.assertContains(pagina, "Adjuntos")

    def test_entrar_dos_veces_reutiliza_el_borrador_vacio(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.client.get(reverse("tickets:ticket_general"))
        self.client.get(reverse("tickets:ticket_general"))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_el_recorrido_completo_radica_por_la_via_normal(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.client.get(reverse("tickets:ticket_general"))
        ticket = Ticket.objects.get()
        self.client.post(
            reverse("tickets:borrador", args=[ticket.pk]),
            {f"campo_{self.campos_g['Asunto'].id}": "Pedido sin servicio", f"campo_{self.campos_g['Descripción'].id}": "Detalle"},
        )
        respuesta = self.client.post(reverse("tickets:enviar", args=[ticket.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RADICADO)

    def test_el_enlace_de_inicio_no_aparece_para_quien_no_tiene_acceso(self):
        self._configurar_interno(alcance_visibilidad="RESTRINGIDO")
        from apps.catalogo.models import ServicioVisibilidad

        ServicioVisibilidad.objects.create(servicio=self.servicio_g, tipo_alcance="USUARIO", usuario=self.usuario_g)
        self.assertContains(self._inicio("usuario_g"), "Crear ticket general")
        self.client.logout()
        self.assertNotContains(self._inicio("atiende_g"), "Crear ticket general")


class TicketGeneralRegresionTests(_EscenarioTicketGeneralMixin, TestCase):
    def setUp(self):
        self._preparar_general(habilitar=True)
        self._preparar_catalogo_normal()

    def test_crear_borrador_normal_conserva_su_contrato(self):
        ticket = crear_borrador(self.usuario_g, self.servicio_n)
        self.assertEqual(ticket.detalle_servicio.servicio, self.servicio_n)
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertFalse(ticket.detalle_servicio.servicio.es_ticket_general)
        # Un servicio normal no visible sigue rechazándose igual que antes.
        Servicio.objects.filter(pk=self.servicio_n.pk).update(alcance_visibilidad="RESTRINGIDO")
        with self.assertRaises(PermissionDenied):
            crear_borrador(self.usuario_g, Servicio.objects.get(pk=self.servicio_n.pk))

    def test_el_flujo_v2_de_un_servicio_normal_sigue_funcionando(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:solicitar", args=[self.servicio_n.pk]))
        ticket = Ticket.objects.get()
        self.assertRedirects(respuesta, reverse("tickets:borrador", args=[ticket.pk]))
        self.assertEqual(ticket.detalle_servicio.servicio, self.servicio_n)

    def test_los_tickets_existentes_no_se_convierten_en_generales(self):
        ticket = crear_borrador(self.usuario_g, self.servicio_n)
        antes = (ticket.estado, ticket.detalle_servicio.servicio_id)
        self._ticket_general()
        ticket.refresh_from_db()
        self.assertEqual((ticket.estado, ticket.detalle_servicio.servicio_id), antes)
        self.assertEqual(Ticket.objects.filter(detalle_servicio__servicio__es_ticket_general=True).count(), 1)

    def test_el_tiempo_objetivo_de_un_servicio_normal_sigue_funcionando(self):
        Servicio.objects.filter(pk=self.servicio_n.pk).update(tiempo_objetivo_cantidad=2, tiempo_objetivo_unidad="DIAS")
        ticket = crear_borrador(self.usuario_g, Servicio.objects.get(pk=self.servicio_n.pk))
        guardar_respuestas_borrador(ticket, self.usuario_g, {})
        radicar_ticket(ticket, self.usuario_g)
        ticket.refresh_from_db()
        self.assertEqual(ticket.tiempo_objetivo_cantidad, 2)
        self.assertIsNotNone(ticket.fecha_objetivo_original)

    def test_la_prorroga_de_un_servicio_normal_sigue_funcionando(self):
        from apps.tickets import prorrogas

        Servicio.objects.filter(pk=self.servicio_n.pk).update(
            tiempo_objetivo_cantidad=2, tiempo_objetivo_unidad="DIAS", politica_prorroga="SIN_APROBACION"
        )
        ticket = crear_borrador(self.usuario_g, Servicio.objects.get(pk=self.servicio_n.pk))
        radicar_ticket(ticket, self.usuario_g)
        ticket = tomar_ticket(ticket, self.atiende_g)
        prorroga = prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), self.atiende_g,
            nueva_fecha=ticket.fecha_objetivo_vigente + timedelta(days=1), motivo="Más tiempo",
        )
        self.assertEqual(prorroga.estado, "APROBADA")

    def test_el_catalogo_y_sus_pruebas_de_visibilidad_no_cambian_para_servicios_normales(self):
        from apps.catalogo.visibilidad import servicios_visibles_para

        Servicio.objects.filter(pk=self.servicio_n.pk).update(alcance_visibilidad="RESTRINGIDO")
        self.assertNotIn(self.servicio_n, servicios_visibles_para(self.usuario_g))
        from apps.catalogo.models import ServicioVisibilidad

        ServicioVisibilidad.objects.create(servicio=self.servicio_n, tipo_alcance="USUARIO", usuario=self.usuario_g)
        self.assertIn(Servicio.objects.get(pk=self.servicio_n.pk), servicios_visibles_para(self.usuario_g))


class TicketGeneralConcurrenciaTests(_EscenarioTicketGeneralMixin, TransactionTestCase):
    def setUp(self):
        self._preparar_general(habilitar=False, designar=False)

    def _correr(self, tareas):
        resultados = {}
        barrera = threading.Barrier(len(tareas))

        def _hilo(clave, funcion):
            barrera.wait()
            try:
                funcion()
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "rechazada"
            finally:
                connection.close()

        hilos = [threading.Thread(target=_hilo, args=(c, f)) for c, f in tareas.items()]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        return resultados

    def test_dos_designaciones_simultaneas_dejan_un_unico_servicio_interno(self):
        otro, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        resultados = self._correr({
            "a": lambda: self.general.designar_servicio_ticket_general(
                Servicio.objects.get(pk=self.servicio_g.pk), self.admin_g
            ),
            "b": lambda: self.general.designar_servicio_ticket_general(Servicio.objects.get(pk=otro.pk), self.admin_g),
        })
        self.assertEqual(sorted(resultados.values()), ["ok", "ok"])  # en serie: la segunda reemplaza a la primera
        self.assertEqual(Servicio.objects.filter(es_ticket_general=True).count(), 1)
        self.assertEqual(self.general.ConfiguracionTicketGeneral.objects.count(), 1)

    def test_dos_creaciones_simultaneas_del_servicio_interno_solo_una_prospera(self):
        Servicio.objects.filter(pk=self.servicio_g.pk).delete()
        categoria = Categoria.objects.create(nombre="Concurrente")
        resultados = self._correr({
            clave: (lambda: self.general.crear_servicio_ticket_general(self.admin_g, categoria=categoria))
            for clave in ("a", "b")
        })
        self.assertEqual(sorted(resultados.values()), ["ok", "rechazada"])
        self.assertEqual(Servicio.objects.filter(es_ticket_general=True).count(), 1)

    def test_habilitar_y_designar_a_la_vez_dejan_la_configuracion_consistente(self):
        self.general.designar_servicio_ticket_general(self.servicio_g, self.admin_g)
        otro, _, _ = _crear_servicio_con_formulario(self.admin_g, [])
        self._correr({
            "habilitar": lambda: self.general.configurar_habilitacion(self.admin_g, habilitado=True),
            "designar": lambda: self.general.designar_servicio_ticket_general(Servicio.objects.get(pk=otro.pk), self.admin_g),
        })
        marcados = Servicio.objects.filter(es_ticket_general=True)
        self.assertEqual(marcados.count(), 1)
        # Si quedó habilitado, es porque el servicio interno vigente es válido y activo.
        configuracion = self.general.ConfiguracionTicketGeneral.actual()
        if configuracion.habilitado:
            self.assertTrue(marcados.get().activo)


# --- 4.C2 — Direccionamiento y enrutamiento del Ticket General -------------------


class _EscenarioDestinosMixin(_EscenarioTicketGeneralMixin):
    """Sobre el escenario de 4.C1 (Servicio interno habilitado, destino de reserva
    `destino_g` = `equipo_g`): un destino de cada tipo y personas con alcance
    `tickets.atender` de ÁREA (no global) para que nada se autorice por accidente."""

    def _preparar_destinos(self, *, habilitar=True):
        self._preparar_general(habilitar=habilitar)
        self.area_otra = Area.objects.create(nombre="Otra área", codigo="OTRA-G")
        self.area_tic = Area.objects.create(nombre="Tecnología", codigo="TIC-G")
        self.equipo_tic = Equipo.objects.create(nombre="Soporte TIC")
        self.equipo_dis = Equipo.objects.create(nombre="Diseño")
        self.miembro_tic = Usuario.objects.create_user("miembro_tic", password=CLAVE_PRUEBA)
        self.miembro_dis = Usuario.objects.create_user("miembro_dis", password=CLAVE_PRUEBA)
        self.persona_g = Usuario.objects.create_user(
            "persona_g", first_name="María", last_name="Pérez", password=CLAVE_PRUEBA
        )
        self.ajeno_g = Usuario.objects.create_user("ajeno_g", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=self.equipo_tic, usuario=self.miembro_tic)
        MiembroEquipo.objects.create(equipo=self.equipo_dis, usuario=self.miembro_dis)
        for usuario in (self.miembro_tic, self.miembro_dis, self.persona_g, self.ajeno_g):
            _otorgar_tickets_atender(usuario, tipo_alcance=AsignacionRol.TipoAlcance.AREA, area=self.area_otra)
        destinos = self.destinos
        self.d_area = destinos.crear_destino(self.admin_g, tipo="AREA", objeto=self.area_tic, responsable=self.equipo_tic)
        self.d_equipo = destinos.crear_destino(self.admin_g, tipo="EQUIPO", objeto=self.equipo_dis)
        self.d_persona = destinos.crear_destino(self.admin_g, tipo="USUARIO", objeto=self.persona_g)

    def _sin_reserva(self):
        self.destinos.definir_predeterminado(self.admin_g, None)

    def _radicar_a(self, destino=None, usuario=None, asunto="Necesito apoyo con un tema nuevo"):
        from apps.tickets import direccionamiento

        usuario = usuario or self.usuario_g
        ticket = self._ticket_general(usuario)
        if destino is not None:
            direccionamiento.seleccionar_destino_borrador(ticket, usuario, destino.pk)
        guardar_respuestas_borrador(ticket, usuario, {self.campos_g["Asunto"].id: asunto})
        radicar_ticket(ticket, usuario)
        ticket.refresh_from_db()
        return ticket

    def _cola(self, usuario, **parametros):
        self.client.login(username=usuario.username, password=CLAVE_PRUEBA)
        return self.client.get(reverse("tickets:cola"), parametros)


class TicketGeneralDestinoModeloTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos(habilitar=False)

    def test_destino_de_area_con_su_responsable(self):
        self.assertEqual((self.d_area.tipo, self.d_area.area, self.d_area.etiqueta), ("AREA", self.area_tic, "Tecnología"))
        self.assertEqual((self.d_area.responsable_equipo, self.d_area.responsable_usuario), (self.equipo_tic, None))
        self.assertTrue(self.d_area.activo)

    def test_destino_de_equipo_es_su_propio_responsable_por_defecto(self):
        self.assertEqual((self.d_equipo.equipo, self.d_equipo.responsable_equipo), (self.equipo_dis, self.equipo_dis))
        self.assertIsNone(self.d_equipo.responsable_usuario)

    def test_destino_de_persona_es_su_propio_responsable_por_defecto(self):
        self.assertEqual(self.d_persona.etiqueta, "María Pérez")
        self.assertEqual((self.d_persona.responsable_usuario, self.d_persona.responsable_equipo), (self.persona_g, None))

    def test_un_destino_de_area_exige_responsable(self):
        otra = Area.objects.create(nombre="Mercadeo", codigo="MER-G")
        with self.assertRaises(ValidationError):
            self.destinos.crear_destino(self.admin_g, tipo="AREA", objeto=otra)

    def test_un_equipo_puede_atender_un_destino_de_persona_y_viceversa(self):
        destino = self.destinos.crear_destino(
            self.admin_g, tipo="USUARIO", objeto=self.ajeno_g, responsable=self.equipo_tic
        )
        self.assertEqual((destino.responsable_equipo, destino.responsable_usuario), (self.equipo_tic, None))
        otra = Area.objects.create(nombre="Mercadeo", codigo="MER-G")
        destino = self.destinos.crear_destino(self.admin_g, tipo="AREA", objeto=otra, responsable=self.miembro_tic)
        self.assertEqual((destino.responsable_usuario, destino.responsable_equipo), (self.miembro_tic, None))

    def test_la_base_rechaza_combinaciones_incoherentes_de_destino(self):
        from apps.catalogo.models import DestinoTicketGeneral

        otra = Area.objects.create(nombre="Mercadeo", codigo="MER-G")
        casos = {
            "area sin area": dict(tipo="AREA", equipo=self.equipo_dis),
            "area con equipo": dict(tipo="AREA", area=otra, equipo=self.equipo_dis),
            "equipo con area": dict(tipo="EQUIPO", equipo=self.equipo_dis, area=otra),
            "equipo con usuario": dict(tipo="EQUIPO", equipo=self.equipo_dis, usuario=self.ajeno_g),
            "usuario sin usuario": dict(tipo="USUARIO", area=otra),
            "usuario con equipo": dict(tipo="USUARIO", usuario=self.ajeno_g, equipo=self.equipo_dis),
            "tipo desconocido": dict(tipo="OTRO", area=otra),
            "tipo sin objeto": dict(tipo="AREA"),
        }
        for nombre, datos in casos.items():
            with self.subTest(nombre), self.assertRaises(IntegrityError), transaction.atomic():
                DestinoTicketGeneral.objects.create(responsable_equipo=self.equipo_tic, **datos)

    def test_la_base_exige_exactamente_un_responsable(self):
        from apps.catalogo.models import DestinoTicketGeneral

        otra = Area.objects.create(nombre="Mercadeo", codigo="MER-G")
        for nombre, responsables in {
            "ninguno": {},
            "ambos": {"responsable_equipo": self.equipo_tic, "responsable_usuario": self.miembro_tic},
        }.items():
            with self.subTest(nombre), self.assertRaises(IntegrityError), transaction.atomic():
                DestinoTicketGeneral.objects.create(tipo="AREA", area=otra, **responsables)

    def test_un_objeto_tiene_un_solo_destino(self):
        from apps.catalogo.models import DestinoTicketGeneral

        with self.assertRaises(ValidationError):
            self.destinos.crear_destino(self.admin_g, tipo="AREA", objeto=self.area_tic, responsable=self.equipo_dis)
        for datos in (
            dict(tipo="AREA", area=self.area_tic), dict(tipo="EQUIPO", equipo=self.equipo_dis),
            dict(tipo="USUARIO", usuario=self.persona_g),
        ):
            with self.subTest(datos["tipo"]), self.assertRaises(IntegrityError), transaction.atomic():
                DestinoTicketGeneral.objects.create(responsable_equipo=self.equipo_tic, **datos)

    def test_no_se_crea_un_destino_con_objetos_inactivos_segun_el_dominio_real(self):
        area = Area.objects.create(nombre="Inactiva", codigo="INA-G", activo=False)
        equipo = Equipo.objects.create(nombre="Equipo inactivo", activo=False)
        usuario = Usuario.objects.create_user("inactivo_g", password=CLAVE_PRUEBA, is_active=False)
        for tipo, objeto, responsable in (
            ("AREA", area, self.equipo_tic), ("EQUIPO", equipo, None), ("USUARIO", usuario, None),
        ):
            with self.subTest(tipo), self.assertRaises(ValidationError):
                self.destinos.crear_destino(self.admin_g, tipo=tipo, objeto=objeto, responsable=responsable)

    def test_el_responsable_debe_poder_atender(self):
        otra = Area.objects.create(nombre="Mercadeo", codigo="MER-G")
        vacio = Equipo.objects.create(nombre="Equipo vacío")
        inactivo = Equipo.objects.create(nombre="Equipo apagado", activo=False)
        MiembroEquipo.objects.create(equipo=inactivo, usuario=self.ajeno_g)
        persona_inactiva = Usuario.objects.create_user("baja_g", password=CLAVE_PRUEBA, is_active=False)
        for responsable in (vacio, inactivo, persona_inactiva):
            with self.subTest(str(responsable)), self.assertRaises(ValidationError):
                self.destinos.crear_destino(self.admin_g, tipo="AREA", objeto=otra, responsable=responsable)
        MiembroEquipo.objects.filter(equipo=self.equipo_tic).update(activo=False)
        with self.assertRaises(ValidationError):
            self.destinos.crear_destino(self.admin_g, tipo="AREA", objeto=otra, responsable=self.equipo_tic)

    def test_un_tipo_u_objeto_incorrecto_se_rechaza(self):
        with self.assertRaises(ValidationError):
            self.destinos.crear_destino(self.admin_g, tipo="AREA", objeto=self.equipo_dis, responsable=self.equipo_tic)
        with self.assertRaises(ValidationError):
            self.destinos.crear_destino(self.admin_g, tipo="OTRO", objeto=self.area_tic, responsable=self.equipo_tic)

    def test_activo_e_inactivo_y_utilizable(self):
        self.assertIn(self.d_area, self.destinos.destinos_utilizables())
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        self.assertNotIn(self.d_area.pk, [d.pk for d in self.destinos.destinos_utilizables()])
        self.destinos.activar_destino(self.admin_g, self.d_area)
        self.assertIn(self.d_area.pk, [d.pk for d in self.destinos.destinos_utilizables()])

    def test_un_destino_activo_deja_de_ser_utilizable_si_su_objeto_se_inactiva(self):
        Area.objects.filter(pk=self.area_tic.pk).update(activo=False)
        destino = self.destinos._consulta().get(pk=self.d_area.pk)
        self.assertTrue(destino.activo)
        self.assertIn("inactiva", self.destinos.motivo_no_utilizable(destino))
        self.assertNotIn(destino.pk, [d.pk for d in self.destinos.destinos_utilizables()])
        with self.assertRaises(ValidationError):  # activo, pero su área no lo es: no puede ser reserva
            self.destinos.definir_predeterminado(self.admin_g, destino)
        self.destinos.desactivar_destino(self.admin_g, destino)
        with self.assertRaises(ValidationError):  # y no se reactiva mientras su área siga inactiva
            self.destinos.activar_destino(self.admin_g, destino)

    def test_el_destino_de_reserva_es_unico_y_siempre_utilizable(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        self.assertEqual(ConfiguracionTicketGeneral.actual().destino_predeterminado, self.destino_g)
        self.destinos.definir_predeterminado(self.admin_g, self.d_area)
        self.assertEqual(ConfiguracionTicketGeneral.actual().destino_predeterminado, self.d_area)
        self.assertEqual(ConfiguracionTicketGeneral.objects.count(), 1)
        self.assertEqual(self.destinos.destino_predeterminado_utilizable().pk, self.d_area.pk)

    def test_el_destino_de_reserva_debe_estar_activo_y_se_puede_quitar(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        self.destinos.desactivar_destino(self.admin_g, self.d_equipo)
        with self.assertRaises(ValidationError):
            self.destinos.definir_predeterminado(self.admin_g, self.d_equipo)
        self.destinos.definir_predeterminado(self.admin_g, None)
        self.assertIsNone(ConfiguracionTicketGeneral.actual().destino_predeterminado_id)
        self.assertIsNone(self.destinos.destino_predeterminado_utilizable())

    def test_desactivar_el_destino_de_reserva_lo_retira_como_reserva(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        self.destinos.desactivar_destino(self.admin_g, self.destino_g)
        self.assertIsNone(ConfiguracionTicketGeneral.actual().destino_predeterminado_id)
        self.assertFalse(self.destinos.opciones_de_seleccion()["permite_omitir"])


class TicketGeneralDestinoAdministracionTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos(habilitar=False)

    def _eventos(self, modelo="catalogo.destinoticketgeneral"):
        return RegistroAuditoria.objects.filter(modelo=modelo).order_by("pk")

    def test_todas_las_operaciones_exigen_catalogo_administrar(self):
        for llamada in (
            lambda: self.destinos.crear_destino(
                self.usuario_g, tipo="AREA", objeto=self.area_otra, responsable=self.equipo_tic
            ),
            lambda: self.destinos.cambiar_responsable(self.usuario_g, self.d_area, responsable=self.equipo_dis),
            lambda: self.destinos.activar_destino(self.usuario_g, self.d_area),
            lambda: self.destinos.desactivar_destino(self.usuario_g, self.d_area),
            lambda: self.destinos.definir_predeterminado(self.usuario_g, self.d_area),
        ):
            with self.assertRaises(PermissionDenied):
                llamada()

    def test_editar_el_responsable_solo_afecta_a_los_tickets_nuevos(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        antes = self._radicar_a(self.d_area)
        self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.equipo_dis)
        self.d_area.refresh_from_db()
        self.assertEqual(self.d_area.responsable_equipo, self.equipo_dis)
        antes.refresh_from_db()
        self.assertEqual(antes.equipo_responsable, self.equipo_tic)
        nuevo = self._radicar_a(self.d_area, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        self.assertEqual(nuevo.equipo_responsable, self.equipo_dis)

    def test_cambiar_a_una_persona_responsable_y_validar(self):
        self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.miembro_tic)
        self.d_area.refresh_from_db()
        self.assertEqual((self.d_area.responsable_usuario, self.d_area.responsable_equipo), (self.miembro_tic, None))
        with self.assertRaises(ValidationError):
            self.destinos.cambiar_responsable(
                self.admin_g, self.d_area, responsable=Equipo.objects.create(nombre="Sin miembros")
            )
        with self.assertRaises(ValidationError):
            self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=None)

    def test_activar_y_desactivar_son_idempotentes(self):
        self.destinos.activar_destino(self.admin_g, self.d_area)
        self.assertEqual(self._eventos().filter(accion="ACTUALIZAR").count(), 0)
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        self.assertEqual(self._eventos().filter(accion="ACTUALIZAR").count(), 1)

    def test_audita_alta_edicion_activacion_y_cambio_de_responsable(self):
        alta = self._eventos().filter(accion="CREAR", object_id=self.d_area.pk).get()
        self.assertEqual(alta.usuario, self.admin_g)
        self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.equipo_dis)
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        self.destinos.activar_destino(self.admin_g, self.d_area)
        eventos = list(self._eventos().filter(accion="ACTUALIZAR", object_id=self.d_area.pk))
        self.assertEqual(len(eventos), 3)
        cambio, baja, alta_de_nuevo = eventos
        self.assertEqual(cambio.datos_anteriores["responsable_equipo_id"], self.equipo_tic.pk)
        self.assertEqual(cambio.datos_nuevos["responsable_equipo_id"], self.equipo_dis.pk)
        self.assertEqual((baja.datos_anteriores["activo"], baja.datos_nuevos["activo"]), (True, False))
        self.assertEqual((alta_de_nuevo.datos_anteriores["activo"], alta_de_nuevo.datos_nuevos["activo"]), (False, True))

    def test_audita_el_cambio_del_destino_de_reserva(self):
        self.destinos.definir_predeterminado(self.admin_g, self.d_area)
        self.destinos.definir_predeterminado(self.admin_g, self.d_area)  # sin cambio: no duplica
        self.destinos.definir_predeterminado(self.admin_g, None)
        eventos = list(self._eventos("catalogo.configuracionticketgeneral").filter(datos_nuevos__has_key="destino_predeterminado_id"))
        self.assertEqual(
            [(e.datos_anteriores["destino_predeterminado_id"], e.datos_nuevos["destino_predeterminado_id"]) for e in eventos],
            [(self.destino_g.pk, self.d_area.pk), (self.d_area.pk, None)],
        )

    def test_habilitar_exige_al_menos_un_destino_utilizable(self):
        for destino in (self.destino_g, self.d_area, self.d_equipo, self.d_persona):
            self.destinos.desactivar_destino(self.admin_g, destino)
        with self.assertRaises(ValidationError) as contexto:
            self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.assertIn("destino", contexto.exception.messages[0])
        self.assertFalse(self.general.estado_para_administracion()["puede_habilitar"])
        self.destinos.activar_destino(self.admin_g, self.d_area)
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)

    def test_sin_destinos_utilizables_no_se_ofrece_la_entrada(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=True)
        self.assertIsNotNone(self.general.servicio_disponible_para(self.usuario_g))
        for destino in (self.destino_g, self.d_area, self.d_equipo, self.d_persona):
            self.destinos.desactivar_destino(self.admin_g, destino)
        self.assertIsNone(self.general.servicio_disponible_para(self.usuario_g))
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tickets:ticket_general")), "aún no está disponible")
        self.assertEqual(Ticket.objects.count(), 0)

    def test_pantalla_lista_destinos_y_estado(self):
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("core:disenador_servicios"))
        self.assertContains(respuesta, "Soporte TIC")
        self.assertContains(respuesta, "Área: Tecnología")
        self.assertContains(respuesta, "Destino de reserva")
        self.assertContains(respuesta, "María Pérez")

    def test_acciones_de_la_pantalla(self):
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        otra = Area.objects.create(nombre="Mercadeo", codigo="MER-G")
        self.client.post(reverse("catalogo:ticket_general_destino_crear"), {
            "tipo": "AREA", "objeto_area": otra.pk, "objeto_equipo": "", "objeto_usuario": "",
            "responsable": f"EQUIPO:{self.equipo_dis.pk}",
        })
        creado = self.destinos._consulta().get(area=otra)
        self.assertEqual(creado.responsable_equipo, self.equipo_dis)
        self.client.post(
            reverse("catalogo:ticket_general_destino_responsable", args=[creado.pk]),
            {"responsable": f"USUARIO:{self.miembro_tic.pk}"},
        )
        creado.refresh_from_db()
        self.assertEqual(creado.responsable_usuario, self.miembro_tic)
        self.client.post(reverse("catalogo:ticket_general_destino_estado", args=[creado.pk]), {"activo": "0"})
        creado.refresh_from_db()
        self.assertFalse(creado.activo)
        self.client.post(reverse("catalogo:ticket_general_destino_estado", args=[creado.pk]), {"activo": "1"})
        self.client.post(reverse("catalogo:ticket_general_destino_predeterminado"), {"destino": creado.pk})
        self.assertEqual(self.general.ConfiguracionTicketGeneral.actual().destino_predeterminado_id, creado.pk)
        self.client.post(reverse("catalogo:ticket_general_destino_predeterminado"), {"destino": ""})
        self.assertIsNone(self.general.ConfiguracionTicketGeneral.actual().destino_predeterminado_id)

    def test_la_pantalla_muestra_el_motivo_si_algo_no_se_acepta(self):
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("catalogo:ticket_general_destino_crear"),
            {"tipo": "AREA", "objeto_area": self.area_otra.pk, "responsable": ""}, follow=True,
        )
        self.assertContains(respuesta, "necesita un responsable")
        self.assertFalse(self.destinos._consulta().filter(area=self.area_otra).exists())

    def test_las_vistas_exigen_permiso_y_solo_aceptan_post(self):
        urls = [
            reverse("catalogo:ticket_general_destino_crear"),
            reverse("catalogo:ticket_general_destino_predeterminado"),
            reverse("catalogo:ticket_general_destino_responsable", args=[self.d_area.pk]),
            reverse("catalogo:ticket_general_destino_estado", args=[self.d_area.pk]),
        ]
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        for url in urls:
            self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.client.login(username="admin_g", password=CLAVE_PRUEBA)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 405)


class TicketGeneralSeleccionTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos()

    def test_una_lista_agrupada_solo_con_destinos_activos(self):
        opciones = self.destinos.opciones_de_seleccion()
        self.assertEqual([g["titulo"] for g in opciones["grupos"]], ["Áreas", "Equipos", "Personas"])
        por_grupo = {g["titulo"]: [o["etiqueta"] for o in g["opciones"]] for g in opciones["grupos"]}
        self.assertEqual(por_grupo["Áreas"], ["Tecnología"])
        self.assertEqual(por_grupo["Equipos"], ["Diseño", "Equipo general"])
        self.assertEqual(por_grupo["Personas"], ["María Pérez"])
        self.destinos.desactivar_destino(self.admin_g, self.d_equipo)
        por_grupo = {g["titulo"]: [o["etiqueta"] for o in g["opciones"]] for g in self.destinos.opciones_de_seleccion()["grupos"]}
        self.assertEqual(por_grupo["Equipos"], ["Equipo general"])

    def test_la_entrada_muestra_el_selector_agrupado(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        ticket_url = self.client.get(reverse("tickets:ticket_general"))["Location"]
        respuesta = self.client.get(ticket_url)
        self.assertContains(respuesta, 'name="destino_general"')
        for titulo in ("Áreas", "Equipos", "Personas"):
            self.assertContains(respuesta, f'<optgroup label="{titulo}">')
        self.assertContains(respuesta, "No estoy seguro")
        self.assertContains(respuesta, "¿A quién diriges tu solicitud?")

    def test_el_destino_es_obligatorio_si_no_hay_destino_de_reserva(self):
        self._sin_reserva()
        self.assertFalse(self.destinos.opciones_de_seleccion()["permite_omitir"])
        ticket = self._ticket_general()
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Sin destino"})
        with self.assertRaises(ValidationError) as contexto:
            radicar_ticket(ticket, self.usuario_g)
        self.assertIn("Selecciona a quién", contexto.exception.messages[0])
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertEqual((ticket.usuario_responsable_id, ticket.equipo_responsable_id, ticket.radicado), (None, None, None))
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:borrador", args=[ticket.pk]))
        self.assertContains(respuesta, "Obligatorio")
        self.assertNotContains(respuesta, "No estoy seguro")

    def test_sin_seleccion_se_resuelve_al_destino_de_reserva(self):
        from apps.tickets.models import DireccionamientoTicket

        ticket = self._radicar_a(None)
        fila = DireccionamientoTicket.objects.get(ticket=ticket)
        self.assertEqual((fila.destino, fila.es_predeterminado), (self.destino_g, True))
        self.assertEqual(ticket.equipo_responsable, self.equipo_g)

    def test_elegir_un_destino_no_usa_la_reserva(self):
        from apps.tickets.models import DireccionamientoTicket

        ticket = self._radicar_a(self.d_equipo)
        fila = DireccionamientoTicket.objects.get(ticket=ticket)
        self.assertEqual((fila.destino, fila.es_predeterminado), (self.d_equipo, False))

    def test_no_se_puede_elegir_un_destino_inactivo_o_inexistente(self):
        from apps.tickets import direccionamiento

        ticket = self._ticket_general()
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        for valor in (self.d_area.pk, 999999, "no-es-un-id"):
            with self.subTest(valor), self.assertRaises(ValidationError):
                direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, valor)

    def test_solo_el_solicitante_en_borrador_y_en_un_ticket_general(self):
        from apps.tickets import direccionamiento

        ticket = self._ticket_general()
        with self.assertRaises(PermissionDenied):
            direccionamiento.seleccionar_destino_borrador(ticket, self.ajeno_g, self.d_area.pk)
        self._preparar_catalogo_normal()
        normal = crear_borrador(self.usuario_g, self.servicio_n)
        with self.assertRaises(ValidationError):
            direccionamiento.seleccionar_destino_borrador(normal, self.usuario_g, self.d_area.pk)
        radicado = self._radicar_a(self.d_area, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        with self.assertRaises(ValidationError):
            direccionamiento.seleccionar_destino_borrador(radicado, radicado.solicitante, self.d_equipo.pk)

    def test_se_puede_cambiar_y_quitar_la_seleccion_en_borrador(self):
        from apps.tickets import direccionamiento
        from apps.tickets.models import DireccionamientoTicket

        ticket = self._ticket_general()
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_area.pk)
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_equipo.pk)
        self.assertEqual(DireccionamientoTicket.objects.get(ticket=ticket).destino, self.d_equipo)
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, "")
        self.assertIsNone(DireccionamientoTicket.objects.get(ticket=ticket).destino)
        self.assertEqual(DireccionamientoTicket.objects.filter(ticket=ticket).count(), 1)

    def test_el_destino_no_es_un_campo_del_formulario_ni_cambia_con_el_formulario(self):
        etiquetas = {c.etiqueta for c in self.version_g.campos.all()}
        self.assertEqual(etiquetas, {"Asunto", "Descripción", "Adjuntos"})
        from apps.tickets import direccionamiento

        nueva = crear_nueva_version(self.servicio_g.formulario, actor=self.admin_g)
        Campo.objects.create(version=nueva, tipo=Campo.TipoCampo.TEXTO, etiqueta="Urgencia")
        activar_version(self.servicio_g.formulario, nueva, actor=self.admin_g)
        ticket = self._ticket_general(Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, nueva.pk)
        direccionamiento.seleccionar_destino_borrador(ticket, ticket.solicitante, self.d_area.pk)
        self.assertIsNone(direccionamiento.error_de_destino(ticket))  # el enrutamiento no depende de la versión

    def test_recorrido_completo_por_la_interfaz(self):
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.client.get(reverse("tickets:ticket_general"))
        ticket = Ticket.objects.get(solicitante=self.usuario_g)
        campo = f"campo_{self.campos_g['Asunto'].id}"
        respuesta = self.client.post(
            reverse("tickets:revisar", args=[ticket.pk]), {"destino_general": self.d_area.pk, campo: "Mi pedido"}
        )
        self.assertRedirects(respuesta, reverse("tickets:revisar", args=[ticket.pk]))
        self.assertContains(self.client.get(reverse("tickets:revisar", args=[ticket.pk])), "Tecnología")
        self.client.post(reverse("tickets:enviar", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(ticket.equipo_responsable, self.equipo_tic)

    def test_la_revision_avisa_si_falta_el_destino_y_no_deja_avanzar(self):
        self._sin_reserva()
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.client.get(reverse("tickets:ticket_general"))
        ticket = Ticket.objects.get(solicitante=self.usuario_g)
        campo = f"campo_{self.campos_g['Asunto'].id}"
        respuesta = self.client.post(reverse("tickets:revisar", args=[ticket.pk]), {"destino_general": "", campo: "Mi pedido"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Selecciona a quién diriges tu solicitud.")
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)

    def test_el_formulario_de_un_servicio_normal_no_muestra_selector(self):
        self._preparar_catalogo_normal()
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        redireccion = self.client.get(reverse("tickets:solicitar", args=[self.servicio_n.pk]))
        self.assertNotContains(self.client.get(redireccion["Location"]), "destino_general")


class TicketGeneralRadicacionDestinoTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos()
        self._configurar_interno(
            tiempo_objetivo_cantidad=3, tiempo_objetivo_unidad="DIAS", tiempo_objetivo_habiles=False,
            politica_prorroga="SIN_APROBACION",
        )

    def test_area_resuelve_al_responsable_configurado(self):
        ticket = self._radicar_a(self.d_area)
        self.assertEqual((ticket.equipo_responsable, ticket.usuario_responsable), (self.equipo_tic, None))
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_equipo_resuelve_a_su_responsable(self):
        ticket = self._radicar_a(self.d_equipo)
        self.assertEqual((ticket.equipo_responsable, ticket.usuario_responsable), (self.equipo_dis, None))
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_persona_resuelve_a_su_responsable(self):
        ticket = self._radicar_a(self.d_persona)
        self.assertEqual((ticket.usuario_responsable, ticket.equipo_responsable), (self.persona_g, None))
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_se_fija_la_foto_del_destino(self):
        from apps.tickets.models import DireccionamientoTicket

        ticket = self._radicar_a(self.d_area)
        fila = DireccionamientoTicket.objects.get(ticket=ticket)
        self.assertEqual(
            (fila.tipo, fila.referencia_id, fila.etiqueta, fila.destino, fila.es_predeterminado),
            ("AREA", self.area_tic.pk, "Tecnología", self.d_area, False),
        )
        self.assertIsNotNone(fila.fijado_en)

    def test_la_foto_fijada_es_inmutable(self):
        from apps.tickets.models import DireccionamientoTicket

        fila = DireccionamientoTicket.objects.get(ticket=self._radicar_a(self.d_area))
        fila.etiqueta = "Otra"
        with self.assertRaises(ValidationError):
            fila.save()
        self.assertEqual(DireccionamientoTicket.objects.get(pk=fila.pk).etiqueta, "Tecnología")

    def test_la_base_impide_fotos_incoherentes(self):
        from apps.tickets.models import DireccionamientoTicket
        from apps.tickets import direccionamiento

        ticket = self._ticket_general()
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_area.pk)
        filas = DireccionamientoTicket.objects.filter(ticket=ticket)
        for cambios in (
            {"fijado_en": timezone.now()},  # fijada sin foto
            {"tipo": "AREA", "etiqueta": "X", "referencia_id": 1},  # foto sin fijar
        ):
            with self.subTest(cambios), self.assertRaises(IntegrityError), transaction.atomic():
                filas.update(**cambios)

    def test_historial_del_direccionamiento_sin_iniciar_atencion(self):
        ticket = self._radicar_a(self.d_area)
        eventos = list(ticket.historial.order_by("pk").values_list("tipo_evento", flat=True))
        self.assertEqual(eventos, ["RADICADO", "DIRECCIONADO"])
        datos = ticket.historial.get(tipo_evento="DIRECCIONADO").datos
        self.assertEqual(
            (datos["destino_tipo"], datos["destino_etiqueta"], datos["responsable_etiqueta"], datos["es_predeterminado"]),
            ("AREA", "Tecnología", "Soporte TIC", False),
        )
        self.assertEqual(ticket.historial.get(tipo_evento="DIRECCIONADO").actor, self.usuario_g)

    def test_el_detalle_explica_destino_y_responsable(self):
        ticket = self._radicar_a(self.d_area)
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[ticket.pk]))
        self.assertContains(respuesta, "Dirigido a:")
        self.assertContains(respuesta, "Destino: Tecnología · Responsable: Soporte TIC")

    def test_audita_el_direccionamiento(self):
        ticket = self._radicar_a(self.d_persona)
        cambio = _auditorias_de_ticket(ticket).filter(datos_nuevos__has_key="usuario_responsable_id").get()
        self.assertEqual(cambio.datos_anteriores, {"usuario_responsable_id": None, "equipo_responsable_id": None})
        self.assertEqual(cambio.datos_nuevos, {"usuario_responsable_id": self.persona_g.pk, "equipo_responsable_id": None})
        foto = RegistroAuditoria.objects.get(modelo="tickets.direccionamientoticket")
        self.assertEqual((foto.accion, foto.usuario), ("CREAR", self.usuario_g))
        self.assertEqual(foto.datos_nuevos["etiqueta"], "María Pérez")

    def test_si_el_destino_se_desactiva_antes_de_radicar_no_se_radica(self):
        from apps.tickets import direccionamiento

        ticket = self._ticket_general()
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_area.pk)
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Pedido"})
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        with self.assertRaises(ValidationError) as contexto:
            radicar_ticket(ticket, self.usuario_g)
        self.assertIn("ya no está disponible", contexto.exception.messages[0])
        ticket.refresh_from_db()
        self.assertEqual((ticket.estado, ticket.equipo_responsable_id, ticket.radicado), (Ticket.Estado.BORRADOR, None, None))
        self.assertFalse(ticket.historial.exists())

    def test_si_el_responsable_deja_de_ser_valido_no_se_radica(self):
        from apps.tickets import direccionamiento

        ticket = self._ticket_general()
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_area.pk)
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Pedido"})
        MiembroEquipo.objects.filter(equipo=self.equipo_tic).update(activo=False)
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario_g)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.BORRADOR)

    def test_un_ticket_general_nunca_se_radica_sin_poder_determinar_responsable(self):
        for destino in (self.destino_g, self.d_area, self.d_equipo, self.d_persona):
            self.destinos.desactivar_destino(self.admin_g, destino)
        ticket = self._ticket_general()
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Pedido"})
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.usuario_g)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.BORRADOR)

    def test_un_ticket_normal_no_se_dirige_ni_cambia(self):
        from apps.tickets.models import DireccionamientoTicket

        self._preparar_catalogo_normal()
        normal = crear_borrador(self.usuario_g, self.servicio_n)
        radicar_ticket(normal, self.usuario_g)
        normal.refresh_from_db()
        self.assertEqual((normal.estado, normal.usuario_responsable_id, normal.equipo_responsable_id), ("RADICADO", None, None))
        self.assertFalse(DireccionamientoTicket.objects.filter(ticket=normal).exists())
        self.assertEqual(list(normal.historial.values_list("tipo_evento", flat=True)), ["RADICADO"])


class TicketGeneralEstadoTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos()
        self._configurar_interno(
            tiempo_objetivo_cantidad=3, tiempo_objetivo_unidad="DIAS", tiempo_objetivo_habiles=False,
            politica_prorroga="SIN_APROBACION",
        )

    def test_direccionar_no_cambia_a_en_atencion(self):
        for destino in (self.d_area, self.d_equipo, self.d_persona):
            ticket = self._radicar_a(
                destino, usuario=Usuario.objects.create_user(f"sol_{destino.pk}", password=CLAVE_PRUEBA)
            )
            self.assertEqual(ticket.estado, Ticket.Estado.RADICADO, destino)
            tipos = set(ticket.historial.values_list("tipo_evento", flat=True))
            self.assertFalse(tipos & {"TOMADO", "ASIGNADO", "ATENCION_INICIADA"})

    def test_un_miembro_del_equipo_dirigido_toma_y_pasa_a_en_atencion(self):
        ticket = self._radicar_a(self.d_area)
        self.assertTrue(puede_tomar(self.miembro_tic, ticket))
        ticket = tomar_ticket(ticket, self.miembro_tic)
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual((ticket.usuario_responsable, ticket.equipo_responsable), (self.miembro_tic, self.equipo_tic))

    def test_quien_no_es_del_equipo_dirigido_no_puede_tomar(self):
        ticket = self._radicar_a(self.d_area)
        for usuario in (self.ajeno_g, self.miembro_dis, self.persona_g, self.usuario_g):
            with self.subTest(usuario.username):
                self.assertFalse(puede_tomar(usuario, ticket))
                with self.assertRaises(PermissionDenied):
                    tomar_ticket(ticket, usuario)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RADICADO)

    def test_un_responsable_global_sigue_pudiendo_tomar(self):
        ticket = self._radicar_a(self.d_area)
        self.assertEqual(tomar_ticket(ticket, self.atiende_g).estado, Ticket.Estado.EN_ATENCION)

    def test_ticket_dirigido_a_una_persona_solo_lo_inicia_esa_persona_o_quien_supervisa(self):
        ticket = self._radicar_a(self.d_persona)
        self.assertTrue(puede_iniciar_atencion(self.persona_g, ticket))
        self.assertTrue(puede_iniciar_atencion(self.atiende_g, ticket))  # alcance global de tickets.atender
        for usuario in (self.ajeno_g, self.miembro_tic, self.usuario_g):
            self.assertFalse(puede_iniciar_atencion(usuario, ticket), usuario.username)
            with self.assertRaises(PermissionDenied):
                iniciar_atencion_ticket(ticket, usuario)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RADICADO)

    def test_iniciar_atencion_cambia_el_estado_y_se_registra(self):
        ticket = self._radicar_a(self.d_persona)
        ticket = iniciar_atencion_ticket(ticket, self.persona_g)
        self.assertEqual((ticket.estado, ticket.usuario_responsable), (Ticket.Estado.EN_ATENCION, self.persona_g))
        evento = ticket.historial.get(tipo_evento="ATENCION_INICIADA")
        self.assertEqual((evento.actor, evento.datos), (self.persona_g, {"usuario_id": self.persona_g.pk}))
        cambios = [e.datos_nuevos for e in _auditorias_de_ticket(ticket) if e.datos_nuevos == {"estado": "EN_ATENCION"}]
        self.assertEqual(len(cambios), 1)

    def test_quien_supervisa_inicia_en_nombre_del_responsable_sin_reasignarlo(self):
        ticket = self._radicar_a(self.d_persona)
        ticket = iniciar_atencion_ticket(ticket, self.atiende_g)
        self.assertEqual((ticket.estado, ticket.usuario_responsable), (Ticket.Estado.EN_ATENCION, self.persona_g))

    def test_iniciar_atencion_exige_un_ticket_radicado_con_persona_responsable(self):
        con_equipo = self._radicar_a(self.d_area)
        with self.assertRaises(ValidationError):
            iniciar_atencion_ticket(con_equipo, self.atiende_g)  # a un equipo se le TOMA
        dirigido = self._radicar_a(self.d_persona, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        iniciar_atencion_ticket(dirigido, self.persona_g)
        with self.assertRaises(ValidationError):
            iniciar_atencion_ticket(dirigido, self.persona_g)  # ya está en atención

    def test_tomar_no_aplica_si_ya_hay_una_persona_responsable(self):
        ticket = self._radicar_a(self.d_persona)
        self.assertFalse(puede_tomar(self.persona_g, ticket))
        with self.assertRaises(ValidationError):
            tomar_ticket(ticket, self.persona_g)

    def test_asignar_ticket_conserva_su_contrato(self):
        # Con usuario: RADICADO → EN_ATENCION, como siempre.
        ticket = self._radicar_a(self.d_area)
        ticket = asignar_ticket(ticket, self.atiende_g, usuario=self.miembro_tic)
        self.assertEqual((ticket.estado, ticket.usuario_responsable), (Ticket.Estado.EN_ATENCION, self.miembro_tic))
        # Solo equipo: no cambia el estado.
        otro = self._radicar_a(self.d_area, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        otro = asignar_ticket(otro, self.atiende_g, equipo=self.equipo_dis)
        self.assertEqual((otro.estado, otro.equipo_responsable), (Ticket.Estado.RADICADO, self.equipo_dis))
        # Ya dirigido a una persona: no es un ticket "sin responsable" que asignar.
        persona = self._radicar_a(self.d_persona, usuario=Usuario.objects.create_user("otro2_g", password=CLAVE_PRUEBA))
        with self.assertRaises(PermissionDenied):
            asignar_ticket(persona, self.atiende_g, usuario=self.miembro_tic)

    def test_la_regla_de_tomar_de_servicios_normales_no_cambia(self):
        self._preparar_catalogo_normal()
        normal = crear_borrador(self.usuario_g, self.servicio_n)
        radicar_ticket(normal, self.usuario_g)
        normal = asignar_ticket(normal, self.atiende_g, equipo=self.equipo_tic)
        self.assertFalse(puede_tomar(self.miembro_tic, normal))  # miembro del equipo, sin ser ServicioResponsable
        with self.assertRaises(PermissionDenied):
            tomar_ticket(normal, self.miembro_tic)

    def test_las_vistas_de_tomar_e_iniciar_atencion(self):
        persona = self._radicar_a(self.d_persona)
        self.client.login(username="persona_g", password=CLAVE_PRUEBA)
        detalle = self.client.get(reverse("tickets:detalle", args=[persona.pk]))
        self.assertContains(detalle, "Iniciar atención")
        self.assertNotContains(detalle, ">Tomar<")
        self.assertEqual(self.client.get(reverse("tickets:iniciar_atencion", args=[persona.pk])).status_code, 405)
        self.client.post(reverse("tickets:iniciar_atencion", args=[persona.pk]))
        persona.refresh_from_db()
        self.assertEqual(persona.estado, Ticket.Estado.EN_ATENCION)
        self.assertNotContains(self.client.get(reverse("tickets:detalle", args=[persona.pk])), "Iniciar atención")
        equipo = self._radicar_a(self.d_area, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        self.client.login(username="miembro_tic", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tickets:detalle", args=[equipo.pk])), ">Tomar<")
        self.client.post(reverse("tickets:tomar", args=[equipo.pk]))
        equipo.refresh_from_db()
        self.assertEqual((equipo.estado, equipo.usuario_responsable), (Ticket.Estado.EN_ATENCION, self.miembro_tic))
        self.client.login(username="ajeno_g", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.post(reverse("tickets:iniciar_atencion", args=[persona.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse("tickets:detalle", args=[persona.pk])).status_code, 403)


class TicketGeneralColaDestinoTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos()

    def _ids(self, respuesta):
        return [t.pk for t in respuesta.context["tickets"]]

    def test_el_equipo_dirigido_lo_ve_y_los_ajenos_no(self):
        ticket = self._radicar_a(self.d_area)
        self.assertIn(ticket.pk, self._ids(self._cola(self.miembro_tic)))
        self.assertIn(ticket.pk, self._ids(self._cola(self.atiende_g)))
        for usuario in (self.ajeno_g, self.miembro_dis, self.persona_g, self.usuario_g):
            self.assertNotIn(ticket.pk, self._ids(self._cola(usuario)), usuario.username)

    def test_la_persona_dirigida_lo_ve_y_los_ajenos_no(self):
        ticket = self._radicar_a(self.d_persona)
        self.assertIn(ticket.pk, self._ids(self._cola(self.persona_g)))
        for usuario in (self.ajeno_g, self.miembro_tic, self.miembro_dis):
            self.assertNotIn(ticket.pk, self._ids(self._cola(usuario)), usuario.username)

    def test_un_ticket_dirigido_a_una_persona_aparece_sin_tomar_hasta_iniciar(self):
        from apps.tickets.trabajo import tickets_a_cargo

        ticket = self._radicar_a(self.d_persona)
        self.assertIn(ticket.pk, self._ids(self._cola(self.persona_g)))
        self.assertNotIn(ticket.pk, [t.pk for t in tickets_a_cargo(self.persona_g)])
        iniciar_atencion_ticket(ticket, self.persona_g)
        # Iniciada la atención deja la Cola y pasa a «Mi trabajo».
        self.assertNotIn(ticket.pk, self._ids(self._cola(self.persona_g)))
        self.assertIn(ticket.pk, [t.pk for t in tickets_a_cargo(self.persona_g)])

    def test_la_cola_sigue_ordenada_por_llegada_con_los_tickets_generales(self):
        primero = self._radicar_a(self.d_area)
        normal_usuario = Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA)
        segundo = self._radicar_a(self.d_area, usuario=normal_usuario)
        self.assertEqual(self._ids(self._cola(self.atiende_g)), [primero.pk, segundo.pk])

    def test_quien_toma_saca_el_ticket_de_la_cola_y_lo_lleva_a_su_trabajo(self):
        from apps.tickets.trabajo import tickets_a_cargo

        ticket = self._radicar_a(self.d_area)
        tomar_ticket(ticket, self.miembro_tic)
        self.assertNotIn(ticket.pk, self._ids(self._cola(self.miembro_tic)))
        self.assertIn(ticket.pk, [t.pk for t in tickets_a_cargo(self.miembro_tic)])

    def test_los_ajenos_no_pueden_ver_el_detalle(self):
        ticket = self._radicar_a(self.d_area)
        self.client.login(username="ajeno_g", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(reverse("tickets:detalle", args=[ticket.pk])).status_code, 403)
        self.client.login(username="miembro_tic", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tickets:detalle", args=[ticket.pk])), "Dirigido a:")


class TicketGeneralDestinoHistoricoTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos()

    def test_cambiar_el_responsable_configurado_no_reasigna_tickets_existentes(self):
        ticket = self._radicar_a(self.d_area)
        self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.miembro_dis)
        ticket.refresh_from_db()
        self.assertEqual((ticket.equipo_responsable, ticket.usuario_responsable), (self.equipo_tic, None))
        self.assertIn(ticket.pk, [t.pk for t in self._cola(self.miembro_tic).context["tickets"]])

    def test_desactivar_el_destino_no_rompe_tickets_existentes(self):
        ticket = self._radicar_a(self.d_area)
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)
        self.assertContains(self.client.get(reverse("tickets:detalle", args=[ticket.pk])), "Tecnología")
        self.assertEqual(tomar_ticket(ticket, self.miembro_tic).estado, Ticket.Estado.EN_ATENCION)
        otro = self._ticket_general(Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        from apps.tickets import direccionamiento

        with self.assertRaises(ValidationError):
            direccionamiento.seleccionar_destino_borrador(otro, otro.solicitante, self.d_area.pk)

    def test_renombrar_o_inactivar_lo_dirigido_no_reescribe_la_foto(self):
        from apps.tickets.models import DireccionamientoTicket

        ticket = self._radicar_a(self.d_area)
        persona = self._radicar_a(self.d_persona, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        Area.objects.filter(pk=self.area_tic.pk).update(nombre="Nuevo nombre", activo=False)
        Usuario.objects.filter(pk=self.persona_g.pk).update(first_name="Otra", is_active=False)
        self.assertEqual(DireccionamientoTicket.objects.get(ticket=ticket).etiqueta, "Tecnología")
        self.assertEqual(DireccionamientoTicket.objects.get(ticket=persona).etiqueta, "María Pérez")
        self.assertEqual(ticket.historial.get(tipo_evento="DIRECCIONADO").datos["destino_etiqueta"], "Tecnología")
        self.assertEqual(self.destinos._consulta().get(pk=self.d_area.pk).etiqueta, "Nuevo nombre")

    def test_el_historial_de_un_ticket_no_cambia_si_se_cambia_el_destino_despues(self):
        ticket = self._radicar_a(self.d_area)
        antes = list(ticket.historial.values_list("tipo_evento", "datos"))
        self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.equipo_dis)
        self.destinos.desactivar_destino(self.admin_g, self.d_area)
        self.assertEqual(list(ticket.historial.values_list("tipo_evento", "datos")), antes)

    def test_un_destino_con_historial_no_se_elimina_fisicamente(self):
        from django.db.models import ProtectedError

        self._radicar_a(self.d_area)
        with self.assertRaises(ProtectedError):
            self.d_area.delete()

    def _cola(self, usuario):
        self.client.login(username=usuario.username, password=CLAVE_PRUEBA)
        return self.client.get(reverse("tickets:cola"))


class TicketGeneralDestinoRegresionTests(_EscenarioDestinosMixin, TestCase):
    def setUp(self):
        self._preparar_destinos()
        self._configurar_interno(
            tiempo_objetivo_cantidad=3, tiempo_objetivo_unidad="DIAS", tiempo_objetivo_habiles=False,
            politica_prorroga="SIN_APROBACION",
        )

    def test_el_tiempo_objetivo_y_la_prorroga_siguen_viniendo_del_servicio_interno(self):
        ticket = self._radicar_a(self.d_persona)
        self.assertEqual((ticket.tiempo_objetivo_cantidad, ticket.tiempo_objetivo_unidad), (3, "DIAS"))
        self.assertIsNotNone(ticket.fecha_objetivo_original)
        self.assertEqual(ticket.fecha_objetivo_original, ticket.fecha_objetivo_vigente)
        self.assertEqual(ticket.prorroga_politica, "SIN_APROBACION")
        # Mismo compromiso para cualquier destino.
        otro = self._radicar_a(self.d_area, usuario=Usuario.objects.create_user("otro_g", password=CLAVE_PRUEBA))
        self.assertEqual(otro.tiempo_objetivo_cantidad, ticket.tiempo_objetivo_cantidad)
        self.assertEqual(otro.entrega_politica, ticket.entrega_politica)

    def test_una_vez_en_atencion_se_solicita_prorroga_como_en_cualquier_ticket(self):
        from apps.tickets import prorrogas

        ticket = self._radicar_a(self.d_persona)
        ticket = iniciar_atencion_ticket(ticket, self.persona_g)
        prorroga = prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), self.persona_g,
            nueva_fecha=ticket.fecha_objetivo_vigente + timedelta(days=2), motivo="Más tiempo",
        )
        self.assertEqual(prorroga.estado, "APROBADA")

    def test_el_ticket_dirigido_a_un_equipo_completa_el_ciclo(self):
        from apps.tickets import prorrogas

        ticket = tomar_ticket(self._radicar_a(self.d_area), self.miembro_tic)
        prorroga = prorrogas.solicitar_prorroga(
            Ticket.objects.get(pk=ticket.pk), self.miembro_tic,
            nueva_fecha=ticket.fecha_objetivo_vigente + timedelta(days=1), motivo="Más tiempo",
        )
        self.assertEqual(prorroga.estado, "APROBADA")
        resolver_ticket(Ticket.objects.get(pk=ticket.pk), self.miembro_tic, "Resuelto")
        cerrar_ticket(Ticket.objects.get(pk=ticket.pk), self.usuario_g)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.CERRADO)

    def test_los_servicios_normales_y_su_catalogo_no_cambian(self):
        from apps.catalogo.visibilidad import servicios_visibles_para

        self._preparar_catalogo_normal()
        self.assertEqual(
            {s.pk for s in servicios_visibles_para(self.usuario_g)}, {self.servicio_n.pk, self.proceso_n.pk}
        )
        normal = crear_borrador(self.usuario_g, self.servicio_n)
        radicar_ticket(normal, self.usuario_g)
        normal = tomar_ticket(normal, self.atiende_g)
        self.assertEqual(normal.estado, Ticket.Estado.EN_ATENCION)

    def test_no_se_crea_ningun_workflow_ni_formulario_por_destino(self):
        from apps.catalogo.models import Formulario
        from apps.workflows.models import InstanciaWorkflow, Workflow

        antes = (Workflow.objects.count(), InstanciaWorkflow.objects.count(), Formulario.objects.count(), Servicio.objects.count())
        self._radicar_a(self.d_area)
        self.assertEqual(
            (Workflow.objects.count(), InstanciaWorkflow.objects.count(), Formulario.objects.count(), Servicio.objects.count()),
            antes,
        )

    def test_el_ticket_general_conserva_su_servicio_unico(self):
        self.assertEqual(Servicio.objects.filter(es_ticket_general=True).count(), 1)
        for destino in (self.d_area, self.d_equipo, self.d_persona):
            ticket = self._radicar_a(
                destino, usuario=Usuario.objects.create_user(f"sol_{destino.pk}", password=CLAVE_PRUEBA)
            )
            self.assertEqual(ticket.detalle_servicio.servicio, self.servicio_g)


class TicketGeneralDestinoConcurrenciaTests(_EscenarioDestinosMixin, TransactionTestCase):
    def setUp(self):
        self._preparar_destinos()

    def _correr(self, tareas):
        resultados = {}
        barrera = threading.Barrier(len(tareas))

        def _hilo(clave, funcion):
            barrera.wait()
            try:
                funcion()
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "rechazada"
            finally:
                connection.close()

        hilos = [threading.Thread(target=_hilo, args=(c, f)) for c, f in tareas.items()]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        return resultados

    def test_dos_destinos_de_reserva_a_la_vez_dejan_uno_solo(self):
        from apps.catalogo.models import ConfiguracionTicketGeneral

        resultados = self._correr({
            "a": lambda: self.destinos.definir_predeterminado(self.admin_g, self.d_area),
            "b": lambda: self.destinos.definir_predeterminado(self.admin_g, self.d_equipo),
        })
        self.assertEqual(resultados, {"a": "ok", "b": "ok"})
        self.assertEqual(ConfiguracionTicketGeneral.objects.count(), 1)
        self.assertIn(ConfiguracionTicketGeneral.actual().destino_predeterminado_id, {self.d_area.pk, self.d_equipo.pk})

    def test_ediciones_concurrentes_del_responsable_dejan_un_estado_coherente(self):
        resultados = self._correr({
            "a": lambda: self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.equipo_dis),
            "b": lambda: self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.miembro_tic),
        })
        self.assertEqual(resultados, {"a": "ok", "b": "ok"})
        destino = self.destinos._consulta().get(pk=self.d_area.pk)
        self.assertEqual((destino.responsable_usuario_id is None) != (destino.responsable_equipo_id is None), True)

    def test_la_radicacion_frente_a_la_desactivacion_es_consistente(self):
        from apps.tickets import direccionamiento
        from apps.tickets.models import DireccionamientoTicket

        ticket = self._ticket_general()
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_area.pk)
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Pedido"})
        resultados = self._correr({
            "radicar": lambda: radicar_ticket(Ticket.objects.get(pk=ticket.pk), self.usuario_g),
            "desactivar": lambda: self.destinos.desactivar_destino(self.admin_g, self.d_area),
        })
        self.assertEqual(resultados["desactivar"], "ok")
        ticket.refresh_from_db()
        self.d_area.refresh_from_db()
        self.assertFalse(self.d_area.activo)
        if resultados["radicar"] == "ok":  # radicó antes de desactivarse: ticket completo y dirigido
            self.assertEqual((ticket.estado, ticket.equipo_responsable), (Ticket.Estado.RADICADO, self.equipo_tic))
            self.assertEqual(DireccionamientoTicket.objects.get(ticket=ticket).etiqueta, "Tecnología")
        else:  # el destino ya estaba inactivo: el ticket sigue intacto como borrador
            self.assertEqual((ticket.estado, ticket.equipo_responsable_id, ticket.radicado), (Ticket.Estado.BORRADOR, None, None))
            self.assertFalse(DireccionamientoTicket.objects.get(ticket=ticket).esta_fijado)

    def test_la_radicacion_frente_al_cambio_de_responsable_usa_uno_de_los_dos(self):
        from apps.tickets import direccionamiento

        ticket = self._ticket_general()
        direccionamiento.seleccionar_destino_borrador(ticket, self.usuario_g, self.d_area.pk)
        guardar_respuestas_borrador(ticket, self.usuario_g, {self.campos_g["Asunto"].id: "Pedido"})
        resultados = self._correr({
            "radicar": lambda: radicar_ticket(Ticket.objects.get(pk=ticket.pk), self.usuario_g),
            "cambiar": lambda: self.destinos.cambiar_responsable(self.admin_g, self.d_area, responsable=self.equipo_dis),
        })
        self.assertEqual(resultados, {"radicar": "ok", "cambiar": "ok"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertIn(ticket.equipo_responsable_id, {self.equipo_tic.pk, self.equipo_dis.pk})


class TicketGeneralBuscadorTests(_EscenarioTicketGeneralMixin, TestCase):
    """4.D — el buscador "¿Qué necesitas?" y el Ticket General: el Servicio interno
    nunca es un resultado y la entrada "Crear ticket general" se ofrece como acción
    alterna reutilizando la disponibilidad de 4.C1/4.C2 (sin duplicar sus reglas)."""

    CTA = "Crear ticket general"

    def setUp(self):
        self._preparar_general(habilitar=True)
        self._preparar_catalogo_normal()
        self.client.login(username="usuario_g", password=CLAVE_PRUEBA)

    def _buscar(self, q, *, fragmento=False):
        extra = {"HTTP_X_REQUESTED_WITH": "fetch"} if fragmento else {}
        return self.client.get(reverse("core:necesidad"), {"q": q}, **extra)

    def test_sin_coincidencias_se_ofrece_el_ticket_general(self):
        respuesta = self._buscar("zzzz qqqq")
        self.assertEqual(respuesta.context["resultados"], [])
        self.assertContains(respuesta, "No encontramos un servicio o proceso que coincida suficientemente")
        self.assertContains(respuesta, self.CTA)
        self.assertContains(respuesta, f'href="{reverse("tickets:ticket_general")}"')

    def test_con_coincidencias_el_ticket_general_es_una_alternativa_secundaria(self):
        respuesta = self._buscar("servicio ordinario")
        self.assertIn(self.servicio_n.pk, [r.servicio.pk for r in respuesta.context["resultados"]])
        contenido = respuesta.content.decode()
        self.assertIn("¿Ninguna opción corresponde a lo que necesitas?", contenido)
        self.assertIn(self.CTA, contenido)
        # Los resultados catalogados van primero; la alternativa, después.
        self.assertLess(contenido.index("Servicio ordinario visible"), contenido.index(self.CTA))
        self.assertLess(contenido.index("Esto puede ayudarte"), contenido.index("¿Ninguna opción"))

    def test_el_servicio_interno_nunca_es_un_resultado(self):
        from apps.catalogo.models import TerminoServicio

        TerminoServicio.objects.create(servicio=self.servicio_g, termino="interno reservado")
        for q in ("servicio interno reservado", "interno reservado", "Para solicitar algo que no está catalogado"):
            respuesta = self._buscar(q)
            self.assertNotIn(self.servicio_g.pk, [r.servicio.pk for r in respuesta.context["resultados"]], q)
            self.assertNotContains(self._buscar(q, fragmento=True), self.NOMBRE_INTERNO)

    def test_deshabilitado_no_hay_entrada_pero_si_el_mensaje(self):
        self.general.configurar_habilitacion(self.admin_g, habilitado=False)
        for q in ("zzzz qqqq", "servicio ordinario"):
            respuesta = self._buscar(q)
            self.assertNotContains(respuesta, self.CTA)
            self.assertNotContains(respuesta, "Ninguna opción corresponde")
        self.assertContains(self._buscar("zzzz qqqq"), "Prueba con otras palabras")

    def test_sin_destinos_utilizables_no_hay_entrada(self):
        self.destinos.desactivar_destino(self.admin_g, self.destino_g)
        self.assertIsNone(self.general.servicio_disponible_para(self.usuario_g))
        for q in ("zzzz qqqq", "servicio ordinario"):
            self.assertNotContains(self._buscar(q), self.CTA)

    def test_sin_acceso_o_sin_formulario_activo_no_hay_entrada(self):
        self._configurar_interno(alcance_visibilidad="RESTRINGIDO")
        self.assertNotContains(self._buscar("zzzz qqqq"), self.CTA)
        self._configurar_interno(alcance_visibilidad="PUBLICO_INTERNO")
        self.assertContains(self._buscar("zzzz qqqq"), self.CTA)
        Formulario.objects.filter(pk=self.servicio_g.formulario_id).update(version_activa=None)
        self.assertNotContains(self._buscar("zzzz qqqq"), self.CTA)

    def test_la_consulta_invalida_no_ofrece_el_ticket_general(self):
        respuesta = self._buscar("   ")
        self.assertNotContains(respuesta, self.CTA)
        self.assertContains(respuesta, "Escribe lo que necesitas.")

    def test_buscar_no_crea_tickets_ni_direccionamientos(self):
        from apps.tickets.models import DireccionamientoTicket

        self._buscar("zzzz qqqq")
        self._buscar("servicio ordinario", fragmento=True)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(DireccionamientoTicket.objects.count(), 0)

    def test_la_entrada_ofrecida_sigue_siendo_la_de_4c1(self):
        respuesta = self.client.get(reverse("tickets:ticket_general"))
        ticket = Ticket.objects.get()
        self.assertRedirects(respuesta, reverse("tickets:borrador", args=[ticket.pk]))

    def test_un_servicio_encontrado_se_solicita_por_la_via_normal(self):
        respuesta = self.client.get(reverse("tickets:solicitar", args=[self.servicio_n.pk]))
        ticket = Ticket.objects.get()
        self.assertRedirects(respuesta, reverse("tickets:borrador", args=[ticket.pk]))
        self.assertEqual(ticket.detalle_servicio.servicio, self.servicio_n)

    def test_el_ticket_general_sigue_radicando_y_direccionando(self):
        from apps.tickets.models import DireccionamientoTicket

        self._buscar("zzzz qqqq")
        ticket = self._radicar_general()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(ticket.equipo_responsable, self.equipo_g)
        self.assertTrue(DireccionamientoTicket.objects.get(ticket=ticket).esta_fijado)

    def test_el_servicio_interno_sigue_fuera_del_catalogo_ordinario(self):
        from apps.catalogo.visibilidad import servicios_visibles_para

        self.assertNotIn(self.servicio_g.pk, [s.pk for s in servicios_visibles_para(self.usuario_g)])
        with self.assertRaises(PermissionDenied):
            crear_borrador(self.usuario_g, self.servicio_g)


class _EscenarioFlujoFasesMixin:
    """Plantilla de fases publicada (Recepción → Revisión) + usuarios + helpers para
    configurar bloques y radicar tickets reales sobre ella (4.B0/4.B1)."""

    def _preparar_flujo(self):
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version as activar_workflow

        self.solicitante = Usuario.objects.create_user(username="var_solicitante", password=CLAVE_PRUEBA)
        self.aprobador = Usuario.objects.create_user(username="var_aprobador", password=CLAVE_PRUEBA)
        self.admin = Usuario.objects.create_user(username="var_admin", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.admin, "catalogo.administrar")
        self.workflow = fases_ops.crear_plantilla_fases(self.admin, nombre="Plantilla con variables")
        version = self.workflow.versiones.get(numero=1)
        self.recepcion = fases_ops.agregar_fase(version, self.admin, nombre="Recepción")
        self.revision = fases_ops.agregar_fase(version, self.admin, nombre="Revisión")
        fases_ops.conectar_fases(self.recepcion, self.revision, self.admin)
        activar_workflow(self.workflow, version, self.admin)

    # --- construcción -----------------------------------------------------------

    def _servicio(self, campos_spec=None):
        T = Campo.TipoCampo
        if campos_spec is None:
            campos_spec = [
                {"tipo": T.NUMERO, "etiqueta": "Valor estimado", "orden": 1},
                {"tipo": T.BOOLEANO, "etiqueta": "Requiere aprobación especial", "orden": 2},
                {"tipo": T.TEXTO, "etiqueta": "Tipo cliente", "orden": 3},
                {"tipo": T.FECHA, "etiqueta": "Fecha límite", "orden": 4},
                {"tipo": T.TEXTO, "etiqueta": "Comentario", "orden": 5},
            ]
        servicio, _, campos = _crear_servicio_con_formulario(self.solicitante, campos_spec)
        servicio.workflow = self.workflow
        servicio.save(update_fields=["workflow", "actualizado_en"])
        return servicio, campos

    def _agregar(self, config, fase, tipo, nombre, **configuracion):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        return agregar_bloque_operativo(
            config, self.admin, fase=fase, tipo=tipo, nombre=nombre, configuracion=configuracion or {}
        )

    def _actividad(self, config, nombre):
        return self._agregar(config, self.revision, BloqueOperativo.Tipo.ACTIVIDAD, nombre, tipo_actor="SOLICITANTE")

    def _aprobacion_bloque(self, config, nombre):
        return self._agregar(
            config, self.recepcion, BloqueOperativo.Tipo.APROBACION, nombre, modo="SECUENCIAL",
            participantes=[{"tipo": "USUARIO", "usuario_id": self.aprobador.pk, "equipo_id": None}],
        )

    def _rutas(self, origen, destino, **por_resultado):
        from apps.catalogo.configuracion_ejecucion import conectar_bloques_operativos

        for resultado in ("APROBADA", "RECHAZADA", "DEVUELTA"):
            conectar_bloques_operativos(
                origen, por_resultado.get(resultado, destino), self.admin, resultado_aprobacion=resultado
            )

    def _decision(self, config, condiciones, *, alto, normal, nombre="Clasificar"):
        """DECISION con `condiciones` = [(variable, operador, valor)] hacia `alto` y el
        fallback hacia `normal`."""
        from apps.catalogo.configuracion_ejecucion import conectar_bloques_operativos

        decision = self._agregar(config, self.recepcion, BloqueOperativo.Tipo.DECISION, nombre)
        for prioridad, (variable, operador, valor) in enumerate(condiciones):
            conectar_bloques_operativos(
                decision, alto, self.admin, variable=variable, operador=operador, valor=valor, prioridad=prioridad
            )
        conectar_bloques_operativos(decision, normal, self.admin, es_fallback=True)
        return decision

    def _activar(self, servicio, config):
        from apps.catalogo.configuracion_ejecucion import activar_configuracion_ejecucion

        activar_configuracion_ejecucion(servicio, config, self.admin)
        servicio.refresh_from_db()

    def _servicio_con_decision(self, condiciones, campos_spec=None):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        servicio, campos = self._servicio(campos_spec)
        config = crear_nueva_version_configuracion(servicio, self.admin)
        alto = self._actividad(config, "Camino alto")
        normal = self._actividad(config, "Camino normal")
        # La DECISION va primero en su fase para ser el bloque inicial.
        self._decision(config, condiciones, alto=alto, normal=normal)
        self._verificar_decision_inicial(config)
        self._activar(servicio, config)
        return servicio, campos

    def _verificar_decision_inicial(self, config):
        """Las actividades viven en Revisión; la decisión es el único bloque de Recepción
        y por tanto el bloque inicial de la ejecución."""
        self.assertEqual(config.bloques.filter(fase=self.recepcion).count(), 1)

    def _radicar(self, servicio, campos, respuestas=None, usuario=None):
        usuario = usuario or self.solicitante
        ticket = crear_borrador(usuario, servicio)
        crudas = {campos[etiqueta].id: valor for etiqueta, valor in (respuestas or {}).items()}
        if crudas:
            guardar_respuestas_borrador(ticket, usuario, crudas)
        radicar_ticket(ticket, usuario)
        ticket.refresh_from_db()
        return ticket

    def _camino(self, ticket):
        ultimo = ticket.instancia_workflow.ejecuciones_etapa.order_by("-orden").first()
        return ultimo.bloque_operativo.nombre

    def _aprobacion(self, ticket, nombre_bloque):
        from apps.workflows.models import EsquemaAprobacionWorkflow

        vinculo = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=ticket.instancia_workflow,
            instancia_etapa__bloque_operativo__nombre=nombre_bloque,
        )
        return vinculo.esquema.participaciones.get()

    def _resolver(self, ticket, nombre_bloque, decision):
        from apps.workflows.integracion import resolver_aprobacion_workflow

        observacion = "" if decision == "APROBADA" else "Motivo de la decisión."
        resolver_aprobacion_workflow(
            self._aprobacion(ticket, nombre_bloque), self.aprobador, decision=decision, observacion=observacion
        )
        ticket.refresh_from_db()
        ticket.instancia_workflow.refresh_from_db()


class VariablesWorkflowTicketTests(_EscenarioFlujoFasesMixin, TestCase):
    """4.B0 — una DECISION de un flujo por fases (PLANTILLA_FASES) evalúa datos del
    Ticket, respuestas del formulario congelado (por clave estable) y resultados de
    aprobaciones (por clave de bloque), con ticket real y radicación real."""

    def setUp(self):
        self._preparar_flujo()

    # --- respuestas del formulario -------------------------------------------------

    def test_un_numero_del_formulario_se_compara_como_numero(self):
        servicio, campos = self._servicio_con_decision([("formulario.valor_estimado", "MAYOR_QUE", "9")])
        # Como texto, "10" < "9": comparado como número 10 > 9.
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Valor estimado": 10})), "Camino alto")
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Valor estimado": 2})), "Camino normal")

    def test_un_booleano_del_formulario_conserva_su_tipo(self):
        servicio, campos = self._servicio_con_decision(
            [("formulario.requiere_aprobacion_especial", "IGUAL_A", "Sí")]
        )
        self.assertEqual(
            self._camino(self._radicar(servicio, campos, {"Requiere aprobación especial": True})), "Camino alto"
        )
        self.assertEqual(
            self._camino(self._radicar(servicio, campos, {"Requiere aprobación especial": False})), "Camino normal"
        )

    def test_un_texto_del_formulario(self):
        servicio, campos = self._servicio_con_decision([("formulario.tipo_cliente", "IGUAL_A", "Premium")])
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Tipo cliente": "Premium"})), "Camino alto")
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Tipo cliente": "Basico"})), "Camino normal")

    def test_una_fecha_del_formulario(self):
        servicio, campos = self._servicio_con_decision([("formulario.fecha_limite", "MENOR_QUE", "2026-12-31")])
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Fecha límite": "2026-06-30"})), "Camino alto")
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Fecha límite": "2027-01-15"})), "Camino normal")

    def test_un_campo_inexistente_nunca_coincide_y_cae_en_el_fallback(self):
        for operador in ("IGUAL_A", "DISTINTO_DE", "NO_CONTIENE", "ESTA_VACIO", "MAYOR_QUE"):
            servicio, campos = self._servicio_con_decision([("formulario.no_existe", operador, "x")])
            ticket = self._radicar(servicio, campos)
            self.assertEqual(self._camino(ticket), "Camino normal", operador)
            self.assertNotEqual(ticket.instancia_workflow.estado, "ERROR", operador)

    def test_campo_existente_sin_respuesta_es_null_y_no_inexistente(self):
        servicio, campos = self._servicio_con_decision([("formulario.comentario", "ESTA_VACIO", "-")])
        self.assertEqual(self._camino(self._radicar(servicio, campos)), "Camino alto")
        self.assertEqual(self._camino(self._radicar(servicio, campos, {"Comentario": "hola"})), "Camino normal")

    def test_varias_variables_en_una_misma_decision_respetan_la_prioridad(self):
        servicio, campos = self._servicio_con_decision([
            ("formulario.tipo_cliente", "IGUAL_A", "Premium"),
            ("formulario.valor_estimado", "MAYOR_QUE", "100"),
        ])
        self.assertEqual(
            self._camino(self._radicar(servicio, campos, {"Tipo cliente": "Basico", "Valor estimado": 500})),
            "Camino alto",
        )
        self.assertEqual(
            self._camino(self._radicar(servicio, campos, {"Tipo cliente": "Basico", "Valor estimado": 5})),
            "Camino normal",
        )

    # --- datos del Ticket -----------------------------------------------------------

    def test_datos_basicos_del_ticket(self):
        casos = [
            ("ticket.estado", "IGUAL_A", "BORRADOR"),  # la radicación aún no cambió el estado
            ("ticket.tipo", "IGUAL_A", "SERVICIO"),
            ("ticket.es_general", "IGUAL_A", "no"),
            ("ticket.solicitante", "IGUAL_A", "var_solicitante"),
            ("ticket.responsable", "ESTA_VACIO", "-"),
        ]
        for caso in casos:
            servicio, campos = self._servicio_con_decision([caso])
            self.assertEqual(self._camino(self._radicar(servicio, campos)), "Camino alto", caso)

    def test_un_dato_del_ticket_inexistente_cae_en_el_fallback(self):
        servicio, campos = self._servicio_con_decision([("ticket.no_existe", "DISTINTO_DE", "x")])
        self.assertEqual(self._camino(self._radicar(servicio, campos)), "Camino normal")

    def test_el_estado_del_ticket_se_lee_en_vivo_despues_de_la_radicacion(self):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        servicio, campos = self._servicio()
        config = crear_nueva_version_configuracion(servicio, self.admin)
        aprobacion = self._aprobacion_bloque(config, "Aprobación jefe")
        alto = self._actividad(config, "Camino alto")
        normal = self._actividad(config, "Camino normal")
        decision = self._decision(config, [("ticket.estado", "IGUAL_A", "RADICADO")], alto=alto, normal=normal)
        self._rutas(aprobacion, decision)
        self._activar(servicio, config)

        ticket = self._radicar(servicio, campos)
        self._resolver(ticket, "Aprobación jefe", "APROBADA")
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(self._camino(ticket), "Camino alto")

    # --- Workflow compartido ---------------------------------------------------------------

    def test_workflow_compartido_con_un_servicio_sin_el_campo_usa_el_fallback(self):
        T = Campo.TipoCampo
        servicio_a, campos_a = self._servicio_con_decision([("formulario.valor_estimado", "MAYOR_QUE", "9")])
        servicio_b, campos_b = self._servicio_con_decision(
            [("formulario.valor_estimado", "MAYOR_QUE", "9")],
            campos_spec=[{"tipo": T.TEXTO, "etiqueta": "Asunto", "orden": 1}],
        )
        self.assertEqual(servicio_a.workflow_id, servicio_b.workflow_id)

        ticket_a = self._radicar(servicio_a, campos_a, {"Valor estimado": 50})
        ticket_b = self._radicar(servicio_b, campos_b, {"Asunto": "algo"})
        self.assertEqual(self._camino(ticket_a), "Camino alto")
        self.assertEqual(self._camino(ticket_b), "Camino normal")
        self.assertNotEqual(ticket_b.instancia_workflow.estado, "ERROR")

    # --- versionamiento del formulario ----------------------------------------------------------

    def test_renombrar_la_etiqueta_en_una_version_nueva_no_rompe_la_referencia(self):
        servicio, campos = self._servicio_con_decision([("formulario.valor_estimado", "MAYOR_QUE", "9")])
        formulario = servicio.formulario
        # Este ticket congela la versión 1 al crear su borrador.
        ticket_v1 = crear_borrador(self.solicitante, servicio)

        nueva = crear_nueva_version(formulario, self.admin)
        campo_nuevo = nueva.campos.get(clave="valor_estimado")
        campo_nuevo.etiqueta = "Valor total estimado"
        campo_nuevo.save()
        activar_version(formulario, nueva, self.admin)
        self.assertEqual(campo_nuevo.clave, "valor_estimado")

        # La ejecución del ticket congelado sigue resolviendo contra SU versión (v1).
        guardar_respuestas_borrador(ticket_v1, self.solicitante, {campos["Valor estimado"].id: 20})
        radicar_ticket(ticket_v1, self.solicitante)
        ticket_v1.refresh_from_db()
        self.assertEqual(ticket_v1.detalle_servicio.formulario_version_id, campos["Valor estimado"].version_id)
        self.assertEqual(self._camino(ticket_v1), "Camino alto")

        # Un ticket nuevo usa la versión 2 con la etiqueta cambiada y la misma clave.
        ticket_v2 = crear_borrador(self.solicitante, servicio)
        guardar_respuestas_borrador(ticket_v2, self.solicitante, {campo_nuevo.id: 20})
        radicar_ticket(ticket_v2, self.solicitante)
        ticket_v2.refresh_from_db()
        self.assertEqual(ticket_v2.detalle_servicio.formulario_version_id, nueva.pk)
        self.assertEqual(self._camino(ticket_v2), "Camino alto")

    # --- resultados de aprobación ------------------------------------------------------------------

    def _servicio_con_dos_aprobaciones(self):
        """Aprobación jefe → Aprobación finanzas → DECISION sobre ambos resultados.
        Cualquier resultado de cada aprobación continúa a la siguiente."""
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        servicio, campos = self._servicio()
        config = crear_nueva_version_configuracion(servicio, self.admin)
        jefe = self._aprobacion_bloque(config, "Aprobación jefe")
        finanzas = self._aprobacion_bloque(config, "Aprobación finanzas")
        a_finanzas = self._actividad(config, "Camino finanzas rechazó")
        a_jefe = self._actividad(config, "Camino jefe aprobó")
        a_fallback = self._actividad(config, "Camino fallback")
        decision = self._agregar(config, self.recepcion, BloqueOperativo.Tipo.DECISION, "Clasificar")
        from apps.catalogo.configuracion_ejecucion import conectar_bloques_operativos

        conectar_bloques_operativos(
            decision, a_finanzas, self.admin, variable="aprobaciones.aprobacion_finanzas.resultado",
            operador="IGUAL_A", valor="RECHAZADA", prioridad=0,
        )
        conectar_bloques_operativos(
            decision, a_jefe, self.admin, variable="aprobaciones.aprobacion_jefe.resultado",
            operador="IGUAL_A", valor="APROBADA", prioridad=1,
        )
        conectar_bloques_operativos(decision, a_fallback, self.admin, es_fallback=True)
        self._rutas(jefe, finanzas)
        self._rutas(finanzas, decision)
        self._activar(servicio, config)
        return servicio, campos

    def test_cada_aprobacion_queda_direccionable_por_su_propia_clave(self):
        servicio, campos = self._servicio_con_dos_aprobaciones()
        ticket = self._radicar(servicio, campos)
        self._resolver(ticket, "Aprobación jefe", "APROBADA")
        self._resolver(ticket, "Aprobación finanzas", "RECHAZADA")

        resultados = ticket.instancia_workflow.contexto["resultados_bloques"]["aprobaciones"]
        self.assertEqual(resultados["aprobacion_jefe"], {"resultado": "APROBADA"})
        self.assertEqual(resultados["aprobacion_finanzas"], {"resultado": "RECHAZADA"})
        # Cada decisión consulta SU bloque: gana la condición de prioridad 0 (finanzas).
        self.assertEqual(self._camino(ticket), "Camino finanzas rechazó")

    def test_la_decision_distingue_entre_dos_aprobaciones_con_resultados_distintos(self):
        servicio, campos = self._servicio_con_dos_aprobaciones()
        ticket = self._radicar(servicio, campos)
        self._resolver(ticket, "Aprobación jefe", "APROBADA")
        self._resolver(ticket, "Aprobación finanzas", "APROBADA")
        self.assertEqual(self._camino(ticket), "Camino jefe aprobó")

        otro = self._radicar(servicio, campos)
        self._resolver(otro, "Aprobación jefe", "RECHAZADA")
        self._resolver(otro, "Aprobación finanzas", "APROBADA")
        self.assertEqual(self._camino(otro), "Camino fallback")

    def test_publicar_un_resultado_conserva_el_resto_del_contexto(self):
        servicio, campos = self._servicio_con_dos_aprobaciones()
        ticket = self._radicar(servicio, campos)
        antes = dict(ticket.instancia_workflow.contexto)
        self._resolver(ticket, "Aprobación jefe", "DEVUELTA")
        despues = ticket.instancia_workflow.contexto
        self.assertEqual(despues["datos_iniciales"], antes["datos_iniciales"])
        self.assertEqual(despues["resultados_bloques"]["aprobaciones"]["aprobacion_jefe"], {"resultado": "DEVUELTA"})

    def test_resultado_aprobacion_sigue_eligiendo_la_ruta_y_ademas_se_publica(self):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        servicio, campos = self._servicio()
        config = crear_nueva_version_configuracion(servicio, self.admin)
        jefe = self._aprobacion_bloque(config, "Aprobación jefe")
        aprobada = self._actividad(config, "Ruta aprobada")
        rechazada = self._actividad(config, "Ruta rechazada")
        devuelta = self._actividad(config, "Ruta devuelta")
        self._rutas(jefe, aprobada, RECHAZADA=rechazada, DEVUELTA=devuelta)
        self._activar(servicio, config)

        for decision, ruta in (("APROBADA", "Ruta aprobada"), ("RECHAZADA", "Ruta rechazada"), ("DEVUELTA", "Ruta devuelta")):
            ticket = self._radicar(servicio, campos)
            self._resolver(ticket, "Aprobación jefe", decision)
            self.assertEqual(self._camino(ticket), ruta)
            publicado = ticket.instancia_workflow.contexto["resultados_bloques"]["aprobaciones"]["aprobacion_jefe"]
            self.assertEqual(publicado["resultado"], decision)

    def test_una_ejecucion_terminada_no_puede_volver_a_publicar(self):
        from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaEtapa
        from apps.workflows.motor import continuar_espera_externa

        servicio, campos = self._servicio_con_dos_aprobaciones()
        ticket = self._radicar(servicio, campos)
        self._resolver(ticket, "Aprobación jefe", "APROBADA")
        ejecucion = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=ticket.instancia_workflow,
            instancia_etapa__bloque_operativo__nombre="Aprobación jefe",
        ).instancia_etapa
        with self.assertRaises(ValueError):
            continuar_espera_externa(
                ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.APROBACION,
                resultado_bloque=("aprobaciones", {"resultado": "RECHAZADA"}),
            )
        ticket.instancia_workflow.refresh_from_db()
        publicado = ticket.instancia_workflow.contexto["resultados_bloques"]["aprobaciones"]["aprobacion_jefe"]
        self.assertEqual(publicado, {"resultado": "APROBADA"})

    def test_renombrar_un_bloque_en_una_configuracion_nueva_no_rompe_la_referencia(self):
        from apps.catalogo.configuracion_ejecucion import (
            crear_nueva_version_configuracion,
            editar_bloque_operativo,
        )

        servicio, campos = self._servicio_con_dos_aprobaciones()
        nueva = crear_nueva_version_configuracion(servicio, self.admin, clonar_desde=servicio.configuracion_ejecucion_activa)
        jefe = nueva.bloques.get(clave="aprobacion_jefe")
        editar_bloque_operativo(nueva, jefe, self.admin, nombre="Visto bueno del jefe")
        self._activar(servicio, nueva)

        ticket = self._radicar(servicio, campos)
        self._resolver(ticket, "Visto bueno del jefe", "APROBADA")
        self._resolver(ticket, "Aprobación finanzas", "APROBADA")
        resultados = ticket.instancia_workflow.contexto["resultados_bloques"]["aprobaciones"]
        self.assertIn("aprobacion_jefe", resultados)
        self.assertEqual(self._camino(ticket), "Camino jefe aprobó")


class VariablesWorkflowRegresionTests(TestCase):
    """4.B0 no altera la radicación de un Ticket sin Workflow."""

    def test_ticket_sin_workflow_se_radica_igual(self):
        usuario = Usuario.objects.create_user(username="var_sin_flujo", password=CLAVE_PRUEBA)
        servicio, _, _ = _crear_servicio_con_formulario(
            usuario, [{"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Asunto", "orden": 1}]
        )
        ticket = crear_borrador(usuario, servicio)
        radicar_ticket(ticket, usuario)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertIsNone(ticket.instancia_workflow_id)


class _EscenarioBloqueEntregableMixin(_EscenarioFlujoFasesMixin):
    """4.B1 — flujos por fases con bloques ENTREGABLE sobre tickets reales. Los
    entregables de un Servicio se definen ANTES de crear el borrador (el snapshot se
    congela al crearlo)."""

    def _preparar_entregables_flujo(self):
        self._preparar_flujo()
        self.responsable = Usuario.objects.create_user(username="ent_responsable", password=CLAVE_PRUEBA)
        self.equipo = Equipo.objects.create(nombre="Equipo de entregables 4B1")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.responsable)
        _otorgar_tickets_atender(self.responsable)

    def _servicio_entregables(self, *definiciones):
        """`definiciones`: `(nombre, tipo, obligatorio)`. Devuelve `(servicio, {nombre: definicion})`."""
        from apps.catalogo.models import DefinicionEntregable

        servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        servicio.workflow = self.workflow
        servicio.save(update_fields=["workflow", "actualizado_en"])
        definidas = {}
        for orden, (nombre, tipo, obligatorio) in enumerate(definiciones):
            definidas[nombre] = DefinicionEntregable.objects.create(
                servicio=servicio, nombre=nombre, tipo=tipo, obligatorio=obligatorio, orden=orden
            )
        return servicio, definidas

    def _flujo(self, servicio, *pasos):
        """Configura y activa: `("ENT", nombre, definicion)` / `("ACT", nombre)` en Recepción,
        en ese orden, más una actividad final «Cierre» en Revisión."""
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion

        config = crear_nueva_version_configuracion(servicio, self.admin)
        for paso in pasos:
            if paso[0] == "ENT":
                agregar_bloque_operativo(
                    config, self.admin, fase=self.recepcion, tipo=BloqueOperativo.Tipo.ENTREGABLE,
                    nombre=paso[1], definicion_entregable=paso[2],
                )
            else:
                self._agregar(config, self.recepcion, BloqueOperativo.Tipo.ACTIVIDAD, paso[1], tipo_actor="SOLICITANTE")
        self._actividad(config, "Cierre")
        self._activar(servicio, config)
        return config

    def _atender(self, ticket):
        ticket = asignar_ticket(ticket, self.responsable, equipo=self.equipo)
        ticket = tomar_ticket(ticket, self.responsable)
        ticket.refresh_from_db()
        return ticket

    def _instancia(self, ticket):
        ticket.refresh_from_db()
        instancia = ticket.instancia_workflow
        instancia.refresh_from_db()
        return instancia

    def _ejecucion(self, ticket, nombre):
        return self._instancia(ticket).ejecuciones_etapa.get(bloque_operativo__nombre=nombre)

    def _ultima(self, ticket):
        return self._instancia(ticket).ejecuciones_etapa.order_by("-orden").first()

    def _satisfacer(self, ticket, definicion, texto="Documento listo"):
        entregable = ticket.entregables.get(definicion=definicion)
        return registrar_resultado_entregable(entregable, self.responsable, texto)


class BloqueEntregableEjecucionTests(_MediaAisladaMixin, _EscenarioBloqueEntregableMixin, TestCase):
    """4.B1 — ejecución del bloque ENTREGABLE: espera externa, reanudación desde el dominio,
    resultado publicado y separación respecto de la entrega formal."""

    def setUp(self):
        self._preparar_entregables_flujo()

    def test_entregable_pendiente_deja_la_ejecucion_en_espera_sin_tarea(self):
        from apps.tareas.models import Tarea

        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._radicar(servicio, {})

        instancia = self._instancia(ticket)
        ultima = self._ultima(ticket)
        self.assertEqual(instancia.estado, "EN_ESPERA")  # estado técnico del motor
        self.assertEqual((ultima.bloque_operativo.nombre, ultima.estado, ultima.motivo_espera),
                         ("Entrega propuesta", "EN_ESPERA", "ENTREGABLE"))
        self.assertEqual(Tarea.objects.count(), 0)
        self.assertEqual(ticket.entregables.count(), 1)
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertNotIn("entregables", instancia.contexto["resultados_bloques"])

    def test_completar_el_entregable_reanuda_publica_y_no_entrega_formalmente(self):
        from apps.tareas.models import Tarea

        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._atender(self._radicar(servicio, {}))
        entregables_antes = ticket.entregables.count()

        self._satisfacer(ticket, defs["Propuesta"])

        instancia = self._instancia(ticket)
        self.assertEqual(self._ejecucion(ticket, "Entrega propuesta").estado, "COMPLETADA")
        ultima = self._ultima(ticket)
        self.assertEqual((ultima.bloque_operativo.nombre, ultima.estado, ultima.motivo_espera),
                         ("Cierre", "EN_ESPERA", "TAREA"))
        self.assertEqual(
            instancia.contexto["resultados_bloques"]["entregables"]["entrega_propuesta"], {"satisfecho": True}
        )
        # Solo la Tarea de «Cierre»: el bloque ENTREGABLE no creó ninguna.
        self.assertEqual(Tarea.objects.count(), 1)
        self.assertEqual(ticket.entregables.count(), entregables_antes)
        # Satisfecho ≠ entrega formal ni resolución/cierre.
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertFalse(EntregaTicket.objects.filter(ticket=ticket).exists())
        self.assertFalse(ResolucionTicket.objects.filter(ticket=ticket).exists())

    def test_entregable_ya_satisfecho_al_entrar_continua_de_inmediato(self):
        from apps.tareas.models import Tarea
        from apps.workflows.integracion import completar_tarea_workflow
        from apps.workflows.models import TareaWorkflow

        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ACT", "Preparar"), ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._atender(self._radicar(servicio, {}))
        self._satisfacer(ticket, defs["Propuesta"])  # antes de llegar al bloque
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Preparar")

        tarea = TareaWorkflow.objects.get(instancia_etapa=self._ejecucion(ticket, "Preparar")).tarea
        completar_tarea_workflow(tarea, self.solicitante)

        self.assertEqual(self._ejecucion(ticket, "Entrega propuesta").estado, "COMPLETADA")
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")
        self.assertEqual(Tarea.objects.count(), 2)  # Preparar + Cierre; ninguna del entregable
        publicado = self._instancia(ticket).contexto["resultados_bloques"]["entregables"]
        self.assertEqual(publicado["entrega_propuesta"], {"satisfecho": True})

    def test_el_mismo_entregable_en_dos_bloques_no_se_copia_y_el_segundo_continua_solo(self):
        servicio, defs = self._servicio_entregables(("Documento X", "TEXTO", True))
        self._flujo(
            servicio, ("ENT", "Primera vez", defs["Documento X"]), ("ENT", "Segunda vez", defs["Documento X"])
        )
        ticket = self._atender(self._radicar(servicio, {}))
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Primera vez")

        self._satisfacer(ticket, defs["Documento X"])

        self.assertEqual(self._ejecucion(ticket, "Primera vez").estado, "COMPLETADA")
        self.assertEqual(self._ejecucion(ticket, "Segunda vez").estado, "COMPLETADA")
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")
        self.assertEqual(ticket.entregables.count(), 1)
        publicado = self._instancia(ticket).contexto["resultados_bloques"]["entregables"]
        self.assertEqual(set(publicado), {"primera_vez", "segunda_vez"})

    def test_dos_entregables_distintos_publican_resultados_separados(self):
        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True), ("Informe", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]), ("ENT", "Entrega informe", defs["Informe"]))
        ticket = self._atender(self._radicar(servicio, {}))

        self._satisfacer(ticket, defs["Propuesta"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Entrega informe")
        publicado = self._instancia(ticket).contexto["resultados_bloques"]["entregables"]
        self.assertEqual(set(publicado), {"entrega_propuesta"})

        self._satisfacer(ticket, defs["Informe"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")
        publicado = self._instancia(ticket).contexto["resultados_bloques"]["entregables"]
        self.assertEqual(set(publicado), {"entrega_propuesta", "entrega_informe"})

    def test_escribir_otro_entregable_no_reanuda_el_bloque(self):
        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True), ("Otro", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._atender(self._radicar(servicio, {}))

        self._satisfacer(ticket, defs["Otro"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Entrega propuesta")
        self.assertEqual(self._ultima(ticket).estado, "EN_ESPERA")

        self._satisfacer(ticket, defs["Propuesta"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")

    def test_una_definicion_opcional_referenciada_por_un_bloque_sigue_exigiendo_satisfaccion(self):
        servicio, defs = self._servicio_entregables(("Opcional", "TEXTO", False))
        self._flujo(servicio, ("ENT", "Entrega opcional", defs["Opcional"]))
        ticket = self._radicar(servicio, {})
        self.assertEqual(self._ultima(ticket).motivo_espera, "ENTREGABLE")
        ticket = self._atender(ticket)
        self._satisfacer(ticket, defs["Opcional"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")

    def test_el_bloque_no_decide_quien_sube_el_entregable(self):
        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._atender(self._radicar(servicio, {}))
        entregable = ticket.entregables.get(definicion=defs["Propuesta"])
        with self.assertRaises(PermissionDenied):
            registrar_resultado_entregable(entregable, self.solicitante, "No soy el responsable")
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Entrega propuesta")
        self.assertEqual(self._ultima(ticket).estado, "EN_ESPERA")

    def test_todos_los_tipos_de_entregable_reanudan_el_flujo(self):
        from apps.tickets.entregables import adjuntar_archivo_entregable, confirmar_entregable

        casos = {"TEXTO": "Texto", "ENLACE": "Enlace", "CONFIRMACION": "Confirmación", "ARCHIVO": "Archivo"}
        for tipo, nombre in casos.items():
            servicio, defs = self._servicio_entregables((nombre, tipo, True))
            self._flujo(servicio, ("ENT", f"Entrega {nombre}", defs[nombre]))
            ticket = self._atender(self._radicar(servicio, {}))
            entregable = ticket.entregables.get(definicion=defs[nombre])
            self.assertEqual(self._ultima(ticket).estado, "EN_ESPERA", tipo)
            if tipo == "TEXTO":
                registrar_resultado_entregable(entregable, self.responsable, "Listo")
            elif tipo == "ENLACE":
                registrar_resultado_entregable(entregable, self.responsable, "https://ejemplo.com/doc")
            elif tipo == "CONFIRMACION":
                confirmar_entregable(entregable, self.responsable)
            else:
                adjuntar_archivo_entregable(
                    entregable, self.responsable, SimpleUploadedFile("propuesta.txt", b"contenido")
                )
            self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre", tipo)

    def test_el_resultado_publicado_alimenta_una_decision_posterior(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion

        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        config = crear_nueva_version_configuracion(servicio, self.admin)
        agregar_bloque_operativo(
            config, self.admin, fase=self.recepcion, tipo=BloqueOperativo.Tipo.ENTREGABLE,
            nombre="Propuesta comercial", definicion_entregable=defs["Propuesta"],
        )
        alto = self._actividad(config, "Camino alto")
        normal = self._actividad(config, "Camino normal")
        self._decision(config, [("entregables.propuesta_comercial.satisfecho", "IGUAL_A", "true")], alto=alto, normal=normal)
        self._activar(servicio, config)

        ticket = self._atender(self._radicar(servicio, {}))
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Propuesta comercial")
        self._satisfacer(ticket, defs["Propuesta"])
        self.assertEqual(self._camino(ticket), "Camino alto")

    # --- idempotencia -----------------------------------------------------------

    def test_un_segundo_evento_de_reanudacion_no_avanza_otra_vez(self):
        from apps.tareas.models import Tarea
        from apps.workflows.integracion import continuar_por_entregable

        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._atender(self._radicar(servicio, {}))
        entregable = self._satisfacer(ticket, defs["Propuesta"])
        ejecuciones = self._instancia(ticket).ejecuciones_etapa.count()

        self.assertFalse(continuar_por_entregable(entregable))
        # Escribir otra vez el mismo valor vuelve a avisar al Workflow: tampoco avanza.
        registrar_resultado_entregable(entregable, self.responsable, "Documento listo")

        self.assertEqual(self._instancia(ticket).ejecuciones_etapa.count(), ejecuciones)
        self.assertEqual(Tarea.objects.count(), 1)

    def test_un_entregable_sin_workflow_no_hace_nada(self):
        from apps.catalogo.models import DefinicionEntregable
        from apps.workflows.integracion import continuar_por_entregable

        servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        definicion = DefinicionEntregable.objects.create(servicio=servicio, nombre="Suelto", tipo="TEXTO")
        ticket = crear_borrador(self.solicitante, servicio)
        radicar_ticket(ticket, self.solicitante)
        self.assertFalse(continuar_por_entregable(ticket.entregables.get(definicion=definicion)))

    # --- consistencia ---------------------------------------------------------------

    def test_sin_el_entregable_congelado_no_se_radica_ni_se_inventa_otro(self):
        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = crear_borrador(self.solicitante, servicio)
        EntregableTicket.objects.filter(ticket=ticket).delete()  # inconsistencia simulada

        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.BORRADOR)
        self.assertEqual(EntregableTicket.objects.filter(ticket=ticket).count(), 0)

    # --- versionamiento y Workflow compartido --------------------------------------------

    def test_una_ejecucion_en_curso_conserva_su_configuracion_aunque_se_publique_otra(self):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion, editar_bloque_operativo

        servicio, defs = self._servicio_entregables(("Propuesta A", "TEXTO", True), ("Propuesta B", "TEXTO", True))
        config1 = self._flujo(servicio, ("ENT", "Entrega", defs["Propuesta A"]))
        ticket = self._atender(self._radicar(servicio, {}))
        self.assertEqual(self._instancia(ticket).configuracion_ejecucion_version_id, config1.pk)

        config2 = crear_nueva_version_configuracion(servicio, self.admin, clonar_desde=config1)
        bloque2 = config2.bloques.get(tipo="ENTREGABLE")
        self.assertEqual(bloque2.clave, "entrega")
        self.assertEqual(bloque2.definicion_entregable_id, defs["Propuesta A"].pk)
        editar_bloque_operativo(config2, bloque2, self.admin, definicion_entregable=defs["Propuesta B"])
        self._activar(servicio, config2)

        # El ticket histórico sigue esperando lo de SU configuración (A), no lo de la vigente (B).
        self._satisfacer(ticket, defs["Propuesta B"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Entrega")
        self._satisfacer(ticket, defs["Propuesta A"])
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")

    def test_workflow_compartido_cada_servicio_usa_su_propia_definicion(self):
        servicio_a, defs_a = self._servicio_entregables(("Propuesta A", "TEXTO", True))
        servicio_b, defs_b = self._servicio_entregables(("Informe B", "TEXTO", True))
        self.assertEqual(servicio_a.workflow_id, servicio_b.workflow_id)
        self._flujo(servicio_a, ("ENT", "Entrega", defs_a["Propuesta A"]))
        self._flujo(servicio_b, ("ENT", "Entrega", defs_b["Informe B"]))
        ticket_a = self._atender(self._radicar(servicio_a, {}))
        ticket_b = self._atender(self._radicar(servicio_b, {}))

        self._satisfacer(ticket_b, defs_b["Informe B"])
        self.assertEqual(self._ultima(ticket_b).bloque_operativo.nombre, "Cierre")
        self.assertEqual(self._ultima(ticket_a).bloque_operativo.nombre, "Entrega")  # A sigue esperando

        self._satisfacer(ticket_a, defs_a["Propuesta A"])
        self.assertEqual(self._ultima(ticket_a).bloque_operativo.nombre, "Cierre")

    # --- compatibilidad histórica de ESPERA ------------------------------------------------------

    def test_una_espera_historica_en_una_configuracion_sigue_ejecutandose(self):
        from datetime import timedelta
        from unittest.mock import patch

        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion
        from apps.workflows.motor import reanudar_instancia

        servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        servicio.workflow = self.workflow
        servicio.save(update_fields=["workflow", "actualizado_en"])
        config = crear_nueva_version_configuracion(servicio, self.admin)
        # Registro histórico creado fuera de la operación normal (que ya la rechaza).
        BloqueOperativo.objects.create(
            version=config, fase=self.recepcion, tipo="ESPERA", nombre="Espera histórica", orden=1,
            configuracion={"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "HORAS"},
        )
        self._actividad(config, "Cierre")
        self._activar(servicio, config)

        ticket = self._radicar(servicio, {})
        ultima = self._ultima(ticket)
        self.assertEqual((ultima.bloque_operativo.nombre, ultima.estado, ultima.motivo_espera),
                         ("Espera histórica", "EN_ESPERA", "TEMPORAL"))
        with patch("apps.workflows.motor.timezone.now", return_value=timezone.now() + timedelta(hours=2)):
            reanudar_instancia(self._instancia(ticket))
        self.assertEqual(self._ultima(ticket).bloque_operativo.nombre, "Cierre")


class ConcurrenciaBloqueEntregableTests(_EscenarioBloqueEntregableMixin, TransactionTestCase):
    def setUp(self):
        self._preparar_entregables_flujo()

    def test_dos_reanudaciones_simultaneas_continuan_el_flujo_una_sola_vez(self):
        from apps.tareas.models import Tarea
        from apps.workflows.integracion import continuar_por_entregable

        servicio, defs = self._servicio_entregables(("Propuesta", "TEXTO", True))
        self._flujo(servicio, ("ENT", "Entrega propuesta", defs["Propuesta"]))
        ticket = self._atender(self._radicar(servicio, {}))
        entregable = ticket.entregables.get(definicion=defs["Propuesta"])
        # Satisfacer sin pasar por el dominio: así ninguna escritura reanuda antes de los hilos.
        EntregableTicket.objects.filter(pk=entregable.pk).update(texto="Listo")
        barrera = threading.Barrier(2)
        resultados = []

        def reanudar():
            try:
                barrera.wait(timeout=10)
                resultados.append(
                    continuar_por_entregable(EntregableTicket.objects.select_related("ticket").get(pk=entregable.pk))
                )
            except Exception as exc:
                resultados.append(exc)
            finally:
                connection.close()

        hilos = [threading.Thread(target=reanudar, daemon=True) for _ in range(2)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(timeout=30)
        self.assertFalse(any(hilo.is_alive() for hilo in hilos))
        self.assertEqual(sorted(resultados, key=str), [False, True])
        instancia = self._instancia(ticket)
        self.assertEqual(instancia.ejecuciones_etapa.filter(bloque_operativo__nombre="Cierre").count(), 1)
        self.assertEqual(instancia.ejecuciones_etapa.filter(bloque_operativo__nombre="Entrega propuesta").count(), 1)
        self.assertEqual(Tarea.objects.count(), 1)


# ---------------------------------------------------------------------------
# 4.E2 — recorridos operables: cierre del Workflow, aprobación que revisa un entregable,
# ciclo de ajustes con nueva versión, finalizar desde aprobación/decisión y protección de
# la resolución prematura.
# ---------------------------------------------------------------------------


class _EscenarioE2Mixin(_EscenarioBloqueEntregableMixin):
    """Plantilla de cinco fases (Recepción → Producción → Revisión → Entrega, y el ciclo
    Revisión ⇄ Ajustes) sobre tickets reales. La actividad la hace `self.responsable`; la
    aprobación, `self.aprobador`."""

    ENTREGABLE = "Presentación comercial"

    def _preparar_e2(self):
        from apps.workflows import fases as fases_ops
        from apps.workflows.versionamiento import activar_version as activar_workflow

        self._preparar_entregables_flujo()
        self.workflow_e2 = fases_ops.crear_plantilla_fases(self.admin, nombre="Plantilla E2")
        version = self.workflow_e2.versiones.get(numero=1)
        self.f_recepcion = fases_ops.agregar_fase(version, self.admin, nombre="Recepción")
        self.f_produccion = fases_ops.agregar_fase(version, self.admin, nombre="Producción")
        self.f_revision = fases_ops.agregar_fase(version, self.admin, nombre="Revisión")
        self.f_ajustes = fases_ops.agregar_fase(version, self.admin, nombre="Ajustes")
        self.f_entrega = fases_ops.agregar_fase(version, self.admin, nombre="Entrega")
        fases_ops.conectar_fases(self.f_recepcion, self.f_produccion, self.admin)
        fases_ops.conectar_fases(self.f_produccion, self.f_revision, self.admin)
        fases_ops.conectar_fases(self.f_revision, self.f_entrega, self.admin, prioridad=0)
        fases_ops.conectar_fases(self.f_revision, self.f_ajustes, self.admin, prioridad=1)
        fases_ops.conectar_fases(self.f_ajustes, self.f_revision, self.admin)
        activar_workflow(self.workflow_e2, version, self.admin)

    def _servicio_e2(self, tipo="TEXTO", politica=None):
        """Servicio con un formulario (`requiere_revision`) y el entregable «Presentación comercial».
        Devuelve `(servicio, campos, definicion)`."""
        from apps.catalogo.models import DefinicionEntregable

        servicio, _, campos = _crear_servicio_con_formulario(
            self.solicitante, [{"tipo": Campo.TipoCampo.BOOLEANO, "etiqueta": "Requiere revision", "orden": 1}]
        )
        servicio.workflow = self.workflow_e2
        servicio.save(update_fields=["workflow", "actualizado_en"])
        if politica:
            Servicio.objects.filter(pk=servicio.pk).update(politica_entrega=politica, dias_observacion=None)
        definicion = DefinicionEntregable.objects.create(
            servicio=servicio, nombre=self.ENTREGABLE, tipo=tipo, obligatorio=True, orden=0
        )
        return servicio, campos, definicion

    def _act(self, config, fase, nombre):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        return agregar_bloque_operativo(
            config, self.admin, fase=fase, tipo=BloqueOperativo.Tipo.ACTIVIDAD, nombre=nombre,
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.responsable.pk},
        )

    def _apr(self, config, fase, nombre, revisa=None):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        return agregar_bloque_operativo(
            config, self.admin, fase=fase, tipo=BloqueOperativo.Tipo.APROBACION, nombre=nombre,
            configuracion={
                "modo": "SECUENCIAL", "politica": "",
                "participantes": [{"tipo": "USUARIO", "usuario_id": self.aprobador.pk, "equipo_id": None}],
            },
            entregable_revisado=revisa,
        )

    def _ruta(self, origen, destino, **reglas):
        from apps.catalogo.configuracion_ejecucion import conectar_bloques_operativos

        conectar_bloques_operativos(origen, destino, self.admin, finaliza=destino is None, **reglas)

    def _configurar_e2(self, servicio, definicion, *, rechazada_finaliza=True):
        """Recepción: Analizar · Producción: Preparar + Entregable · Revisión: Aprobación (revisa
        el entregable) · Ajustes: Corregir + Entregable · Entrega: Entregar. APROBADA → Entrega,
        DEVUELTA → Ajustes, RECHAZADA → finalizar."""
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion

        config = crear_nueva_version_configuracion(servicio, self.admin)
        analizar = self._act(config, self.f_recepcion, "Analizar solicitud")
        self._act(config, self.f_produccion, "Preparar presentación")
        entrega1 = agregar_bloque_operativo(
            config, self.admin, fase=self.f_produccion, tipo=BloqueOperativo.Tipo.ENTREGABLE,
            nombre=self.ENTREGABLE, definicion_entregable=definicion,
        )
        aprobacion = self._apr(config, self.f_revision, "Revisar presentación", revisa=entrega1)
        corregir = self._act(config, self.f_ajustes, "Corregir presentación")
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_ajustes, tipo=BloqueOperativo.Tipo.ENTREGABLE,
            nombre="Presentación corregida", definicion_entregable=definicion,
        )
        entregar = self._act(config, self.f_entrega, "Entregar resultado")
        self._ruta(aprobacion, entregar, resultado_aprobacion="APROBADA")
        self._ruta(aprobacion, corregir, resultado_aprobacion="DEVUELTA")
        self._ruta(aprobacion, None if rechazada_finaliza else corregir, resultado_aprobacion="RECHAZADA")
        self._activar(servicio, config)
        return config, analizar

    def _configurar_minimo(self, servicio):
        """Recepción: Analizar · Revisión: aprobación GENERAL (sin entregable) · Ajustes: Corregir.
        APROBADA y RECHAZADA finalizan; DEVUELTA va a Corregir. Entrega queda vacía."""
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        config = crear_nueva_version_configuracion(servicio, self.admin)
        self._act(config, self.f_recepcion, "Analizar solicitud")
        aprobacion = self._apr(config, self.f_revision, "Aprobar contratación")
        corregir = self._act(config, self.f_ajustes, "Corregir presentación")
        self._ruta(aprobacion, None, resultado_aprobacion="APROBADA")
        self._ruta(aprobacion, corregir, resultado_aprobacion="DEVUELTA")
        self._ruta(aprobacion, None, resultado_aprobacion="RECHAZADA")
        self._activar(servicio, config)
        return config

    def _configurar_decision(self, servicio):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        config = crear_nueva_version_configuracion(servicio, self.admin)
        decision = self._agregar(config, self.f_recepcion, BloqueOperativo.Tipo.DECISION, "¿Requiere trabajo?")
        trabajar = self._act(config, self.f_revision, "Trabajar")
        self._ruta(decision, trabajar, variable="formulario.requiere_revision", operador="IGUAL_A", valor="Sí", prioridad=0)
        self._ruta(decision, None, es_fallback=True)
        self._activar(servicio, config)

    def _radicar_e2(self, servicio, campos, requiere=True):
        return self._radicar(servicio, campos, {"Requiere revision": requiere})

    # --- acciones de las personas ------------------------------------------------------

    def _nombre_ultima(self, ticket):
        return self._ultima(ticket).bloque_operativo.nombre

    def _completar(self, ticket, nombre):
        from apps.workflows.integracion import completar_tarea_workflow
        from apps.workflows.models import TareaWorkflow

        ejecucion = (
            self._instancia(ticket).ejecuciones_etapa.filter(bloque_operativo__nombre=nombre).order_by("-orden").first()
        )
        completar_tarea_workflow(TareaWorkflow.objects.get(instancia_etapa=ejecucion).tarea, self.responsable)

    def _aprobacion_vigente(self, ticket, nombre="Revisar presentación"):
        from apps.workflows.models import EsquemaAprobacionWorkflow

        vinculo = (
            EsquemaAprobacionWorkflow.objects.filter(
                instancia_etapa__instancia_workflow=self._instancia(ticket),
                instancia_etapa__bloque_operativo__nombre=nombre,
            )
            .order_by("-instancia_etapa__orden")
            .first()
        )
        return vinculo.esquema.participaciones.get()

    def _revisar(self, ticket, decision, nombre="Revisar presentación"):
        from apps.workflows.integracion import resolver_aprobacion_workflow

        resolver_aprobacion_workflow(
            self._aprobacion_vigente(ticket, nombre), self.aprobador, decision=decision,
            observacion="" if decision == "APROBADA" else "Ajustar la presentación.",
        )

    def _hasta_la_primera_revision(self, ticket, definicion, valor="Versión 1"):
        """Analizar → Preparar → Entregable (V1) → la aprobación queda pendiente."""
        self._completar(ticket, "Analizar solicitud")
        self._completar(ticket, "Preparar presentación")
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).motivo_espera), (self.ENTREGABLE, "ENTREGABLE"))
        self._satisfacer(ticket, definicion, valor)
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).motivo_espera), ("Revisar presentación", "APROBACION"))

    def _pasadas(self, ticket, nombre):
        return self._instancia(ticket).ejecuciones_etapa.filter(bloque_operativo__nombre=nombre).count()


class RecorridoE2Tests(_MediaAisladaMixin, _EscenarioE2Mixin, TestCase):
    def setUp(self):
        self._preparar_e2()

    # --- recorrido 1: aprobado a la primera -----------------------------------------------

    def test_aprobado_en_la_primera_revision_completa_el_workflow_y_el_ticket_sigue_su_ciclo(self):
        servicio, campos, definicion = self._servicio_e2(politica=DIRECTO)
        self._configurar_e2(servicio, definicion)
        ticket = self._radicar_e2(servicio, campos)
        self.assertEqual((ticket.estado, self._nombre_ultima(ticket)), (Ticket.Estado.RADICADO, "Analizar solicitud"))

        ticket = self._atender(ticket)
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "APROBADA")
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).motivo_espera), ("Entregar resultado", "TAREA"))
        self.assertEqual(self._instancia(ticket).estado, "EN_ESPERA")

        self._completar(ticket, "Entregar resultado")  # el último bloque: nadie «finaliza» el flujo
        instancia = self._instancia(ticket)
        self.assertEqual(instancia.estado, "COMPLETADA")
        self.assertIsNotNone(instancia.finalizada_en)
        self.assertEqual(self._ultima(ticket).estado, "COMPLETADA")
        # Workflow y Ticket son ciclos separados: completar el flujo no resuelve ni entrega.
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertFalse(EntregaTicket.objects.filter(ticket=ticket).exists())
        self.assertFalse(ResolucionTicket.objects.filter(ticket=ticket).exists())

        entregar_ticket(ticket, self.responsable)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)  # política de cierre directo

    def test_sin_politica_de_entrega_el_ticket_se_resuelve_y_se_cierra_con_el_workflow_completado(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "APROBADA")
        self._completar(ticket, "Entregar resultado")
        resolver_ticket(ticket, self.responsable, "Presentación entregada.")
        cerrar_ticket(Ticket.objects.get(pk=ticket.pk), self.solicitante)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.CERRADO)

    def test_cada_bloque_y_cada_fase_avanzan_solos_en_su_orden(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "APROBADA")
        self._completar(ticket, "Entregar resultado")
        recorrido = [
            (e.fase_workflow.nombre, e.bloque_operativo.nombre)
            for e in self._instancia(ticket).ejecuciones_etapa.select_related("fase_workflow", "bloque_operativo")
        ]
        self.assertEqual(recorrido, [
            ("Recepción", "Analizar solicitud"), ("Producción", "Preparar presentación"),
            ("Producción", self.ENTREGABLE), ("Revisión", "Revisar presentación"), ("Entrega", "Entregar resultado"),
        ])

    # --- recorrido 2: requiere ajustes -----------------------------------------------------

    def test_requiere_ajustes_exige_una_nueva_version_del_entregable(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion, "Versión 1")

        self._revisar(ticket, "DEVUELTA")
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).motivo_espera), ("Corregir presentación", "TAREA"))
        self._completar(ticket, "Corregir presentación")
        # V1 sigue «satisfecha», pero ya fue observada: el bloque espera la versión corregida.
        self.assertEqual(
            (self._nombre_ultima(ticket), self._ultima(ticket).estado, self._ultima(ticket).motivo_espera),
            ("Presentación corregida", "EN_ESPERA", "ENTREGABLE"),
        )
        self.assertTrue(ticket.entregables.get(definicion=definicion).satisfecho)

        self._satisfacer(ticket, definicion, "Versión 2")
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).motivo_espera), ("Revisar presentación", "APROBACION"))
        self.assertEqual(self._pasadas(ticket, "Revisar presentación"), 2)  # una instancia NUEVA, no la anterior
        self._revisar(ticket, "APROBADA")
        self._completar(ticket, "Entregar resultado")
        self.assertEqual(self._instancia(ticket).estado, "COMPLETADA")

    def test_volver_a_guardar_la_misma_version_no_reanuda_el_flujo(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion, "Versión 1")
        self._revisar(ticket, "DEVUELTA")
        self._completar(ticket, "Corregir presentación")

        self._satisfacer(ticket, definicion, "Versión 1")  # mismo valor: no es una versión nueva
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).estado), ("Presentación corregida", "EN_ESPERA"))
        self.assertEqual(self._pasadas(ticket, "Revisar presentación"), 1)

    def test_una_aprobacion_aprobada_no_exige_otra_version(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "APROBADA")
        from apps.tickets.entregables import entregable_vigente_para_flujo

        self.assertTrue(entregable_vigente_para_flujo(ticket.entregables.get(definicion=definicion)))

    def test_con_archivos_la_version_anterior_conserva_su_trazabilidad(self):
        servicio, campos, definicion = self._servicio_e2("ARCHIVO")
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        entregable = ticket.entregables.get(definicion=definicion)
        self._completar(ticket, "Analizar solicitud")
        self._completar(ticket, "Preparar presentación")
        v1 = adjuntar_archivo_entregable(entregable, self.responsable, SimpleUploadedFile("v1.txt", b"version 1"))
        self.assertEqual(self._nombre_ultima(ticket), "Revisar presentación")

        self._revisar(ticket, "DEVUELTA")
        self._completar(ticket, "Corregir presentación")
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).estado), ("Presentación corregida", "EN_ESPERA"))

        # Retirar V1 (sin subir nada) no la «corrige»: el bloque sigue esperando.
        retirar_archivo_entregable(v1, self.responsable)
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).estado), ("Presentación corregida", "EN_ESPERA"))
        v2 = adjuntar_archivo_entregable(entregable, self.responsable, SimpleUploadedFile("v2.txt", b"version 2"))
        self.assertEqual(self._nombre_ultima(ticket), "Revisar presentación")
        self.assertEqual(self._pasadas(ticket, "Revisar presentación"), 2)

        # V1 sigue ahí (retirada, no borrada) y lo que cada revisión vio quedó en su historial.
        v1.refresh_from_db()
        self.assertIsNotNone(v1.retirado_en)
        self.assertTrue(Adjunto.objects.filter(pk=v1.pk).exists())
        self.assertIsNone(Adjunto.objects.get(pk=v2.pk).retirado_en)
        primera, segunda = self._instancia(ticket).ejecuciones_etapa.filter(
            bloque_operativo__nombre="Revisar presentación"
        ).order_by("orden")
        self.assertEqual(primera.resultado["revision"]["version"]["adjuntos"], [v1.pk])
        self.assertEqual(primera.transicion_bloque_tomada.resultado_aprobacion, "DEVUELTA")
        self.assertEqual(primera.resultado["revision"]["entregable_id"], entregable.pk)

    def test_una_segunda_version_sin_retirar_la_primera_tambien_cuenta(self):
        servicio, campos, definicion = self._servicio_e2("ARCHIVO")
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        entregable = ticket.entregables.get(definicion=definicion)
        self._completar(ticket, "Analizar solicitud")
        self._completar(ticket, "Preparar presentación")
        adjuntar_archivo_entregable(entregable, self.responsable, SimpleUploadedFile("v1.txt", b"version 1"))
        self._revisar(ticket, "DEVUELTA")
        self._completar(ticket, "Corregir presentación")
        self.assertEqual(self._ultima(ticket).estado, "EN_ESPERA")
        adjuntar_archivo_entregable(entregable, self.responsable, SimpleUploadedFile("v2.txt", b"version 2"))
        self.assertEqual(self._nombre_ultima(ticket), "Revisar presentación")

    # --- recorrido 3: varios ajustes --------------------------------------------------------

    def test_varias_devoluciones_repiten_el_ciclo_sin_reutilizar_instancias_ni_perder_versiones(self):
        from apps.tickets.entregables import _huella

        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion, "Versión 1")

        for numero, siguiente in ((1, "Versión 2"), (2, "Versión 3")):
            self._revisar(ticket, "DEVUELTA")
            publicado = self._instancia(ticket).contexto["resultados_bloques"]["aprobaciones"]["revisar_presentacion"]
            self.assertEqual(publicado, {"resultado": "DEVUELTA"})
            self.assertEqual(self._nombre_ultima(ticket), "Corregir presentación")
            self._completar(ticket, "Corregir presentación")
            self.assertEqual(
                (self._nombre_ultima(ticket), self._ultima(ticket).estado), ("Presentación corregida", "EN_ESPERA"), numero
            )
            self._satisfacer(ticket, definicion, siguiente)
            self.assertEqual(self._nombre_ultima(ticket), "Revisar presentación")
            self.assertEqual(self._pasadas(ticket, "Revisar presentación"), numero + 1)

        self._revisar(ticket, "APROBADA")
        publicado = self._instancia(ticket).contexto["resultados_bloques"]["aprobaciones"]["revisar_presentacion"]
        self.assertEqual(publicado, {"resultado": "APROBADA"})
        self._completar(ticket, "Entregar resultado")

        self.assertEqual(self._instancia(ticket).estado, "COMPLETADA")
        self.assertEqual(self._pasadas(ticket, "Corregir presentación"), 2)
        self.assertEqual(self._pasadas(ticket, "Presentación corregida"), 2)
        revisiones = self._instancia(ticket).ejecuciones_etapa.filter(
            bloque_operativo__nombre="Revisar presentación"
        ).order_by("orden")
        self.assertEqual(len({e.pk for e in revisiones}), 3)
        self.assertTrue(all(e.estado == "COMPLETADA" for e in revisiones))
        # Cada pasada revisó una versión distinta (V1, V2, V3), y todas quedan en su historial.
        self.assertEqual(
            [e.resultado["revision"]["version"]["huella"] for e in revisiones],
            [_huella("Versión 1"), _huella("Versión 2"), _huella("Versión 3")],
        )

    def test_rechazada_finaliza_el_flujo_por_su_ruta(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "RECHAZADA")
        instancia = self._instancia(ticket)
        self.assertEqual(instancia.estado, "COMPLETADA")
        self.assertIsNotNone(instancia.finalizada_en)
        self.assertEqual(self._nombre_ultima(ticket), "Revisar presentación")

    # --- recorrido 4: finalizar desde aprobación / decisión ----------------------------------------

    def test_aprobada_finaliza_el_workflow_sin_una_actividad_artificial(self):
        from apps.tareas.models import Tarea

        servicio, campos, _ = self._servicio_e2()
        self._configurar_minimo(servicio)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._completar(ticket, "Analizar solicitud")
        self.assertEqual((self._nombre_ultima(ticket), self._ultima(ticket).motivo_espera), ("Aprobar contratación", "APROBACION"))
        ejecuciones = self._instancia(ticket).ejecuciones_etapa.count()

        self._revisar(ticket, "APROBADA", nombre="Aprobar contratación")

        instancia = self._instancia(ticket)
        self.assertEqual(instancia.estado, "COMPLETADA")
        self.assertIsNotNone(instancia.finalizada_en)
        self.assertEqual(instancia.ejecuciones_etapa.count(), ejecuciones)  # nada se creó después
        self.assertEqual(Tarea.objects.count(), 1)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)  # finalizar no resuelve ni cierra
        self.assertFalse(EntregaTicket.objects.filter(ticket=ticket).exists())
        self.assertFalse(ResolucionTicket.objects.filter(ticket=ticket).exists())

    def test_devuelta_sigue_su_ruta_aunque_otras_rutas_finalicen(self):
        servicio, campos, _ = self._servicio_e2()
        self._configurar_minimo(servicio)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._completar(ticket, "Analizar solicitud")
        self._revisar(ticket, "DEVUELTA", nombre="Aprobar contratación")
        self.assertEqual(self._instancia(ticket).estado, "EN_ESPERA")
        self.assertEqual(self._nombre_ultima(ticket), "Corregir presentación")

    def test_una_decision_puede_finalizar_el_workflow_al_radicar(self):
        from apps.tareas.models import Tarea

        servicio, campos, _ = self._servicio_e2()
        self._configurar_decision(servicio)

        sin_trabajo = self._radicar_e2(servicio, campos, requiere=False)
        instancia = self._instancia(sin_trabajo)
        self.assertEqual(instancia.estado, "COMPLETADA")
        self.assertIsNotNone(instancia.finalizada_en)
        self.assertEqual(sin_trabajo.estado, Ticket.Estado.RADICADO)
        self.assertEqual(Tarea.objects.count(), 0)  # una DECISION nunca crea Tarea

        con_trabajo = self._radicar_e2(servicio, campos, requiere=True)
        self.assertEqual(self._instancia(con_trabajo).estado, "EN_ESPERA")
        self.assertEqual(self._nombre_ultima(con_trabajo), "Trabajar")
        self.assertEqual(Tarea.objects.count(), 1)

    def test_una_fase_final_vacia_no_deja_el_flujo_detenido(self):
        servicio, campos, _ = self._servicio_e2()
        self._configurar_decision(servicio)
        ticket = self._atender(self._radicar_e2(servicio, campos, requiere=True))
        self._completar(ticket, "Trabajar")  # tras «Trabajar» solo quedan fases sin bloques
        self.assertEqual(self._instancia(ticket).estado, "COMPLETADA")

    # --- recorrido 5: resolución prematura -------------------------------------------------------------

    def test_un_ticket_con_workflow_en_espera_no_puede_resolverse(self):
        from apps.workflows.models import InstanciaWorkflow

        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self.assertEqual(self._instancia(ticket).estado, "EN_ESPERA")

        with self.assertRaises(ValidationError) as ctx:
            resolver_ticket(ticket, self.responsable, "Intento prematuro.")
        self.assertIn("El trabajo interno de este ticket todavía no ha terminado.", ctx.exception.messages)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertFalse(ResolucionTicket.objects.filter(ticket=ticket).exists())

        InstanciaWorkflow.objects.filter(pk=ticket.instancia_workflow_id).update(estado="EN_EJECUCION")
        with self.assertRaises(ValidationError):
            resolver_ticket(Ticket.objects.get(pk=ticket.pk), self.responsable, "Intento prematuro.")

    def test_con_el_workflow_completado_el_ticket_puede_resolverse(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        with self.assertRaises(ValidationError):
            resolver_ticket(Ticket.objects.get(pk=ticket.pk), self.responsable, "Todavía no.")
        self._revisar(ticket, "APROBADA")
        self._completar(ticket, "Entregar resultado")
        resolucion = resolver_ticket(Ticket.objects.get(pk=ticket.pk), self.responsable, "Presentación entregada.")
        self.assertEqual(resolucion.ticket_id, ticket.pk)
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RESUELTO)

    def test_la_entrega_formal_tambien_espera_al_trabajo_interno(self):
        servicio, campos, definicion = self._servicio_e2(politica=DIRECTO)
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        with self.assertRaises(ValidationError) as ctx:
            entregar_ticket(Ticket.objects.get(pk=ticket.pk), self.responsable)
        self.assertIn("El trabajo interno de este ticket todavía no ha terminado.", ctx.exception.messages)
        self.assertFalse(EntregaTicket.objects.filter(ticket=ticket).exists())

    def test_un_ticket_sin_workflow_se_resuelve_como_siempre(self):
        servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        ticket = crear_borrador(self.solicitante, servicio)
        radicar_ticket(ticket, self.solicitante)
        ticket = self._atender(Ticket.objects.get(pk=ticket.pk))
        resolver_ticket(ticket, self.responsable, "Resuelto sin workflow.")
        self.assertEqual(Ticket.objects.get(pk=ticket.pk).estado, Ticket.Estado.RESUELTO)


class OperabilidadE2Tests(_MediaAisladaMixin, _EscenarioE2Mixin, TestCase):
    """Lo que quien atiende y quien aprueba ven y pueden hacer desde la interfaz."""

    def setUp(self):
        self._preparar_e2()
        self.ajeno = Usuario.objects.create_user(username="e2_ajeno", password=CLAVE_PRUEBA)

    def _entrar(self, usuario):
        self.client.logout()
        self.assertTrue(self.client.login(username=usuario.get_username(), password=CLAVE_PRUEBA))

    def _detalle(self, ticket):
        return self.client.get(reverse("tickets:detalle", args=[ticket.pk]))

    # --- quien atiende ------------------------------------------------------------------------------

    def test_el_detalle_del_ticket_indica_el_trabajo_actual_y_donde_actuar(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._entrar(self.responsable)

        pagina = self._detalle(ticket)
        self.assertContains(pagina, "Trabajo actual")
        self.assertContains(pagina, "Analizar solicitud")
        self.assertContains(pagina, "Hay una actividad por completar.")
        self.assertContains(pagina, "Abrir la actividad")

        self._completar(ticket, "Analizar solicitud")
        self._completar(ticket, "Preparar presentación")
        pagina = self._detalle(ticket)
        self.assertContains(pagina, f"Falta entregar «{self.ENTREGABLE}».")
        self.assertNotContains(pagina, "Abrir la actividad")

        self._satisfacer(ticket, definicion)
        pagina = self._detalle(ticket)
        self.assertContains(pagina, "Revisar presentación")
        self.assertContains(pagina, "Está pendiente de aprobación.")

    def test_el_detalle_avisa_que_aun_no_se_puede_resolver_y_luego_lo_permite(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._entrar(self.responsable)
        self.assertContains(self._detalle(ticket), "Todavía no puedes resolver.")
        self.assertNotContains(self._detalle(ticket), 'id="id_descripcion_resolucion"')

        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "APROBADA")
        self._completar(ticket, "Entregar resultado")
        pagina = self._detalle(ticket)
        self.assertContains(pagina, "El trabajo interno de este ticket ya terminó.")
        self.assertNotContains(pagina, "Todavía no puedes resolver.")
        self.assertContains(pagina, 'id="id_descripcion_resolucion"')

    def test_un_ticket_sin_workflow_no_muestra_trabajo_actual(self):
        servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        ticket = crear_borrador(self.solicitante, servicio)
        radicar_ticket(ticket, self.solicitante)
        self._entrar(self.solicitante)
        self.assertNotContains(self._detalle(ticket), "Trabajo actual")

    # --- quien aprueba ---------------------------------------------------------------------------------

    def test_el_aprobador_abre_la_bandeja_la_vista_previa_y_el_detalle_de_una_aprobacion_por_fases(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion, "Texto de la primera versión")
        aprobacion = self._aprobacion_vigente(ticket)
        self._entrar(self.aprobador)

        lista = self.client.get(reverse("aprobaciones:lista"))
        self.assertEqual(lista.status_code, 200)
        self.assertContains(lista, "Revisar presentación")
        self.assertContains(lista, "Revisión")  # la fase

        previa = self.client.get(reverse("aprobaciones:vista_previa", args=[aprobacion.pk]))
        self.assertEqual(previa.status_code, 200)
        self.assertContains(previa, "Revisar presentación")
        self.assertContains(previa, self.ENTREGABLE)

        detalle = self.client.get(reverse("aprobaciones:detalle", args=[aprobacion.pk]))
        self.assertEqual(detalle.status_code, 200)
        self.assertContains(detalle, "Fase")
        self.assertContains(detalle, "Bloque")
        self.assertContains(detalle, "Entregable que se revisa")
        self.assertContains(detalle, self.ENTREGABLE)
        self.assertContains(detalle, "Entregado")
        self.assertContains(detalle, "Texto de la primera versión")
        self.assertNotContains(detalle, "Revisión</dt><dd>N.º")  # primera pasada: sin número de ronda

    def test_el_aprobador_decide_desde_la_interfaz_y_el_flujo_continua_solo(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        aprobacion = self._aprobacion_vigente(ticket)
        self._entrar(self.aprobador)

        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[aprobacion.pk]), {"decision": "APROBADA", "observacion": ""}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self._nombre_ultima(ticket), "Entregar resultado")

    def test_en_la_segunda_pasada_el_aprobador_ve_que_es_la_revision_numero_dos(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion, "Versión 1")
        self._revisar(ticket, "DEVUELTA")
        self._completar(ticket, "Corregir presentación")
        self._satisfacer(ticket, definicion, "Versión corregida")
        self._entrar(self.aprobador)
        detalle = self.client.get(reverse("aprobaciones:detalle", args=[self._aprobacion_vigente(ticket).pk]))
        self.assertContains(detalle, "N.º 2 de este entregable")
        self.assertContains(detalle, "Versión corregida")
        self.assertNotContains(detalle, "Versión 1")  # el contenido mostrado es la versión vigente

    def test_una_aprobacion_general_no_muestra_la_seccion_del_entregable(self):
        servicio, campos, _ = self._servicio_e2()
        self._configurar_minimo(servicio)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._completar(ticket, "Analizar solicitud")
        aprobacion = self._aprobacion_vigente(ticket, "Aprobar contratación")
        self._entrar(self.aprobador)
        detalle = self.client.get(reverse("aprobaciones:detalle", args=[aprobacion.pk]))
        self.assertEqual(detalle.status_code, 200)
        self.assertContains(detalle, "Bloque")
        self.assertContains(detalle, "Aprobar contratación")
        self.assertNotContains(detalle, "Entregable que se revisa")

    def test_el_aprobador_descarga_el_archivo_que_revisa_y_un_ajeno_no(self):
        servicio, campos, definicion = self._servicio_e2("ARCHIVO")
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._completar(ticket, "Analizar solicitud")
        self._completar(ticket, "Preparar presentación")
        archivo = adjuntar_archivo_entregable(
            ticket.entregables.get(definicion=definicion), self.responsable, SimpleUploadedFile("propuesta.txt", b"contenido")
        )
        url = reverse("tickets:descargar_adjunto", args=[archivo.pk])

        self._entrar(self.aprobador)
        detalle = self.client.get(reverse("aprobaciones:detalle", args=[self._aprobacion_vigente(ticket).pk]))
        self.assertContains(detalle, "propuesta.txt")
        self.assertContains(detalle, url)
        self.assertEqual(self.client.get(url).status_code, 200)

        self._entrar(self.ajeno)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_el_aprobador_no_gana_acceso_al_resto_del_ticket(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._entrar(self.aprobador)
        self.assertEqual(self._detalle(ticket).status_code, 403)

    def test_el_contexto_de_una_aprobacion_por_fases_trae_fase_y_bloque(self):
        from apps.aprobaciones.views import _contexto_workflow_por_esquema
        from apps.workflows.models import EsquemaAprobacionWorkflow

        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        vinculo = EsquemaAprobacionWorkflow.objects.get(instancia_etapa__instancia_workflow=self._instancia(ticket))
        contexto = _contexto_workflow_por_esquema([vinculo.esquema_id])[vinculo.esquema_id]
        self.assertTrue(contexto["por_fases"])
        self.assertIsNone(contexto["etapa"])
        self.assertEqual((contexto["fase"].nombre, contexto["bloque"].nombre), ("Revisión", "Revisar presentación"))


# ---------------------------------------------------------------------------
# 4.F2 — código público, seguimiento del solicitante, separación solicitante/trabajo
# y fecha requerida
# ---------------------------------------------------------------------------


class CodigoPublicoTicketTests(_EscenarioAtencionMixin, TestCase):
    """`TCK-000123`: corto, único, estable y asignado una sola vez al radicar."""

    def setUp(self):
        self._preparar_escenario()

    def _otro_radicado(self):
        ticket = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(ticket, self.solicitante, self.campos)
        radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        return ticket

    def test_el_borrador_no_tiene_codigo(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        self.assertIsNone(borrador.consecutivo)
        self.assertIsNone(borrador.codigo)

    def test_radicar_asigna_un_codigo_corto_con_el_formato_esperado(self):
        import re

        self.assertRegex(self.ticket.codigo, r"^TCK-\d{6}$")
        self.assertNotIn(str(self.ticket.radicado), self.ticket.codigo)
        self.assertEqual(self.ticket.codigo, f"TCK-{self.ticket.consecutivo:06d}")
        self.assertIsNotNone(re.match(r"^TCK-0+\d+$", self.ticket.codigo))

    def test_los_codigos_son_consecutivos_y_unicos(self):
        segundo = self._otro_radicado()
        tercero = self._otro_radicado()
        self.assertEqual(segundo.consecutivo, self.ticket.consecutivo + 1)
        self.assertEqual(tercero.consecutivo, self.ticket.consecutivo + 2)
        self.assertEqual(len({self.ticket.codigo, segundo.codigo, tercero.codigo}), 3)

    def test_la_unicidad_la_garantiza_la_base_de_datos(self):
        otro = crear_borrador(self.solicitante, self.servicio)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.filter(pk=otro.pk).update(consecutivo=self.ticket.consecutivo)

    def test_el_codigo_es_estable_a_lo_largo_de_la_vida_del_ticket(self):
        codigo = self.ticket.codigo
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.codigo, codigo)

    def test_el_codigo_no_se_puede_modificar_ni_borrar(self):
        for nuevo in (self.ticket.consecutivo + 500, None):
            with self.subTest(nuevo=nuevo):
                self.ticket.consecutivo = nuevo
                with self.assertRaises(ValidationError):
                    self.ticket.save()
                self.ticket.refresh_from_db()

    def test_un_borrador_o_una_radicacion_que_falla_no_gastan_numero(self):
        from apps.tickets.models import ConsecutivoTicket

        antes = ConsecutivoTicket.objects.get(pk=1).ultimo
        borrador = crear_borrador(self.solicitante, self.servicio)
        Campo.objects.filter(pk=self.campos["Texto"].pk).update(obligatorio=True)  # nunca queda completo
        with self.assertRaises(ValidationError):
            radicar_ticket(borrador, self.solicitante)
        self.assertEqual(ConsecutivoTicket.objects.get(pk=1).ultimo, antes)

    def test_se_puede_buscar_un_ticket_por_su_codigo(self):
        from apps.tickets.codigos import consecutivo_desde_codigo

        numero = self.ticket.consecutivo
        for escrito in (self.ticket.codigo, self.ticket.codigo.lower(), f" tck {numero} ", f"TCK-{numero}"):
            with self.subTest(escrito=escrito):
                self.assertEqual(consecutivo_desde_codigo(escrito), numero)
        for invalido in ("", "123", "TCK-", "TICKET-1", "TCK-12a"):
            with self.subTest(invalido=invalido):
                self.assertIsNone(consecutivo_desde_codigo(invalido))
        self.assertEqual(Ticket.objects.get(consecutivo=consecutivo_desde_codigo(self.ticket.codigo)), self.ticket)


class ConfirmacionYMisTicketsTests(_EscenarioAtencionMixin, TestCase):
    """Después de radicar: confirmación con el código y dos caminos; «Mis tickets» = lo que
    yo solicité, nunca lo que debo atender."""

    def setUp(self):
        self._preparar_escenario()
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)

    def _borrador_completo(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(borrador, self.solicitante, self.campos)
        return borrador

    def test_enviar_lleva_a_la_confirmacion_y_no_a_trabajo(self):
        borrador = self._borrador_completo()
        respuesta = self.client.post(reverse("tickets:enviar", args=[borrador.pk]))
        self.assertRedirects(respuesta, reverse("tickets:enviada", args=[borrador.pk]))
        self.assertNotIn(reverse("tickets:cola"), respuesta["Location"])

    def test_la_confirmacion_muestra_el_codigo_y_los_dos_botones(self):
        respuesta = self.client.get(reverse("tickets:enviada", args=[self.ticket.pk]))
        self.assertContains(respuesta, "Ticket creado")
        self.assertContains(respuesta, self.ticket.codigo)
        self.assertContains(respuesta, "Ver mi ticket")
        self.assertContains(respuesta, reverse("tickets:seguimiento", args=[self.ticket.pk]))
        self.assertContains(respuesta, "Ir a Mis tickets")
        self.assertContains(respuesta, reverse("tickets:mis_tickets"))
        self.assertNotContains(respuesta, "Tomar")
        self.assertNotContains(respuesta, reverse("tickets:cola"))

    def test_mis_tickets_incluye_los_mios_en_cualquier_estado(self):
        tomar_ticket(self.ticket, self.responsable_directo)  # ahora EN_ATENCION
        borrador = self._borrador_completo()
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        self.assertEqual({t.pk for t in respuesta.context["tickets"]}, {self.ticket.pk, borrador.pk})
        self.assertContains(respuesta, self.ticket.codigo)
        self.assertContains(respuesta, reverse("tickets:seguimiento", args=[self.ticket.pk]))
        self.assertContains(respuesta, "Ver ticket")
        self.assertContains(respuesta, self.servicio.nombre)

    def test_mis_tickets_no_incluye_lo_que_debo_atender(self):
        self.client.logout()
        self.client.login(username="responsable23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        self.assertEqual(list(respuesta.context["tickets"]), [])
        self.assertNotContains(respuesta, self.ticket.codigo)

    def test_cada_fila_muestra_estado_responsable_y_fecha_objetivo(self):
        asignar_ticket(self.ticket, self.gestor_area, equipo=self.equipo)
        objetivo = timezone.now() + timedelta(days=3)
        Ticket.objects.filter(pk=self.ticket.pk).update(
            tiempo_objetivo_cantidad=3, tiempo_objetivo_unidad="DIAS",
            fecha_objetivo_original=objetivo, fecha_objetivo_vigente=objetivo,
        )
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        self.assertContains(respuesta, "Radicado")
        self.assertContains(respuesta, self.equipo.nombre)
        self.assertContains(respuesta, "Objetivo:")

    def test_el_borrador_sigue_abriendose_para_continuar(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        self.assertContains(respuesta, reverse("tickets:borrador", args=[borrador.pk]))


class SeguimientoSolicitanteTests(_EscenarioAtencionMixin, TestCase):
    """«Ver mi ticket»: seguimiento de solo lectura, sin acciones internas y sin acceso ajeno."""

    def setUp(self):
        self._preparar_escenario()
        self.url = reverse("tickets:seguimiento", args=[self.ticket.pk])

    def _ver(self, usuario):
        self.client.logout()
        self.assertTrue(self.client.login(username=usuario.get_username(), password=CLAVE_PRUEBA))
        return self.client.get(self.url)

    def test_el_solicitante_ve_su_ticket_con_lo_esencial(self):
        asignar_ticket(self.ticket, self.gestor_area, equipo=self.equipo)
        respuesta = self._ver(self.solicitante)
        self.assertEqual(respuesta.status_code, 200)
        self.assertTemplateUsed(respuesta, "tickets/seguimiento.html")
        self.assertContains(respuesta, self.ticket.codigo)
        self.assertContains(respuesta, self.servicio.nombre)
        self.assertContains(respuesta, "Radicado")  # estado
        self.assertContains(respuesta, self.equipo.nombre)  # quién lo tiene
        self.assertContains(respuesta, "Respuestas")  # lo que pidió
        self.assertContains(respuesta, "Texto")

    def test_no_ofrece_ninguna_accion_interna(self):
        respuesta = self._ver(self.solicitante)
        for nombre in ("tomar", "asignar", "reasignar", "resolver", "cerrar", "iniciar_atencion"):
            with self.subTest(nombre):
                self.assertNotContains(respuesta, reverse(f"tickets:{nombre}", args=[self.ticket.pk]))
        self.assertNotContains(respuesta, "Trabajo actual")
        self.assertNotContains(respuesta, "Entregables")

    def test_no_revela_usernames_ni_ids_internos_del_responsable(self):
        responsable = Usuario.objects.create_user(
            username="ana.perez.interno", password=CLAVE_PRUEBA, first_name="Ana", last_name="Pérez"
        )
        _otorgar_tickets_atender(responsable)
        Ticket.objects.filter(pk=self.ticket.pk).update(usuario_responsable=responsable)
        respuesta = self._ver(self.solicitante)
        self.assertContains(respuesta, "Ana Pérez")
        self.assertNotContains(respuesta, "ana.perez.interno")
        self.assertNotContains(respuesta, "tickets.atender")

    def test_un_usuario_ajeno_recibe_403_aunque_cambie_el_id(self):
        self.assertEqual(self._ver(self.ajeno).status_code, 403)
        self.assertEqual(self._ver(self.gestor_otra_area).status_code, 403)
        otro = Usuario.objects.create_user(username="otro_solicitante23", password=CLAVE_PRUEBA)
        self.client.logout()
        self.client.login(username="otro_solicitante23", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(reverse("tickets:seguimiento", args=[99999999])).status_code, 404)

    def test_quien_ya_puede_consultar_el_ticket_tambien_lo_ve(self):
        self.assertEqual(self._ver(self.responsable_directo).status_code, 200)
        self.assertEqual(self._ver(self.gestor_area).status_code, 200)

    def test_exige_sesion(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_un_borrador_lleva_al_formulario_y_no_se_ve_si_es_ajeno(self):
        borrador = crear_borrador(self.solicitante, self.servicio)
        url = reverse("tickets:seguimiento", args=[borrador.pk])
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        self.assertRedirects(self.client.get(url), reverse("tickets:borrador", args=[borrador.pk]))
        self.client.logout()
        self.client.login(username="ajeno23", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_sin_flujo_por_fases_no_hay_progreso(self):
        respuesta = self._ver(self.solicitante)
        self.assertIsNone(respuesta.context["progreso"])
        self.assertNotContains(respuesta, "Progreso")

    def test_el_solicitante_aun_puede_responder_una_solicitud_de_informacion(self):
        ticket = tomar_ticket(self.ticket, self.responsable_directo)  # la instancia devuelta es la EN_ATENCION
        solicitar_informacion(ticket, self.responsable_directo, "¿Puedes aclarar el motivo?")
        respuesta = self._ver(self.solicitante)
        self.assertContains(respuesta, "te pidió más información")
        self.assertContains(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))

    def test_el_detalle_operativo_conserva_su_contenido_y_enlaza_al_seguimiento_del_solicitante(self):
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        detalle = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        self.assertContains(detalle, self.ticket.codigo)
        self.assertContains(detalle, self.url)
        self.assertContains(detalle, "Línea de tiempo")
        self.assertContains(detalle, "Respuestas")


class SeguimientoPorFasesTests(_MediaAisladaMixin, _EscenarioE2Mixin, TestCase):
    """El progreso sale del estado real de la ejecución (fase actual, pasadas y fases omitidas)."""

    def setUp(self):
        self._preparar_e2()

    def _progreso(self, ticket):
        self.client.logout()
        self.client.login(username=self.solicitante.get_username(), password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:seguimiento", args=[ticket.pk]))
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.context["progreso"]
        return respuesta, {fila["nombre"]: fila["situacion"] for fila in datos["fases"]}, datos

    def test_la_fase_actual_avanza_con_la_ejecucion(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))

        respuesta, situacion, datos = self._progreso(ticket)
        self.assertEqual(datos["actual"], "Recepción")
        self.assertEqual(situacion["Recepción"], "actual")
        self.assertEqual(situacion["Producción"], "pendiente")
        self.assertContains(respuesta, "Fase actual")

        self._hasta_la_primera_revision(ticket, definicion)
        _respuesta, situacion, datos = self._progreso(ticket)
        self.assertEqual(datos["actual"], "Revisión")
        self.assertEqual(situacion["Recepción"], "completada")
        self.assertEqual(situacion["Producción"], "completada")
        self.assertEqual(situacion["Revisión"], "actual")
        self.assertEqual(situacion["Entrega"], "pendiente")

    def test_las_fases_futuras_no_se_prometen_como_obligatorias(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        respuesta, _situacion, datos = self._progreso(ticket)
        self.assertTrue(datos["hay_pendientes"])
        self.assertContains(respuesta, "no todas se recorren siempre")

    def test_una_fase_a_la_que_no_se_llego_queda_como_no_necesaria_al_terminar(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "APROBADA")
        self._completar(ticket, "Entregar resultado")

        respuesta, situacion, datos = self._progreso(ticket)
        self.assertTrue(datos["terminado"])
        self.assertEqual(datos["actual"], "")
        self.assertEqual(situacion["Ajustes"], "no_aplico")
        self.assertEqual(situacion["Entrega"], "completada")
        self.assertContains(respuesta, "No fue necesaria")

    def test_una_devolucion_pasa_por_ajustes_y_vuelve_a_la_revision(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self._revisar(ticket, "DEVUELTA")

        _respuesta, situacion, datos = self._progreso(ticket)
        self.assertEqual(datos["actual"], "Ajustes")
        self.assertEqual(situacion["Revisión"], "completada")

    def test_mis_tickets_muestra_la_fase_actual(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._hasta_la_primera_revision(ticket, definicion)
        self.client.login(username=self.solicitante.get_username(), password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:mis_tickets"))
        self.assertContains(respuesta, "Fase: Revisión")
        self.assertContains(respuesta, ticket.codigo)


class SolicitanteNoTomaSuTicketTests(_EscenarioAtencionMixin, TestCase):
    """Regla V1 en dominio/autorización: quien solicita no se autoasigna la atención."""

    def setUp(self):
        self._preparar_escenario()
        # Solicita Y pertenece al equipo responsable (y tiene `tickets.atender` global).
        self.doble = Usuario.objects.create_user(username="doble_rol23", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.doble, activo=True)
        _otorgar_tickets_atender(self.doble)
        self.suyo = crear_borrador(self.doble, self.servicio)
        _completar_texto(self.suyo, self.doble, self.campos)
        radicar_ticket(self.suyo, self.doble)
        self.suyo.refresh_from_db()

    def test_el_solicitante_no_puede_tomar_su_propio_ticket_aunque_sea_del_equipo(self):
        self.assertTrue(usuario_es_responsable_configurado(self.doble, self.servicio))
        self.assertFalse(puede_tomar(self.doble, self.suyo))

    def test_la_operacion_de_dominio_tambien_lo_rechaza(self):
        with self.assertRaises(PermissionDenied):
            tomar_ticket(self.suyo, self.doble)
        self.suyo.refresh_from_db()
        self.assertEqual((self.suyo.estado, self.suyo.usuario_responsable_id), (Ticket.Estado.RADICADO, None))

    def test_el_endpoint_directo_tambien_lo_rechaza(self):
        self.client.login(username="doble_rol23", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tickets:tomar", args=[self.suyo.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.suyo.refresh_from_db()
        self.assertEqual((self.suyo.estado, self.suyo.usuario_responsable_id), (Ticket.Estado.RADICADO, None))
        self.assertTrue(any("autorización" in str(m) for m in get_messages(respuesta.wsgi_request)))

    def test_el_detalle_no_ofrece_tomar_al_solicitante(self):
        self.client.login(username="doble_rol23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:detalle", args=[self.suyo.pk]))
        self.assertFalse(respuesta.context["puede_tomar"])
        self.assertNotContains(respuesta, reverse("tickets:tomar", args=[self.suyo.pk]))

    def test_un_miembro_valido_del_equipo_si_puede_tomar_ese_ticket(self):
        self.assertTrue(puede_tomar(self.miembro_equipo, self.suyo))
        tomado = tomar_ticket(self.suyo, self.miembro_equipo)
        self.assertEqual((tomado.estado, tomado.usuario_responsable_id), (Ticket.Estado.EN_ATENCION, self.miembro_equipo.pk))

    def test_el_mismo_usuario_si_puede_tomar_los_tickets_de_otras_personas(self):
        self.assertTrue(puede_tomar(self.doble, self.ticket))
        self.assertEqual(tomar_ticket(self.ticket, self.doble).usuario_responsable_id, self.doble.pk)

    def test_un_responsable_global_tampoco_toma_su_propio_ticket(self):
        global_ = Usuario.objects.create_user(username="global_solicita23", password=CLAVE_PRUEBA)
        _otorgar_tickets_atender(global_)
        propio = crear_borrador(global_, self.servicio)
        _completar_texto(propio, global_, self.campos)
        radicar_ticket(propio, global_)
        propio.refresh_from_db()
        self.assertFalse(puede_tomar(global_, propio))
        with self.assertRaises(PermissionDenied):
            tomar_ticket(propio, global_)

    def test_asignar_sigue_siendo_otra_operacion_y_no_cambia(self):
        self.assertTrue(puede_asignar(self.gestor_area, self.suyo))

    def test_crear_un_ticket_no_lo_pone_en_la_cola_del_solicitante(self):
        self.client.login(username="doble_rol23", password=CLAVE_PRUEBA)
        cola = self.client.get(reverse("tickets:cola"))
        self.assertNotIn(self.suyo, cola.context["tickets"])
        self.assertIn(self.ticket, cola.context["tickets"])  # lo ajeno sí sigue siendo trabajo suyo

    def test_los_miembros_validos_siguen_viendolo_en_su_cola(self):
        self.client.login(username="miembro23", password=CLAVE_PRUEBA)
        cola = self.client.get(reverse("tickets:cola"))
        self.assertIn(self.suyo, cola.context["tickets"])
        self.assertTrue(puede_ver_en_cola(self.miembro_equipo, self.suyo))

    def test_si_alguien_le_asigna_su_propio_ticket_si_es_trabajo_suyo(self):
        from apps.tickets.trabajo import tickets_a_cargo

        asignado = asignar_ticket(self.suyo, self.gestor_area, usuario=self.doble)
        self.assertIn(asignado.pk, [t.pk for t in tickets_a_cargo(self.doble)])

    def test_el_escenario_a_b_del_equipo(self):
        # A solicita; B pertenece al equipo responsable.
        self.assertFalse(puede_ver_en_cola(self.ajeno, self.ticket))
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)
        self.assertIn(self.ticket, self.client.get(reverse("tickets:mis_tickets")).context["tickets"])
        self.assertNotIn(self.ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        self.assertFalse(puede_tomar(self.solicitante, self.ticket))
        self.client.logout()
        self.client.login(username="miembro23", password=CLAVE_PRUEBA)
        self.assertIn(self.ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        self.assertEqual(self.client.get(reverse("tickets:seguimiento", args=[self.ticket.pk])).status_code, 200)
        self.assertTrue(puede_tomar(self.miembro_equipo, self.ticket))


class FechaRequeridaPlazoTests(TestCase):
    """La fecha que pide el solicitante frente al tiempo objetivo del servicio: informa, no bloquea."""

    def setUp(self):
        self.usuario = Usuario.objects.create_user(username="plazo_solicitante", password=CLAVE_PRUEBA)
        self.responsable = Usuario.objects.create_user(username="plazo_responsable", password=CLAVE_PRUEBA)
        self.equipo = Equipo.objects.create(nombre="Equipo plazo")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.responsable)
        _otorgar_tickets_atender(self.responsable)

    def _servicio(self, *, con_marca=True, tiempo=(5, "DIAS", True), tipo=Campo.TipoCampo.FECHA):
        servicio, _version, campos = _crear_servicio_con_formulario(
            self.usuario,
            [
                {"tipo": Campo.TipoCampo.TEXTO, "etiqueta": "Tema", "orden": 0},
                {"tipo": tipo, "etiqueta": "Para cuándo", "orden": 1, "es_fecha_requerida": con_marca},
            ],
        )
        ServicioResponsable.objects.create(
            servicio=servicio, tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO, equipo=self.equipo
        )
        if tiempo is not None:
            Servicio.objects.filter(pk=servicio.pk).update(
                tiempo_objetivo_cantidad=tiempo[0], tiempo_objetivo_unidad=tiempo[1], tiempo_objetivo_habiles=tiempo[2]
            )
        servicio.refresh_from_db()
        self.campos = campos
        return servicio

    def _borrador(self, servicio, fecha=None):
        ticket = crear_borrador(self.usuario, servicio)
        respuestas = {self.campos["Tema"].id: "Presentación cliente Ara"}
        if fecha is not None:
            respuestas[self.campos["Para cuándo"].id] = fecha
        guardar_respuestas_borrador(ticket, self.usuario, respuestas)
        ticket.refresh_from_db()
        return ticket

    def _evaluar(self, ticket, **kw):
        from apps.tickets import plazos

        return plazos.evaluar_plazo(ticket, **kw)

    def test_sin_tiempo_objetivo_no_hay_alerta(self):
        servicio = self._servicio(tiempo=None)
        ticket = self._borrador(servicio, "2026-10-09")
        self.assertIsNone(self._evaluar(ticket))
        self.assertEqual(self._fecha(ticket).isoformat(), "2026-10-09")  # la fecha sí se conserva

    def _fecha(self, ticket):
        from apps.tickets import plazos

        return plazos.fecha_solicitada(ticket)["valor"]

    def test_sin_fecha_diligenciada_no_hay_alerta(self):
        ticket = self._borrador(self._servicio(), None)
        self.assertIsNone(self._evaluar(ticket))

    def test_un_campo_fecha_sin_la_marca_no_es_un_plazo(self):
        ticket = self._borrador(self._servicio(con_marca=False), "2026-10-09")
        self.assertIsNone(self._evaluar(ticket))
        from apps.tickets import plazos

        self.assertIsNone(plazos.fecha_solicitada(ticket))

    def test_la_comparacion_usa_el_calculo_de_dias_habiles_de_4a1(self):
        from apps.tickets import tiempos

        servicio = self._servicio()
        with _bogota():
            ahora = _local(2026, 10, 5, 9)  # lunes: 5 días hábiles → lunes 12
            esperado = tiempos.calcular_fecha_objetivo(ahora, 5, "DIAS", True)
            casos = {"2026-10-09": True, "2026-10-11": True, "2026-10-12": False, "2026-10-13": False}
            for fecha, anticipada in casos.items():
                with self.subTest(fecha=fecha):
                    ticket = self._borrador(servicio, fecha)
                    plazo = self._evaluar(ticket, ahora=ahora)
                    self.assertEqual(plazo["objetivo"], esperado)
                    self.assertEqual(plazo["anticipada"], anticipada)
                    self.assertEqual(plazo["tiempo"], "5 días hábiles")

    def test_fecha_y_hora_se_compara_con_el_instante_exacto(self):
        servicio = self._servicio(tiempo=(8, "HORAS", False), tipo=Campo.TipoCampo.FECHA_HORA)
        ticket = self._borrador(servicio, "2026-10-12T08:00")
        pedida = self._fecha(ticket)
        self.assertTrue(self._evaluar(ticket, ahora=pedida - timedelta(hours=7))["anticipada"])  # objetivo = pedida + 1 h
        self.assertFalse(self._evaluar(ticket, ahora=pedida - timedelta(hours=9))["anticipada"])  # objetivo = pedida − 1 h
        self.assertFalse(self._evaluar(ticket, ahora=pedida - timedelta(hours=8))["anticipada"])  # justo en el objetivo
        self.assertEqual(self._evaluar(ticket, ahora=pedida)["tiempo"], "8 horas")

    def test_la_alerta_no_bloquea_revisar_ni_radicar(self):
        servicio = self._servicio()
        ayer = (timezone.localdate() - timedelta(days=1)).isoformat()
        ticket = self._borrador(servicio, ayer)
        self.client.login(username="plazo_solicitante", password=CLAVE_PRUEBA)

        revision = self.client.get(reverse("tickets:revisar", args=[ticket.pk]))
        self.assertEqual(revision.status_code, 200)
        self.assertContains(revision, "La fecha solicitada es anterior al tiempo establecido")
        self.assertContains(revision, "5 días hábiles")
        self.assertContains(revision, "Puedes continuar con la solicitud")
        self.assertContains(revision, reverse("tickets:enviar", args=[ticket.pk]))  # «Solicitar» sigue ahí

        enviado = self.client.post(reverse("tickets:enviar", args=[ticket.pk]))
        self.assertRedirects(enviado, reverse("tickets:enviada", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)

    def test_una_fecha_dentro_del_objetivo_no_muestra_alerta(self):
        servicio = self._servicio()
        lejos = (timezone.localdate() + timedelta(days=60)).isoformat()
        ticket = self._borrador(servicio, lejos)
        self.client.login(username="plazo_solicitante", password=CLAVE_PRUEBA)
        revision = self.client.get(reverse("tickets:revisar", args=[ticket.pk]))
        self.assertEqual(revision.status_code, 200)
        self.assertNotContains(revision, "La fecha solicitada es anterior")

    def test_la_fecha_solicitada_se_puede_recuperar_despues_de_radicar(self):
        servicio = self._servicio()
        ayer = timezone.localdate() - timedelta(days=1)
        ticket = self._borrador(servicio, ayer.isoformat())
        radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        plazo = self._evaluar(ticket)
        self.assertEqual(plazo["valor"], ayer)
        self.assertTrue(plazo["anticipada"])
        self.assertEqual(plazo["objetivo"], ticket.fecha_objetivo_original)  # el compromiso real, no una recomputación

    def test_quien_atiende_ve_el_aviso_y_no_cambia_nada_mas(self):
        servicio = self._servicio()
        ticket = self._borrador(servicio, (timezone.localdate() - timedelta(days=1)).isoformat())
        radicar_ticket(ticket, self.usuario)
        ticket.refresh_from_db()
        estado, vigente, politica = ticket.estado, ticket.fecha_objetivo_vigente, ticket.prorroga_politica

        self.client.login(username="plazo_responsable", password=CLAVE_PRUEBA)
        detalle = self.client.get(reverse("tickets:detalle", args=[ticket.pk]))
        self.assertContains(detalle, "Fecha solicitada anterior al tiempo objetivo")
        self.assertContains(detalle, "Objetivo del servicio")
        ticket.refresh_from_db()
        # Es información operativa: ni prioridad, ni SLA, ni prórroga automática.
        self.assertEqual((ticket.estado, ticket.fecha_objetivo_vigente, ticket.prorroga_politica), (estado, vigente, politica))
        self.assertFalse(ticket.prorrogas.exists())

    def test_el_solicitante_ve_su_fecha_solicitada_y_la_objetivo_en_el_seguimiento(self):
        servicio = self._servicio()
        ticket = self._borrador(servicio, "2030-01-15")
        radicar_ticket(ticket, self.usuario)
        self.client.login(username="plazo_solicitante", password=CLAVE_PRUEBA)
        seguimiento = self.client.get(reverse("tickets:seguimiento", args=[ticket.pk]))
        self.assertContains(seguimiento, "Fecha solicitada")
        self.assertContains(seguimiento, "15 Ene 2030")
        self.assertContains(seguimiento, "Fecha objetivo")
        self.assertNotContains(seguimiento, "anterior al tiempo")


# ---------------------------------------------------------------------------
# 4.F3 — experiencia operativa de Trabajo (Cola, Mi trabajo, espacio por fases, Home)
# ---------------------------------------------------------------------------


class _EscenarioTrabajoMixin(_EscenarioE2Mixin):
    """Servicio «Creación de presentaciones» sobre la plantilla de cinco fases (4.E2)."""

    def _entrar(self, usuario):
        self.client.logout()
        self.assertTrue(self.client.login(username=usuario.get_username(), password=CLAVE_PRUEBA))

    def _trabajo(self, ticket):
        return self.client.get(reverse("tickets:trabajo", args=[ticket.pk]))

    def _escenario_completo(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)
        ticket = self._radicar_e2(servicio, campos)
        return servicio, definicion, ticket

    def _estados_fases(self, respuesta):
        return {fila["nombre"]: fila["situacion"] for fila in respuesta.context["progreso"]["fases"]}


class ColaYTomarTests(_EscenarioTrabajoMixin, TestCase):
    """Cola → Ver resumen (no toma) → Tomar → Mi trabajo."""

    def setUp(self):
        self._preparar_e2()
        _servicio, _definicion, self.ticket = self._escenario_completo()

    def test_el_solicitante_lo_ve_en_mis_tickets_y_no_en_la_cola(self):
        self._entrar(self.solicitante)
        self.assertIn(self.ticket, self.client.get(reverse("tickets:mis_tickets")).context["tickets"])
        self.assertNotIn(self.ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        self.assertEqual(self.client.get(reverse("core:mi_trabajo")).context["tarjetas"], [])

    def test_quien_puede_tomarlo_lo_ve_con_lo_necesario_para_decidir(self):
        self._entrar(self.responsable)
        cola = self.client.get(reverse("tickets:cola"))
        self.assertIn(self.ticket, cola.context["tickets"])
        self.assertContains(cola, self.ticket.codigo)
        self.assertContains(cola, self.ticket.detalle_servicio.servicio.nombre)
        self.assertContains(cola, "Ver resumen")
        self.assertContains(cola, "Tomar ticket")
        self.assertContains(cola, reverse("tickets:resumen", args=[self.ticket.pk]))

    def test_ver_el_resumen_no_toma_el_ticket(self):
        self._entrar(self.responsable)
        pagina = self.client.get(reverse("tickets:resumen", args=[self.ticket.pk]))
        fragmento = self.client.get(reverse("tickets:resumen", args=[self.ticket.pk]), HTTP_X_REQUESTED_WITH="fetch")
        for respuesta in (pagina, fragmento):
            self.assertEqual(respuesta.status_code, 200)
            self.assertContains(respuesta, self.ticket.codigo)
            self.assertContains(respuesta, "Tomar ticket")  # se ofrece, no se ejecuta
        self.assertNotContains(fragmento, "<html")
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.usuario_responsable_id), (Ticket.Estado.RADICADO, None))

    def test_el_resumen_solo_lo_ve_quien_puede_verlo_en_la_cola(self):
        self._entrar(self.solicitante)
        self.assertEqual(self.client.get(reverse("tickets:resumen", args=[self.ticket.pk])).status_code, 403)
        ajeno = Usuario.objects.create_user(username="trabajo_ajeno", password=CLAVE_PRUEBA)
        self._entrar(ajeno)
        self.assertEqual(self.client.get(reverse("tickets:resumen", args=[self.ticket.pk])).status_code, 403)

    def test_tomar_lo_saca_de_la_cola_y_lo_lleva_a_mi_trabajo(self):
        self._entrar(self.responsable)
        mi_trabajo = reverse("core:mi_trabajo")
        respuesta = self.client.post(reverse("tickets:tomar", args=[self.ticket.pk]), {"next": mi_trabajo})
        self.assertRedirects(respuesta, mi_trabajo)
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.usuario_responsable_id), (Ticket.Estado.EN_ATENCION, self.responsable.pk))
        self.assertNotIn(self.ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        tarjetas = self.client.get(mi_trabajo).context["tarjetas"]
        self.assertEqual([t["ticket"].pk for t in tarjetas], [self.ticket.pk])
        self.assertEqual(tarjetas[0]["fase"], "Recepción")
        self.assertEqual(tarjetas[0]["bloque"], "Analizar solicitud")

    def test_un_destino_externo_en_next_se_ignora(self):
        self._entrar(self.responsable)
        respuesta = self.client.post(reverse("tickets:tomar", args=[self.ticket.pk]), {"next": "https://otro.example/x"})
        self.assertRedirects(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]), fetch_redirect_response=False)

    def test_el_solicitante_no_puede_tomarlo_ni_llamando_al_endpoint(self):
        self._entrar(self.solicitante)
        self.client.post(reverse("tickets:tomar", args=[self.ticket.pk]))
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.usuario_responsable_id), (Ticket.Estado.RADICADO, None))


class EspacioDeTrabajoPorFasesTests(_MediaAisladaMixin, _EscenarioTrabajoMixin, TestCase):
    """El espacio muestra el estado REAL del flujo y solo ofrece lo que el motor y el dominio permiten."""

    def setUp(self):
        self._preparar_e2()
        self.servicio, self.definicion, ticket = self._escenario_completo()
        self.ticket = self._atender(ticket)
        self._entrar(self.responsable)

    def test_las_fases_salen_de_la_ejecucion_real_y_las_futuras_no_son_operables(self):
        respuesta = self._trabajo(self.ticket)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(
            self._estados_fases(respuesta),
            {"Recepción": "actual", "Producción": "pendiente", "Revisión": "pendiente", "Ajustes": "pendiente", "Entrega": "pendiente"},
        )
        self.assertEqual(respuesta.context["panel"]["nombre"], "Analizar solicitud")
        self.assertEqual([f["nombre"] for f in respuesta.context["bloques"]], ["Analizar solicitud"])
        # Lo futuro no ofrece ninguna acción: el único formulario de actividad es el de la tarea vigente.
        tarea = respuesta.context["panel"]["tarea"]
        self.assertContains(respuesta, reverse("tareas:completar", args=[tarea.pk]))
        self.assertNotContains(respuesta, "Finalizar fase")
        self.assertNotContains(respuesta, "Continuar flujo")

    def test_completar_la_actividad_hace_que_el_motor_entre_solo_a_la_siguiente_fase(self):
        respuesta = self._trabajo(self.ticket)
        tarea = respuesta.context["panel"]["tarea"]
        enviada = self.client.post(reverse("tareas:completar", args=[tarea.pk]), {"next": reverse("tickets:trabajo", args=[self.ticket.pk])})
        self.assertRedirects(enviada, reverse("tickets:trabajo", args=[self.ticket.pk]))
        respuesta = self._trabajo(self.ticket)
        estados = self._estados_fases(respuesta)
        self.assertEqual((estados["Recepción"], estados["Producción"], estados["Revisión"]), ("completada", "actual", "pendiente"))
        self.assertEqual(respuesta.context["panel"]["nombre"], "Preparar presentación")

    def test_el_entregable_se_completa_desde_el_espacio_y_el_flujo_continua_a_la_revision(self):
        self._completar(self.ticket, "Analizar solicitud")
        self._completar(self.ticket, "Preparar presentación")
        respuesta = self._trabajo(self.ticket)
        self.assertEqual(respuesta.context["panel"]["tipo"], "ENTREGABLE")
        self.assertTrue(respuesta.context["panel"]["puede_escribir"])
        entregable = respuesta.context["panel"]["entregable"]
        self.assertContains(respuesta, reverse("tickets:entregable_resultado", args=[self.ticket.pk, entregable.pk]))
        self.assertNotContains(respuesta, "Continuar flujo")

        self.client.post(
            reverse("tickets:entregable_resultado", args=[self.ticket.pk, entregable.pk]),
            {"valor": "Versión 1 de la presentación", "next": reverse("tickets:trabajo", args=[self.ticket.pk])},
        )
        respuesta = self._trabajo(self.ticket)
        self.assertEqual(self._estados_fases(respuesta)["Revisión"], "actual")
        self.assertEqual(respuesta.context["panel"]["tipo"], "APROBACION")
        self.assertContains(respuesta, "Enviado a revisión")
        self.assertContains(respuesta, "Pendiente")
        # Quien atiende no puede aprobar: no se le ofrece abrir la aprobación.
        self.assertIsNone(respuesta.context["panel"]["aprobacion"])
        self.assertNotContains(respuesta, "Abrir la aprobación")

    def test_un_entregable_de_una_fase_futura_no_se_puede_escribir_por_url(self):
        entregable = self.ticket.entregables.get(definicion=self.definicion)
        for ruta, datos in (
            ("tickets:entregable_resultado", {"valor": "Adelantado"}),
            ("tickets:entregable_adjuntar", {"archivo": SimpleUploadedFile("adelantado.txt", b"x")}),
            ("tickets:entregable_confirmar", {}),
        ):
            with self.subTest(ruta):
                respuesta = self.client.post(reverse(ruta, args=[self.ticket.pk, entregable.pk]), datos)
                self.assertEqual(respuesta.status_code, 302)
                entregable.refresh_from_db()
                self.assertFalse(entregable.satisfecho)
        self.assertEqual(self._nombre_ultima(self.ticket), "Analizar solicitud")  # el flujo no se movió
        self.assertEqual(entregable.archivos.count(), 0)

    def test_el_detalle_tampoco_ofrece_escribir_un_entregable_que_aun_no_es_su_turno(self):
        detalle = self.client.get(reverse("tickets:detalle", args=[self.ticket.pk]))
        entregable = self.ticket.entregables.get(definicion=self.definicion)
        self.assertContains(detalle, "Se podrá completar cuando el flujo llegue a este paso")
        self.assertNotContains(detalle, reverse("tickets:entregable_resultado", args=[self.ticket.pk, entregable.pk]))

    def test_una_devolucion_habilita_ajustes_y_el_flujo_terminado_se_refleja(self):
        self._hasta_la_primera_revision(self.ticket, self.definicion)
        self._revisar(self.ticket, "DEVUELTA")
        respuesta = self._trabajo(self.ticket)
        estados = self._estados_fases(respuesta)
        self.assertEqual((estados["Revisión"], estados["Ajustes"]), ("completada", "actual"))
        self.assertEqual(respuesta.context["panel"]["nombre"], "Corregir presentación")

        self._completar(self.ticket, "Corregir presentación")
        self._satisfacer(self.ticket, self.definicion, "Versión 2")
        self._revisar(self.ticket, "APROBADA")
        self._completar(self.ticket, "Entregar resultado")
        respuesta = self._trabajo(self.ticket)
        self.assertTrue(respuesta.context["flujo_terminado"])
        self.assertContains(respuesta, "El flujo de trabajo terminó")
        self.assertEqual(self._instancia(self.ticket).estado, "COMPLETADA")

    def test_un_ajeno_o_el_solicitante_no_operan_el_espacio(self):
        self._entrar(self.solicitante)
        self.assertRedirects(
            self._trabajo(self.ticket), reverse("tickets:seguimiento", args=[self.ticket.pk]), fetch_redirect_response=False
        )
        ajeno = Usuario.objects.create_user(username="trabajo_ajeno2", password=CLAVE_PRUEBA)
        self._entrar(ajeno)
        self.assertEqual(self._trabajo(self.ticket).status_code, 403)
        tarea = self._trabajo_tarea()
        self.assertEqual(self.client.post(reverse("tareas:completar", args=[tarea.pk])).status_code, 403)

    def _trabajo_tarea(self):
        from apps.workflows.models import TareaWorkflow

        return TareaWorkflow.objects.get(instancia_etapa=self._ultima(self.ticket)).tarea

    def test_sin_flujo_se_indica_y_se_remite_al_detalle(self):
        servicio, _version, campos = _crear_servicio_con_formulario(self.solicitante, [])
        ticket = crear_borrador(self.solicitante, servicio)
        radicar_ticket(ticket, self.solicitante)
        ticket = self._atender(Ticket.objects.get(pk=ticket.pk))
        respuesta = self._trabajo(ticket)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "no tiene un flujo de trabajo asociado")


class TrabajoVariasActividadesYDecisionTests(_EscenarioTrabajoMixin, TestCase):
    def setUp(self):
        self._preparar_e2()

    def test_varias_actividades_avanzan_en_orden_y_la_ultima_cambia_de_fase_sola(self):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        servicio, campos, _definicion = self._servicio_e2()
        config = crear_nueva_version_configuracion(servicio, self.admin)
        for nombre in ("A", "B", "C"):
            self._act(config, self.f_recepcion, nombre)
        self._act(config, self.f_produccion, "D")
        self._activar(servicio, config)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._entrar(self.responsable)

        for anterior, siguiente in (("A", "B"), ("B", "C")):
            self.assertEqual(self._nombre_ultima(ticket), anterior)
            self._completar(ticket, anterior)
            self.assertEqual(self._nombre_ultima(ticket), siguiente)
            self.assertEqual(self._estados_fases(self._trabajo(ticket))["Recepción"], "actual")
        self._completar(ticket, "C")
        respuesta = self._trabajo(ticket)
        self.assertEqual(self._nombre_ultima(ticket), "D")
        self.assertEqual(self._estados_fases(respuesta)["Producción"], "actual")
        self.assertEqual(self._estados_fases(respuesta)["Recepción"], "completada")
        self.assertNotContains(respuesta, "Finalizar fase")

    def test_una_decision_se_evalua_sola_y_se_muestra_sin_boton(self):
        from apps.catalogo.configuracion_ejecucion import crear_nueva_version_configuracion

        servicio, campos, _definicion = self._servicio_e2()
        config = crear_nueva_version_configuracion(servicio, self.admin)
        previa = self._act(config, self.f_produccion, "Preparar")
        decision = self._agregar(config, self.f_produccion, BloqueOperativo.Tipo.DECISION, "¿Requiere revisión?")
        siguiente = self._act(config, self.f_produccion, "Revisar contenido")
        self._ruta(decision, siguiente, variable="formulario.requiere_revision", operador="IGUAL_A", valor="Sí", prioridad=0)
        self._ruta(decision, siguiente, es_fallback=True)
        self._activar(servicio, config)
        ticket = self._atender(self._radicar_e2(servicio, campos))
        self._entrar(self.responsable)

        self._completar(ticket, "Preparar")
        self.assertEqual(self._nombre_ultima(ticket), "Revisar contenido")  # nadie ejecutó la decisión
        respuesta = self._trabajo(ticket)
        filas = {f["nombre"]: f for f in respuesta.context["bloques"]}
        self.assertEqual(filas["¿Requiere revisión?"]["situacion"], "completado")
        self.assertEqual(filas["¿Requiere revisión?"]["detalle"], "Condición evaluada")
        self.assertEqual(filas["Revisar contenido"]["situacion"], "actual")
        self.assertEqual(respuesta.context["panel"]["tipo"], "ACTIVIDAD")
        self.assertContains(respuesta, "Condición evaluada")


class TareasDelFlujoSinDuenoTests(_EscenarioTrabajoMixin, TestCase):
    """GAP de 4.F3: la actividad «para el responsable del ticket» creada antes de que el ticket tuviera
    responsable pasa a quien lo toma (o a quien se lo asignan)."""

    def setUp(self):
        self._preparar_e2()

    def _servicio_con_actividad_del_responsable(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion

        servicio, campos, _definicion = self._servicio_e2()
        config = crear_nueva_version_configuracion(servicio, self.admin)
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_recepcion, tipo=BloqueOperativo.Tipo.ACTIVIDAD, nombre="Atender solicitud",
            configuracion={"tipo_actor": "RESPONSABLE_TICKET"},
        )
        self._activar(servicio, config)
        return servicio, campos

    def _tarea_vigente(self, ticket):
        from apps.workflows.models import TareaWorkflow

        return TareaWorkflow.objects.get(instancia_etapa=self._ultima(ticket)).tarea

    def test_tomar_el_ticket_entrega_la_actividad_pendiente_a_quien_lo_toma(self):
        servicio, campos = self._servicio_con_actividad_del_responsable()
        ticket = self._radicar_e2(servicio, campos)
        tarea = self._tarea_vigente(ticket)
        self.assertIsNone(tarea.usuario_responsable_id)  # el flujo arrancó sin responsable del ticket

        tomar_ticket(ticket, self.responsable)
        tarea.refresh_from_db()
        self.assertEqual(tarea.usuario_responsable_id, self.responsable.pk)
        self.assertTrue(tarea.historial.filter(tipo_evento="ASIGNADA", datos__causa="TICKET_TOMADO").exists())

        self._entrar(self.responsable)
        respuesta = self._trabajo(ticket)
        self.assertTrue(respuesta.context["panel"]["puede_completar_tarea"])
        self.assertContains(respuesta, "Completar actividad")

    def test_asignar_el_ticket_a_una_persona_tambien_le_entrega_la_actividad(self):
        servicio, campos = self._servicio_con_actividad_del_responsable()
        ticket = self._radicar_e2(servicio, campos)
        gestor = Usuario.objects.create_user(username="trabajo_gestor", password=CLAVE_PRUEBA)
        _otorgar_tickets_atender(gestor)
        asignar_ticket(ticket, gestor, usuario=self.responsable)
        self.assertEqual(self._tarea_vigente(ticket).usuario_responsable_id, self.responsable.pk)

    def test_no_pisa_a_un_responsable_ni_toca_actividades_de_otro_tipo(self):
        servicio, campos, definicion = self._servicio_e2()
        self._configurar_e2(servicio, definicion)  # actividades con responsable fijo (USUARIO)
        ticket = self._radicar_e2(servicio, campos)
        otro = Usuario.objects.create_user(username="trabajo_otro_miembro", password=CLAVE_PRUEBA)
        _otorgar_tickets_atender(otro)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=otro)
        antes = self._tarea_vigente(ticket).usuario_responsable_id
        tomar_ticket(ticket, otro)
        self.assertEqual(self._tarea_vigente(ticket).usuario_responsable_id, antes)
        self.assertNotEqual(antes, otro.pk)


class TrabajoEnElHomeYCalendarioTests(_EscenarioTrabajoMixin, TestCase):
    def setUp(self):
        self._preparar_e2()
        _servicio, _definicion, ticket = self._escenario_completo()
        self.ticket = self._atender(ticket)

    def test_el_home_resume_los_tickets_que_se_atienden_y_lleva_a_continuar(self):
        from apps.core import inicio

        resumen = inicio.resumen_trabajo(self.responsable)
        self.assertEqual([t["ticket"].pk for t in resumen["tickets"]], [self.ticket.pk])
        self.assertEqual(resumen["tickets"][0]["bloque"], "Analizar solicitud")
        self._entrar(self.responsable)
        respuesta = self.client.get(reverse("core:inicio"))
        self.assertContains(respuesta, self.ticket.codigo)
        self.assertContains(respuesta, reverse("tickets:trabajo", args=[self.ticket.pk]))
        self.assertEqual(resumen["url"], reverse("core:mi_trabajo"))

    def test_el_solicitante_no_ve_en_su_trabajo_un_ticket_que_solo_solicito(self):
        from apps.core import inicio

        self.assertEqual(inicio.resumen_trabajo(self.solicitante), None)

    def test_el_calendario_usa_solo_fechas_reales(self):
        from apps.core import inicio
        from apps.tareas.models import Tarea

        hasta = timezone.now() + timedelta(days=60)
        eventos = inicio._eventos(self.responsable, hasta)
        # La tarea que creó el flujo NO trae fecha límite: no se inventa una (GAP reportado en 4.F3).
        self.assertFalse([e for e in eventos if e["tipo"] == "tarea"])
        # La fecha objetivo del ticket es real y ya está fijada al radicar.
        ticket_eventos = [e for e in eventos if e["tipo"] == "ticket"]
        if self.ticket.fecha_objetivo_vigente is not None:
            self.assertEqual(len(ticket_eventos), 1)
            self.assertEqual(ticket_eventos[0]["fecha"], self.ticket.fecha_objetivo_vigente)
            self.assertEqual(ticket_eventos[0]["url"], reverse("tickets:trabajo", args=[self.ticket.pk]))
        else:
            self.assertEqual(ticket_eventos, [])

        # Si la tarea tiene una fecha límite (campo real de Tarea), sí aparece y abre la tarea.
        tarea = self._ultima_tarea()
        Tarea.objects.filter(pk=tarea.pk).update(fecha_limite=timezone.now() + timedelta(days=3))
        eventos = inicio._eventos(self.responsable, hasta)
        con_fecha = [e for e in eventos if e["tipo"] == "tarea"]
        self.assertEqual([e["url"] for e in con_fecha], [reverse("tareas:detalle", args=[tarea.pk])])

    def test_la_fecha_objetivo_del_ticket_aparece_en_el_calendario_del_home(self):
        from apps.core import inicio

        objetivo = timezone.now() + timedelta(days=4)
        Ticket.objects.filter(pk=self.ticket.pk).update(
            tiempo_objetivo_cantidad=4, tiempo_objetivo_unidad="DIAS",
            fecha_objetivo_original=objetivo, fecha_objetivo_vigente=objetivo,
        )
        agenda = inicio.agenda(self.responsable)
        self.assertTrue(agenda["hay_eventos"])
        self.assertIn(self.ticket.codigo, " ".join(e["titulo"] for e in agenda["proximos"]))

    def _ultima_tarea(self):
        from apps.workflows.models import TareaWorkflow

        return TareaWorkflow.objects.get(instancia_etapa=self._ultima(self.ticket)).tarea


# ---------------------------------------------------------------------------
# Sprint 4.G1 — Procesos programables: generación automática de ejecuciones.
# PROGRAMACIÓN (cuándo) ≠ WORKFLOW (ruta). Un ticket programado es un Ticket normal
# (tipo PROCESO, origen PROGRAMACION, sin solicitante) que entra a Cola → Mi trabajo.
# ---------------------------------------------------------------------------

from apps.catalogo.models import DefinicionEntregable, ProgramacionProceso  # noqa: E402
from apps.catalogo.operaciones import (  # noqa: E402
    configurar_politica_entrega,
    editar_servicio_general,
    retirar_responsable,
)
from apps.catalogo.programacion import configurar_programacion, desactivar_programacion  # noqa: E402
from apps.catalogo.visibilidad import servicios_visibles_para  # noqa: E402
from apps.tickets import periodos  # noqa: E402
from apps.tickets.models import EjecucionProgramada  # noqa: E402
from apps.tickets.programadas import (  # noqa: E402
    generar_ejecucion_programada,
    periodos_omitidos,
    reconciliar_ejecuciones_programadas,
)
from django.test import SimpleTestCase  # noqa: E402


class PeriodosProgramadosTests(SimpleTestCase):
    """Módulo puro de periodos: sin base de datos ni reloj."""

    def test_mes_actual_y_mes_siguiente(self):
        actual = periodos.periodo_de_creacion(date(2026, 10, 25), periodos.MES_ACTUAL)
        siguiente = periodos.periodo_de_creacion(date(2026, 10, 25), periodos.MES_SIGUIENTE)
        self.assertEqual(tuple(actual), (date(2026, 10, 1), date(2026, 10, 31), "Octubre 2026"))
        self.assertEqual(tuple(siguiente), (date(2026, 11, 1), date(2026, 11, 30), "Noviembre 2026"))

    def test_diciembre_pasa_a_enero_del_anio_siguiente(self):
        periodo = periodos.periodo_de_creacion(date(2026, 12, 25), periodos.MES_SIGUIENTE)
        self.assertEqual(tuple(periodo), (date(2027, 1, 1), date(2027, 1, 31), "Enero 2027"))

    def test_febrero_y_anios_bisiestos(self):
        self.assertEqual(periodos.periodo_mensual(2027, 2).fin, date(2027, 2, 28))
        self.assertEqual(periodos.periodo_mensual(2028, 2).fin, date(2028, 2, 29))
        self.assertEqual(periodos.periodo_mensual(2100, 2).fin, date(2100, 2, 28))  # no bisiesto
        self.assertEqual(periodos.periodo_de_creacion(date(2028, 1, 28), periodos.MES_SIGUIENTE).etiqueta, "Febrero 2028")

    def test_el_dia_de_creacion_es_de_1_a_28(self):
        for valido in (1, 15, 28):
            self.assertEqual(periodos.validar_dia(valido), valido)
        for invalido in (0, 29, 31, -1, "5", None, True, 2.0):
            with self.subTest(dia=invalido), self.assertRaises(ValueError):
                periodos.validar_dia(invalido)

    def test_la_creacion_vencida_mas_reciente_no_depende_de_que_hoy_sea_el_dia(self):
        self.assertEqual(periodos.creacion_vencida_mas_reciente(date(2026, 10, 25), 25), date(2026, 10, 25))
        self.assertEqual(periodos.creacion_vencida_mas_reciente(date(2026, 10, 26), 25), date(2026, 10, 25))
        self.assertEqual(periodos.creacion_vencida_mas_reciente(date(2026, 10, 24), 25), date(2026, 9, 25))
        self.assertEqual(periodos.creacion_vencida_mas_reciente(date(2027, 1, 10), 25), date(2026, 12, 25))

    def test_creaciones_entre_lista_las_fechas_de_un_rango(self):
        fechas = periodos.creaciones_entre(date(2026, 1, 1), date(2026, 4, 1), 1)
        self.assertEqual(fechas, [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)])
        self.assertEqual(periodos.creaciones_entre(date(2026, 1, 26), date(2026, 2, 24), 25), [])

    def test_periodo_desconocido_falla(self):
        with self.assertRaises(ValueError):
            periodos.periodo_de_creacion(date(2026, 1, 1), "SEMANA")

    def test_la_siguiente_creacion_es_siempre_posterior_a_hoy(self):
        self.assertEqual(periodos.creacion_siguiente(date(2026, 10, 7), 25), date(2026, 10, 25))
        self.assertEqual(periodos.creacion_siguiente(date(2026, 10, 25), 25), date(2026, 11, 25))
        self.assertEqual(periodos.creacion_siguiente(date(2026, 12, 30), 1), date(2027, 1, 1))


class _EscenarioProgramadoMixin(_EscenarioTrabajoMixin):
    """Procesos sobre la plantilla de cinco fases de 4.E2, con un Equipo Analítica como
    responsable inicial por defecto."""

    def _preparar_programado(self):
        self._preparar_e2()
        self.analitica = Equipo.objects.create(nombre="Equipo Analítica")
        self.analista = Usuario.objects.create_user(username="g1_analista", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=self.analitica, usuario=self.analista)
        _otorgar_tickets_atender(self.analista)

    def _proceso(
        self, nombre="Informe mensual de indicadores", *, flujo="informe", campos=None, politica=None,
        responsable_usuario=False,
    ):
        """Proceso PUBLICADO con su responsable (equipo Analítica, o el usuario analista).
        `flujo`: "informe" (Preparar → Aprobación → Entregable → Cierre), "simple" (una actividad del
        responsable del ticket), "solicitante" (actividad dirigida al solicitante) o
        "aprobacion_solicitante". Devuelve `(servicio, servicio_responsable)`."""
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion

        servicio, _, _ = _crear_servicio_con_formulario(self.admin, campos or [])
        servicio.nombre = nombre
        servicio.tipo = "PROCESO"
        servicio.workflow = self.workflow_e2
        servicio.save(update_fields=["nombre", "tipo", "workflow", "actualizado_en"])
        if politica:
            Servicio.objects.filter(pk=servicio.pk).update(politica_entrega=politica, dias_observacion=None)
        if responsable_usuario:
            responsable = ServicioResponsable.objects.create(
                servicio=servicio, tipo_responsable="USUARIO", usuario=self.analista
            )
        else:
            responsable = ServicioResponsable.objects.create(
                servicio=servicio, tipo_responsable="EQUIPO", equipo=self.analitica
            )
        ACT = BloqueOperativo.Tipo.ACTIVIDAD
        config = crear_nueva_version_configuracion(servicio, self.admin)
        if flujo == "informe":
            definicion = DefinicionEntregable.objects.create(
                servicio=servicio, nombre="Informe de indicadores", tipo="TEXTO", obligatorio=True, orden=0
            )
            self._agregar(config, self.f_recepcion, ACT, "Preparar informe", tipo_actor="RESPONSABLE_TICKET")
            aprobacion = self._apr(config, self.f_revision, "Aprobar informe")
            entrega = agregar_bloque_operativo(
                config, self.admin, fase=self.f_entrega, tipo=BloqueOperativo.Tipo.ENTREGABLE,
                nombre="Entregar informe", definicion_entregable=definicion,
            )
            self._agregar(config, self.f_entrega, ACT, "Cierre", tipo_actor="RESPONSABLE_TICKET")
            self._ruta(aprobacion, entrega, resultado_aprobacion="APROBADA")
            self._ruta(aprobacion, None, resultado_aprobacion="DEVUELTA")
            self._ruta(aprobacion, None, resultado_aprobacion="RECHAZADA")
        elif flujo == "simple":
            self._agregar(config, self.f_recepcion, ACT, "Atender", tipo_actor="RESPONSABLE_TICKET")
        elif flujo == "solicitante":
            self._agregar(config, self.f_recepcion, ACT, "Confirmar con quien solicitó", tipo_actor="SOLICITANTE")
        elif flujo == "aprobacion_solicitante":
            aprobacion = agregar_bloque_operativo(
                config, self.admin, fase=self.f_recepcion, tipo=BloqueOperativo.Tipo.APROBACION,
                nombre="Visto bueno del solicitante",
                configuracion={
                    "modo": "SECUENCIAL", "politica": "",
                    "participantes": [{"tipo": "SOLICITANTE", "usuario_id": None, "equipo_id": None}],
                },
            )
            for resultado in ("APROBADA", "DEVUELTA", "RECHAZADA"):
                self._ruta(aprobacion, None, resultado_aprobacion=resultado)
        self._activar(servicio, config)
        return servicio, responsable

    def _programar(self, servicio, responsable, *, dia=1, periodo="MES_ACTUAL", desde=date(2026, 1, 1)):
        """Deja el Proceso programado y fija `activada_desde` (la fecha de activación real depende
        del reloj; las pruebas necesitan una fecha conocida)."""
        programacion = configurar_programacion(
            servicio, self.admin, dia_creacion=dia, periodo=periodo, responsable_inicial=responsable
        )
        ProgramacionProceso.objects.filter(pk=programacion.pk).update(activada_desde=desde)
        programacion.refresh_from_db()
        return programacion

    def _ejecucion(self, servicio):
        return EjecucionProgramada.objects.get(servicio=servicio)

    def _completar_como(self, ticket, nombre, usuario):
        from apps.workflows.integracion import completar_tarea_workflow
        from apps.workflows.models import TareaWorkflow

        ticket.refresh_from_db()
        ejecucion = (
            ticket.instancia_workflow.ejecuciones_etapa.filter(bloque_operativo__nombre=nombre).order_by("-orden").first()
        )
        completar_tarea_workflow(TareaWorkflow.objects.get(instancia_etapa=ejecucion).tarea, usuario)


class ProgramacionProcesoModeloTests(_EscenarioProgramadoMixin, TestCase):
    def setUp(self):
        self._preparar_programado()

    def test_solo_un_proceso_puede_programarse(self):
        servicio, _, _ = _crear_servicio_con_formulario(self.admin, [])  # tipo SERVICIO
        with self.assertRaises(ValidationError):
            ProgramacionProceso(servicio=servicio, activa=False, dia_creacion=5).save()
        with self.assertRaises(ValidationError):
            configurar_programacion(
                servicio, self.admin, dia_creacion=5, periodo="MES_ACTUAL", responsable_inicial=None
            )
        self.assertFalse(ProgramacionProceso.objects.exists())

    def test_el_dia_va_de_1_a_28_en_el_dominio_y_en_la_base_de_datos(self):
        servicio, responsable = self._proceso(flujo="simple")
        for invalido in (0, 29, 31):
            with self.subTest(dia=invalido):
                with self.assertRaises(ValidationError):
                    configurar_programacion(
                        servicio, self.admin, dia_creacion=invalido, periodo="MES_ACTUAL", responsable_inicial=responsable
                    )
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ProgramacionProceso.objects.bulk_create(
                        [ProgramacionProceso(servicio=servicio, activa=False, dia_creacion=invalido)]
                    )

    def test_una_programacion_activa_exige_responsable_y_fecha_de_activacion(self):
        servicio, _ = self._proceso(flujo="simple")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProgramacionProceso.objects.bulk_create([ProgramacionProceso(servicio=servicio, activa=True, dia_creacion=5)])

    def test_el_responsable_inicial_debe_ser_de_este_proceso(self):
        servicio, _ = self._proceso(flujo="simple")
        _, ajeno = self._proceso("Otro proceso", flujo="simple")
        with self.assertRaises(ValidationError):
            configurar_programacion(
                servicio, self.admin, dia_creacion=5, periodo="MES_ACTUAL", responsable_inicial=ajeno
            )
        with self.assertRaises(ValidationError):
            ProgramacionProceso(servicio=servicio, activa=False, dia_creacion=5, responsable_inicial=ajeno).save()

    def test_solo_quien_administra_el_catalogo_programa(self):
        servicio, responsable = self._proceso(flujo="simple")
        with self.assertRaises(PermissionDenied):
            configurar_programacion(
                servicio, self.solicitante, dia_creacion=5, periodo="MES_ACTUAL", responsable_inicial=responsable
            )

    def test_configurar_audita_y_cambiar_el_calendario_reinicia_la_fecha_de_activacion(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 1, 1))
        self.assertTrue(
            RegistroAuditoria.objects.filter(modelo="catalogo.programacionproceso", object_id=programacion.pk).exists()
        )
        # Cambiar solo el responsable no reinicia; cambiar el día sí (nunca genera hacia atrás).
        configurar_programacion(servicio, self.admin, dia_creacion=25, periodo="MES_SIGUIENTE", responsable_inicial=responsable)
        programacion.refresh_from_db()
        self.assertEqual(programacion.activada_desde, date(2026, 1, 1))
        configurar_programacion(servicio, self.admin, dia_creacion=10, periodo="MES_SIGUIENTE", responsable_inicial=responsable)
        programacion.refresh_from_db()
        self.assertGreater(programacion.activada_desde, date(2026, 1, 1))

    def test_manual_pausa_sin_perder_la_configuracion(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable, dia=7)
        desactivar_programacion(servicio, self.admin)
        programacion.refresh_from_db()
        self.assertFalse(programacion.activa)
        self.assertEqual(programacion.dia_creacion, 7)
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 3, 8))["generadas"], 0)


class TicketProgramadoModeloTests(_EscenarioProgramadoMixin, TestCase):
    def setUp(self):
        self._preparar_programado()

    def test_un_ticket_manual_no_puede_quedar_sin_solicitante(self):
        for origen in ("MANUAL", "SISTEMA"):
            with self.subTest(origen=origen), self.assertRaises(IntegrityError), transaction.atomic():
                Ticket.objects.create(solicitante=None, tipo="PROCESO", origen=origen)

    def test_un_ticket_de_programacion_no_puede_tener_solicitante(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.create(solicitante=self.solicitante, tipo="PROCESO", origen="PROGRAMACION")

    def test_un_ticket_de_programacion_es_siempre_un_proceso(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.create(solicitante=None, tipo="SERVICIO", origen="PROGRAMACION")

    def test_un_ticket_de_programacion_sin_solicitante_es_valido(self):
        ticket = Ticket.objects.create(solicitante=None, tipo="PROCESO", origen="PROGRAMACION")
        self.assertIsNone(ticket.solicitante_id)
        self.assertFalse(es_propietario_borrador(self.solicitante, ticket))


class ProcesoProgramadoFueraDelCatalogoTests(_EscenarioProgramadoMixin, TestCase):
    def setUp(self):
        self._preparar_programado()
        self.servicio, self.resp_servicio = self._proceso(flujo="simple")

    def test_un_proceso_manual_sigue_en_el_catalogo(self):
        self.assertIn(self.servicio.pk, [s.pk for s in servicios_visibles_para(self.solicitante)])

    def test_un_proceso_programado_activo_sale_de_toda_vista_basada_en_visibles(self):
        from apps.catalogo.busqueda import buscar_servicios_por_necesidad, servicios_buscables_para
        from apps.core import inicio

        self.assertIn(self.servicio.pk, [s.pk for s in servicios_buscables_para(self.solicitante)])
        self.assertEqual(len(inicio.categorias_con_servicios(self.solicitante)), 1)
        self._programar(self.servicio, self.resp_servicio)
        self.assertNotIn(self.servicio.pk, [s.pk for s in servicios_visibles_para(self.solicitante)])
        self.assertNotIn(self.servicio.pk, [s.pk for s in servicios_buscables_para(self.solicitante)])
        self.assertEqual(inicio.categorias_con_servicios(self.solicitante), [])
        resultados, _ = inicio.buscar_servicios(self.solicitante, "Informe")
        self.assertNotIn(self.servicio.pk, [s.pk for s in resultados])
        encontrados = buscar_servicios_por_necesidad(self.solicitante, "informe mensual indicadores")
        self.assertNotIn(self.servicio.pk, [r.servicio.pk for r in encontrados])
        with self.assertRaises(PermissionDenied):
            crear_borrador(self.solicitante, self.servicio)
        self._entrar(self.solicitante)
        self.assertEqual(self.client.get(reverse("tickets:solicitar", args=[self.servicio.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("catalogo:detalle", args=[self.servicio.pk])).status_code, 404)

    def test_pausar_la_programacion_devuelve_el_proceso_al_catalogo(self):
        self._programar(self.servicio, self.resp_servicio)
        desactivar_programacion(self.servicio, self.admin)
        self.assertIn(self.servicio.pk, [s.pk for s in servicios_visibles_para(self.solicitante)])

    def test_un_proceso_manual_se_sigue_solicitando_con_origen_manual_y_solicitante(self):
        ticket = crear_borrador(self.solicitante, self.servicio)
        radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual((ticket.tipo, ticket.origen, ticket.etiqueta), ("PROCESO", "MANUAL", ""))
        self.assertEqual(ticket.solicitante, self.solicitante)
        evento = ticket.historial.get(tipo_evento="RADICADO")
        self.assertEqual(evento.actor, self.solicitante)
        self.assertIsNone(evento.datos)
        self.assertFalse(EjecucionProgramada.objects.exists())

    def test_un_servicio_sigue_radicando_con_su_solicitante(self):
        servicio, _, _ = _crear_servicio_con_formulario(self.solicitante, [])
        ticket = crear_borrador(self.solicitante, servicio)
        radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual((ticket.tipo, ticket.origen, ticket.solicitante), ("SERVICIO", "MANUAL", self.solicitante))
        self.assertEqual(ticket.estado, "RADICADO")


class CompatibilidadDeProcesoProgramadoTests(_EscenarioProgramadoMixin, TestCase):
    def setUp(self):
        self._preparar_programado()

    def _intentar(self, servicio, responsable):
        return configurar_programacion(
            servicio, self.admin, dia_creacion=5, periodo="MES_ACTUAL", responsable_inicial=responsable
        )

    def _rechaza(self, servicio, responsable, fragmento):
        with self.assertRaises(ValidationError) as contexto:
            self._intentar(servicio, responsable)
        self.assertIn(fragmento, " ".join(contexto.exception.messages))
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=servicio).exists())

    def test_una_actividad_dirigida_al_solicitante_bloquea_la_activacion_y_dice_cual(self):
        servicio, responsable = self._proceso(flujo="solicitante")
        self._rechaza(servicio, responsable, "Confirmar con quien solicitó")

    def test_una_aprobacion_con_el_solicitante_como_participante_bloquea(self):
        servicio, responsable = self._proceso(flujo="aprobacion_solicitante")
        self._rechaza(servicio, responsable, "Visto bueno del solicitante")

    def test_la_entrega_formal_bloquea(self):
        servicio, responsable = self._proceso(flujo="simple", politica=DIRECTO)
        self._rechaza(servicio, responsable, "entrega formal")

    def test_un_formulario_con_campos_obligatorios_bloquea(self):
        T = Campo.TipoCampo
        servicio, responsable = self._proceso(
            flujo="simple", campos=[{"tipo": T.TEXTO, "etiqueta": "Motivo", "orden": 1, "obligatorio": True}]
        )
        self._rechaza(servicio, responsable, "Motivo")

    def test_un_proceso_compatible_se_programa(self):
        servicio, responsable = self._proceso(flujo="informe")
        self.assertTrue(self._intentar(servicio, responsable).activa)

    def test_publicar_un_flujo_con_actor_solicitante_se_rechaza_si_el_proceso_esta_programado(self):
        from apps.catalogo.configuracion_ejecucion import activar_configuracion_ejecucion, crear_nueva_version_configuracion

        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        config = crear_nueva_version_configuracion(servicio, self.admin)
        self._agregar(config, self.f_recepcion, BloqueOperativo.Tipo.ACTIVIDAD, "Pedir dato", tipo_actor="SOLICITANTE")
        with self.assertRaises(ValidationError) as contexto:
            activar_configuracion_ejecucion(servicio, config, self.admin)
        self.assertIn("Pedir dato", " ".join(contexto.exception.messages))

    def test_no_se_puede_definir_entrega_formal_ni_pasar_a_servicio_mientras_este_programado(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        with self.assertRaises(ValidationError):
            configurar_politica_entrega(servicio, self.admin, politica="CIERRE_DIRECTO")
        with self.assertRaises(ValidationError):
            editar_servicio_general(
                servicio, self.admin, nombre=servicio.nombre, descripcion="", categoria=servicio.categoria,
                tipo="SERVICIO", instrucciones="", alcance_visibilidad=servicio.alcance_visibilidad,
            )

    def test_no_se_activa_una_version_de_formulario_con_obligatorios_en_un_proceso_programado(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        nueva = crear_nueva_version(servicio.formulario, self.admin)
        Campo.objects.create(version=nueva, tipo=Campo.TipoCampo.TEXTO, etiqueta="Obligatorio", obligatorio=True, orden=1)
        with self.assertRaises(ValueError):
            activar_version(servicio.formulario, nueva, self.admin)

    def test_el_diagnostico_de_publicacion_senala_un_proceso_programado_incompatible(self):
        from apps.catalogo.operaciones import validar_publicacion

        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable)
        Servicio.objects.filter(pk=servicio.pk).update(politica_entrega=DIRECTO)  # saltando la operación
        servicio.refresh_from_db()
        with self.assertRaises(ValidationError) as contexto:
            validar_publicacion(servicio)
        self.assertIn("entrega formal", " ".join(contexto.exception.messages))
        # Y la generación automática lo rechaza en vez de crear un ticket que no se puede cerrar bien.
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 3, 2))["con_error"], 1)
        programacion.refresh_from_db()
        self.assertIn("entrega formal", programacion.ultimo_error)


class GenerarEjecucionProgramadaTests(_EscenarioProgramadoMixin, TestCase):
    """Casos E2E de 4.G1."""

    def setUp(self):
        self._preparar_programado()

    # --- caso principal ---------------------------------------------------------------

    def test_informe_mensual_dia_1_mes_actual_genera_el_ticket_y_recorre_el_circuito(self):
        servicio, responsable = self._proceso("Informe mensual de indicadores", flujo="informe")
        self._programar(servicio, responsable, dia=1, periodo="MES_ACTUAL", desde=date(2026, 9, 15))

        resumen = reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        self.assertEqual(resumen, {"generadas": 1, "con_error": 0, "omitidos": 0})

        ejecucion = self._ejecucion(servicio)
        ticket = ejecucion.ticket
        ticket.refresh_from_db()
        self.assertEqual((ticket.tipo, ticket.origen), ("PROCESO", "PROGRAMACION"))
        self.assertIsNone(ticket.solicitante_id)
        self.assertEqual(ticket.etiqueta, "Octubre 2026")
        self.assertEqual((ejecucion.periodo_inicio, ejecucion.periodo_fin), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual((ejecucion.etiqueta, ejecucion.frecuencia), ("Octubre 2026", "MENSUAL"))
        self.assertTrue(ticket.codigo.startswith("TCK-"))
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)
        self.assertEqual(ticket.detalle_servicio.servicio, servicio)
        self.assertEqual(ticket.entrega_politica, "")
        self.assertIsNotNone(ticket.instancia_workflow_id)
        self.assertEqual(ticket.instancia_workflow.estado, "EN_ESPERA")
        self.assertEqual((ticket.equipo_responsable, ticket.usuario_responsable_id), (self.analitica, None))
        self.assertEqual(ticket.entregables.count(), 1)
        self.assertEqual(ejecucion.programacion_foto["dia_creacion"], 1)
        self.assertEqual(ejecucion.programacion_foto["responsable_inicial"]["equipo_id"], self.analitica.pk)

        # Historial como SISTEMA y auditoría sin usuario.
        radicado = ticket.historial.get(tipo_evento="RADICADO")
        self.assertIsNone(radicado.actor_id)
        self.assertEqual((radicado.datos["origen"], radicado.datos["etiqueta"]), ("PROGRAMACION", "Octubre 2026"))
        auditoria = RegistroAuditoria.objects.get(modelo="tickets.ejecucionprogramada", object_id=ejecucion.pk)
        self.assertEqual((auditoria.origen, auditoria.usuario_id), ("SISTEMA", None))
        self.assertTrue(
            _auditorias_de_ticket(ticket).filter(origen="SISTEMA", usuario__isnull=True).exists()
        )

        # No aparece en Mis tickets de nadie; sí en la Cola del equipo.
        for persona in (self.analista, self.admin, self.solicitante):
            self._entrar(persona)
            self.assertNotIn(ticket, self.client.get(reverse("tickets:mis_tickets")).context["tickets"])
        self._entrar(self.analista)
        self.assertIn(ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        cola = self.client.get(reverse("tickets:cola"))
        self.assertContains(cola, "Octubre 2026")
        self.assertContains(cola, "Generado por programación")

        # Pantallas de lectura sin solicitante.
        self.assertEqual(self.client.get(reverse("tickets:detalle", args=[ticket.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tickets:resumen", args=[ticket.pk])).status_code, 200)
        self.assertRedirects(
            self.client.get(reverse("tickets:seguimiento", args=[ticket.pk])),
            reverse("tickets:detalle", args=[ticket.pk]), fetch_redirect_response=False,
        )

        # Un miembro autorizado lo toma y pasa a Mi trabajo.
        self.client.post(reverse("tickets:tomar", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual((ticket.estado, ticket.usuario_responsable), (Ticket.Estado.EN_ATENCION, self.analista))
        tarjetas = self.client.get(reverse("core:mi_trabajo")).context["tarjetas"]
        self.assertEqual([t["ticket"].pk for t in tarjetas], [ticket.pk])
        self.assertContains(self.client.get(reverse("core:mi_trabajo")), "Octubre 2026")
        self.assertEqual(self.client.get(reverse("tickets:trabajo", args=[ticket.pk])).status_code, 200)

        # No hay a quién pedirle información ni entregarle formalmente.
        from apps.tickets.autorizacion import puede_entregar_ticket

        self.assertFalse(puede_solicitar_informacion(self.analista, ticket))
        self.assertFalse(puede_entregar_ticket(self.analista, ticket))

        # Workflow normal: preparar → aprobar → entregable → cierre.
        self._completar_como(ticket, "Preparar informe", self.analista)
        self._revisar(ticket, "APROBADA", nombre="Aprobar informe")
        entregable = ticket.entregables.get()
        registrar_resultado_entregable(entregable, self.analista, "Informe de octubre listo")
        self._completar_como(ticket, "Cierre", self.analista)
        ticket.instancia_workflow.refresh_from_db()
        self.assertEqual(ticket.instancia_workflow.estado, "COMPLETADA")
        # Completar el Workflow no cierra el Ticket: se resuelve y cierra por las operaciones de siempre.
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        resolver_ticket(ticket, self.analista, "Informe publicado")
        cerrar_ticket(ticket, self.analista)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.CERRADO)

    def test_el_nombre_del_proceso_no_se_toca_y_la_etiqueta_se_congela(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        ticket = self._ejecucion(servicio).ticket
        self.assertEqual(ticket.detalle_servicio.servicio.nombre, "Informe mensual de indicadores")
        ticket.etiqueta = "Otra"
        with self.assertRaises(ValidationError):
            ticket.save()
        ejecucion = self._ejecucion(servicio)
        ejecucion.etiqueta = "Otra"
        with self.assertRaises(ValidationError):
            ejecucion.save()

    # --- segundo caso: mes siguiente y no duplicar ---------------------------------------

    def test_revision_mensual_dia_25_mes_siguiente_crea_enero_y_no_se_duplica(self):
        servicio, responsable = self._proceso("Revisión mensual de indicadores", flujo="simple")
        self._programar(servicio, responsable, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 12, 1))

        hoy = date(2026, 12, 25)
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=hoy)["generadas"], 1)
        ejecucion = self._ejecucion(servicio)
        self.assertEqual((ejecucion.periodo_inicio, ejecucion.periodo_fin), (date(2027, 1, 1), date(2027, 1, 31)))
        self.assertEqual(ejecucion.ticket.etiqueta, "Enero 2027")

        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=hoy)["generadas"], 0)
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 12, 31))["generadas"], 0)
        self.assertEqual(EjecucionProgramada.objects.count(), 1)
        self.assertEqual(Ticket.objects.count(), 1)

    def test_pausar_cambiar_el_responsable_y_reactivar_no_duplican_el_periodo(self):
        servicio, responsable = self._proceso(flujo="simple")
        otro = ServicioResponsable.objects.create(servicio=servicio, tipo_responsable="USUARIO", usuario=self.analista)
        self._programar(servicio, responsable, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 12, 1))
        reconciliar_ejecuciones_programadas(hoy=date(2026, 12, 25))
        desactivar_programacion(servicio, self.admin)
        configurar_programacion(servicio, self.admin, dia_creacion=25, periodo="MES_SIGUIENTE", responsable_inicial=otro)
        ProgramacionProceso.objects.filter(servicio=servicio).update(activada_desde=date(2026, 12, 1))
        reconciliar_ejecuciones_programadas(hoy=date(2026, 12, 28))
        self.assertEqual(EjecucionProgramada.objects.filter(servicio=servicio).count(), 1)

    def test_la_base_de_datos_impide_dos_ejecuciones_del_mismo_proceso_y_periodo(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        original = self._ejecucion(servicio)
        otro_ticket = Ticket.objects.create(solicitante=None, tipo="PROCESO", origen="PROGRAMACION", etiqueta="Octubre 2026")
        with self.assertRaises(IntegrityError), transaction.atomic():
            EjecucionProgramada.objects.create(
                servicio=servicio, frecuencia="MENSUAL", periodo_inicio=original.periodo_inicio,
                periodo_fin=original.periodo_fin, etiqueta="Octubre 2026", ticket=otro_ticket,
                generada_en=timezone.now(),
            )

    # --- recuperación, retroactividad y acumulación --------------------------------------

    def test_si_el_beat_no_corrio_el_dia_25_la_ejecucion_se_recupera_el_26(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 10, 1))
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 24))["generadas"], 0)  # aún no vence
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 26))["generadas"], 1)
        ejecucion = self._ejecucion(servicio)
        self.assertEqual(ejecucion.etiqueta, "Noviembre 2026")
        self.assertEqual(EjecucionProgramada.objects.count(), 1)

    def test_no_es_retroactivo_la_primera_ejecucion_es_el_siguiente_vencimiento_real(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 10, 7))
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 7))["generadas"], 0)
        # Configurada el 26: el vencimiento del 25 ya pasó y no se rellena.
        ProgramacionProceso.objects.filter(servicio=servicio).update(activada_desde=date(2026, 10, 26))
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 27))["generadas"], 0)
        self.assertFalse(EjecucionProgramada.objects.exists())
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 11, 25))["generadas"], 1)
        self.assertEqual(self._ejecucion(servicio).etiqueta, "Diciembre 2026")

    def test_si_el_beat_estuvo_apagado_meses_no_se_acumulan_ejecuciones_en_silencio(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable, dia=1, periodo="MES_ACTUAL", desde=date(2026, 1, 1))
        hoy = date(2026, 7, 1)
        resumen = reconciliar_ejecuciones_programadas(hoy=hoy)
        self.assertEqual((resumen["generadas"], resumen["omitidos"]), (1, 6))
        self.assertEqual(EjecucionProgramada.objects.count(), 1)
        self.assertEqual(self._ejecucion(servicio).etiqueta, "Julio 2026")
        # Los periodos que faltaron se pueden detectar, no se rellenan.
        self.assertEqual(
            [p.etiqueta for p in periodos_omitidos(programacion, hoy)],
            ["Enero 2026", "Febrero 2026", "Marzo 2026", "Abril 2026", "Mayo 2026", "Junio 2026"],
        )
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=hoy)["generadas"], 0)

    # --- responsable inicial -------------------------------------------------------------

    def test_responsable_inicial_usuario_deja_el_ticket_dirigido_sin_falsear_la_atencion(self):
        servicio, responsable = self._proceso(flujo="informe", responsable_usuario=True)
        self._programar(servicio, responsable, dia=1, periodo="MES_ACTUAL")
        reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        ticket = self._ejecucion(servicio).ticket
        ticket.refresh_from_db()
        self.assertEqual((ticket.usuario_responsable, ticket.equipo_responsable_id), (self.analista, None))
        self.assertEqual(ticket.estado, Ticket.Estado.RADICADO)  # dirigido NO es en atención
        self.assertTrue(puede_iniciar_atencion(self.analista, ticket))
        self.assertFalse(puede_tomar(self.analista, ticket))
        # La actividad «para el responsable del ticket» ya nace con dueño.
        from apps.workflows.models import TareaWorkflow

        tarea = TareaWorkflow.objects.get(instancia_etapa=self._ultima(ticket)).tarea
        self.assertEqual(tarea.usuario_responsable_id, self.analista.pk)
        self._entrar(self.analista)
        self.assertIn(ticket, self.client.get(reverse("tickets:cola")).context["tickets"])
        self.assertEqual(self.client.get(reverse("core:mi_trabajo")).context["tarjetas"], [])
        iniciar_atencion_ticket(ticket, self.analista)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, Ticket.Estado.EN_ATENCION)
        self.assertEqual([t["ticket"].pk for t in self.client.get(reverse("core:mi_trabajo")).context["tarjetas"]], [ticket.pk])

    def test_responsable_inicial_equipo_lo_deja_en_la_cola_de_sus_miembros(self):
        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        ticket = self._ejecucion(servicio).ticket
        self.assertEqual((ticket.equipo_responsable, ticket.usuario_responsable_id), (self.analitica, None))
        self.assertTrue(puede_tomar(self.analista, ticket))
        ajeno = Usuario.objects.create_user(username="g1_ajeno", password=CLAVE_PRUEBA)
        self.assertFalse(puede_tomar(ajeno, ticket))
        self.assertFalse(puede_tomar(self.admin, ticket))  # administrar el catálogo no es atender

    # --- errores -------------------------------------------------------------------------

    def test_un_responsable_inactivo_no_genera_un_ticket_huerfano_y_deja_el_error(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable)
        retirar_responsable(responsable, self.admin)

        resumen = reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        self.assertEqual((resumen["generadas"], resumen["con_error"]), (0, 1))
        self.assertFalse(Ticket.objects.exists())
        self.assertFalse(EjecucionProgramada.objects.exists())
        programacion.refresh_from_db()
        self.assertIsNotNone(programacion.ultimo_intento_en)
        self.assertIn("responsable inicial", programacion.ultimo_error)

    def test_un_usuario_responsable_desactivado_tampoco_genera(self):
        servicio, responsable = self._proceso(flujo="simple", responsable_usuario=True)
        programacion = self._programar(servicio, responsable)
        Usuario.objects.filter(pk=self.analista.pk).update(is_active=False)
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))["con_error"], 1)
        programacion.refresh_from_db()
        self.assertIn("ya no está activo", programacion.ultimo_error)
        self.assertFalse(Ticket.objects.exists())

    def test_un_error_en_un_proceso_no_impide_generar_los_demas_y_se_limpia_al_funcionar(self):
        malo, resp_malo = self._proceso("Proceso con responsable retirado", flujo="simple")
        bueno, resp_bueno = self._proceso("Proceso sano", flujo="simple")
        prog_malo = self._programar(malo, resp_malo)
        self._programar(bueno, resp_bueno)
        retirar_responsable(resp_malo, self.admin)

        resumen = reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        self.assertEqual((resumen["generadas"], resumen["con_error"]), (1, 1))
        self.assertEqual(EjecucionProgramada.objects.get().servicio, bueno)
        prog_malo.refresh_from_db()
        self.assertTrue(prog_malo.ultimo_error)

        # Se corrige (otro responsable activo) y la siguiente corrida genera y limpia el error.
        nuevo = ServicioResponsable.objects.create(servicio=malo, tipo_responsable="EQUIPO", equipo=self.analitica)
        ProgramacionProceso.objects.filter(pk=prog_malo.pk).update(responsable_inicial=nuevo)
        resumen = reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 2))
        self.assertEqual((resumen["generadas"], resumen["con_error"]), (1, 0))
        prog_malo.refresh_from_db()
        self.assertEqual(prog_malo.ultimo_error, "")
        self.assertEqual(EjecucionProgramada.objects.count(), 2)

    def test_un_flujo_incompatible_descubierto_al_generar_queda_registrado_en_la_programacion(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable)
        # Alguien deja el flujo dirigido al solicitante saltándose la validación de publicación.
        bloque = servicio.configuracion_ejecucion_activa.bloques.get()
        BloqueOperativo.objects.filter(pk=bloque.pk).update(configuracion={"tipo_actor": "SOLICITANTE"})
        self.assertEqual(reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))["con_error"], 1)
        programacion.refresh_from_db()
        self.assertIn("solicitante", programacion.ultimo_error)
        self.assertFalse(Ticket.objects.exists())

    def test_si_algo_falla_al_crear_no_queda_nada_ni_se_gasta_el_consecutivo(self):
        from unittest.mock import patch

        from apps.tickets.models import ConsecutivoTicket

        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable)
        with patch("apps.tickets.programadas.EjecucionProgramada.objects.create", side_effect=RuntimeError("falla")):
            with self.assertRaises(RuntimeError):
                generar_ejecucion_programada(programacion, hoy=date(2026, 10, 1))
        self.assertFalse(Ticket.objects.exists())
        self.assertFalse(EjecucionProgramada.objects.exists())
        self.assertEqual(ConsecutivoTicket.objects.filter(ultimo__gt=0).count(), 0)
        # Sin el fallo, la misma operación genera el ticket y el consecutivo sigue desde el principio.
        self.assertEqual(generar_ejecucion_programada(programacion, hoy=date(2026, 10, 1)).ticket.consecutivo, 1)

    def test_una_programacion_pausada_no_genera(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable)
        desactivar_programacion(servicio, self.admin)
        self.assertIsNone(generar_ejecucion_programada(programacion, hoy=date(2026, 10, 1)))
        self.assertFalse(Ticket.objects.exists())

    def test_las_variables_del_workflow_ven_el_origen_y_un_solicitante_vacio(self):
        from apps.workflows.variables import _CAMPOS_TICKET

        servicio, responsable = self._proceso(flujo="simple")
        self._programar(servicio, responsable)
        reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        ticket = self._ejecucion(servicio).ticket
        self.assertEqual(_CAMPOS_TICKET["origen"](ticket), "PROGRAMACION")
        self.assertIsNone(_CAMPOS_TICKET["solicitante"](ticket))


class ConcurrenciaEjecucionProgramadaTests(_EscenarioProgramadoMixin, TransactionTestCase):
    """Bloqueante: dos workers que generan el mismo periodo producen UN solo ticket."""

    def setUp(self):
        self._preparar_programado()

    def test_dos_workers_generando_el_mismo_periodo_crean_una_sola_ejecucion(self):
        servicio, responsable = self._proceso(flujo="simple")
        programacion = self._programar(servicio, responsable, dia=1, periodo="MES_ACTUAL")
        barrera = threading.Barrier(2)
        resultados = []

        def trabajar():
            try:
                barrera.wait(timeout=10)
                ejecucion = generar_ejecucion_programada(programacion, hoy=date(2026, 10, 1))
                resultados.append("generada" if ejecucion is not None else "nada")
            except Exception as exc:  # noqa: BLE001
                resultados.append(exc)
            finally:
                connection.close()

        hilos = [threading.Thread(target=trabajar, daemon=True) for _ in range(2)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(timeout=30)
            self.assertFalse(hilo.is_alive())
        self.assertCountEqual(resultados, ["generada", "nada"])
        self.assertEqual(EjecucionProgramada.objects.count(), 1)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Ticket.objects.get().historial.filter(tipo_evento="RADICADO").count(), 1)


class StudioInicioProcesoTests(_EscenarioProgramadoMixin, TestCase):
    """Tipo = Proceso → «¿Cómo inicia este proceso?» (Manual / Programado), dentro de las mismas
    pantallas de crear y de Básico. Programado NO es una categoría aparte del Diseñador."""

    def setUp(self):
        self._preparar_programado()
        self.servicio, self.resp_servicio = self._proceso(flujo="informe")

    def _datos_generales(self, servicio=None, **extra):
        servicio = servicio or self.servicio
        datos = {
            "nombre": servicio.nombre, "descripcion": servicio.descripcion or "", "categoria": servicio.categoria_id,
            "tipo": servicio.tipo, "instrucciones": servicio.instrucciones or "",
            "alcance_visibilidad": servicio.alcance_visibilidad,
        }
        datos.update(extra)
        return datos

    def _guardar(self, servicio=None, **inicio):
        servicio = servicio or self.servicio
        return self.client.post(
            reverse("catalogo:studio_general_guardar", args=[servicio.pk]), self._datos_generales(servicio, **inicio)
        )

    def _programado(self, **extra):
        datos = {
            "modo": "PROGRAMADO", "frecuencia": "MENSUAL", "dia_creacion": "25", "periodo": "MES_SIGUIENTE",
            "responsable_inicial": f"E:{self.analitica.pk}",
        }
        datos.update(extra)
        return datos

    def _mensajes(self, respuesta):
        return [str(m) for m in get_messages(respuesta.wsgi_request)]

    # --- qué se muestra según el Tipo ---

    def test_un_proceso_muestra_inicio_manual_programado_y_un_servicio_lo_trae_oculto(self):
        self._entrar(self.admin)
        pagina = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertContains(pagina, 'id="inicio-proceso"')
        self.assertContains(pagina, "¿Cómo inicia este proceso?")
        self.assertContains(pagina, "Manual")
        self.assertContains(pagina, "Programado")
        self.assertNotContains(pagina, 'id="inicio-proceso" data-solo-proceso hidden')
        servicio_simple, _, _ = _crear_servicio_con_formulario(self.admin, [])
        pagina = self.client.get(reverse("catalogo:studio", args=[servicio_simple.pk]))
        self.assertContains(pagina, 'id="inicio-proceso" data-solo-proceso hidden')
        self.assertContains(pagina, "data-solo-proceso")  # el JS lo muestra si el Tipo pasa a Proceso

    def test_proceso_manual_oculta_la_programacion_y_programado_la_muestra(self):
        self._entrar(self.admin)
        manual = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertContains(manual, "data-inicio-programado hidden")
        self._guardar(**self._programado())
        programado = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertNotContains(programado, "data-inicio-programado hidden")
        self.assertContains(programado, "Órbita creará automáticamente una nueva ejecución")
        self.assertContains(programado, "Crear ejecución el día")

    def test_editar_un_proceso_programado_precarga_su_configuracion(self):
        self._entrar(self.admin)
        self._guardar(**self._programado(dia_creacion="12", periodo="MES_ACTUAL"))
        pagina = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        inicial = pagina.context["form_inicio"].initial
        self.assertEqual(
            (inicial["modo"], inicial["dia_creacion"], inicial["periodo"], inicial["responsable_inicial"]),
            ("PROGRAMADO", 12, "MES_ACTUAL", f"E:{self.analitica.pk}"),
        )
        self.assertTrue(pagina.context["proceso_programado"])
        self.assertContains(pagina, 'value="12"')
        self.assertContains(pagina, "Próxima ejecución")

    def test_un_proceso_programado_no_muestra_lo_orientado_a_quien_solicita(self):
        self._entrar(self.admin)
        self._guardar(**self._programado())
        basico = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertContains(basico, 'svc-panel--visibility" data-oculto-si-programado hidden')
        self.assertContains(basico, 'id="terminos-busqueda" data-oculto-si-programado hidden')
        salida = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]) + "?tab=salida")
        self.assertNotContains(salida, "Entrega al solicitante")
        self.assertContains(salida, "no admite entrega formal")
        publicar = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]) + "?tab=publicacion")
        self.assertNotContains(publicar, "Qué verá el solicitante al cierre")
        # El mismo Proceso en modo Manual conserva todo lo de siempre.
        self._guardar(modo="MANUAL")
        salida = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]) + "?tab=salida")
        self.assertContains(salida, "Entrega al solicitante")

    def test_crear_trae_la_pregunta_de_inicio_dentro_del_mismo_formulario(self):
        self._entrar(self.admin)
        pagina = self.client.get(reverse("core:disenador_servicio_crear"))
        self.assertContains(pagina, "data-config-inicio")
        self.assertContains(pagina, "¿Cómo inicia este proceso?")
        self.assertContains(pagina, "Crear ejecución el día")
        self.assertContains(pagina, "Responsable inicial")
        self.assertContains(pagina, "data-solo-proceso hidden")  # Servicio es el tipo inicial

    # --- crear ---

    def _crear(self, **extra):
        datos = {"tipo": "PROCESO", "nombre": "Cierre contable", "categoria_nueva": "Finanzas G1"}
        datos.update(extra)
        return self.client.post(reverse("core:disenador_servicio_crear"), datos)

    def test_crear_un_proceso_programado_lo_deja_programado_con_su_responsable(self):
        self._entrar(self.admin)
        respuesta = self._crear(**self._programado(dia_creacion="7"))
        servicio = Servicio.objects.get(nombre="Cierre contable")
        self.assertRedirects(
            respuesta, reverse("catalogo:studio", args=[servicio.pk]) + "?tab=general", fetch_redirect_response=False
        )
        programacion = ProgramacionProceso.objects.get(servicio=servicio)
        self.assertEqual((servicio.tipo, servicio.activo), ("PROCESO", False))
        self.assertEqual((programacion.activa, programacion.dia_creacion), (True, 7))
        self.assertEqual(programacion.responsable_inicial.servicio, servicio)
        self.assertEqual(programacion.responsable_inicial.equipo, self.analitica)
        self.assertTrue(any("programado" in m for m in self._mensajes(respuesta)))

    def test_crear_un_proceso_manual_o_un_servicio_no_crea_programacion(self):
        self._entrar(self.admin)
        self._crear(modo="MANUAL")
        self._crear(nombre="Soporte puntual", tipo="SERVICIO", categoria_nueva="Soporte G1", **self._programado())  # lo de Inicio se ignora
        self.assertEqual(Servicio.objects.filter(nombre__in=["Cierre contable", "Soporte puntual"]).count(), 2)
        self.assertFalse(ProgramacionProceso.objects.filter(servicio__nombre__in=["Cierre contable", "Soporte puntual"]).exists())

    def test_crear_un_proceso_programado_incompleto_no_crea_nada(self):
        self._entrar(self.admin)
        respuesta = self._crear(modo="PROGRAMADO", frecuencia="MENSUAL")
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(Servicio.objects.filter(nombre="Cierre contable").exists())
        self.assertContains(respuesta, "Indique el día del mes")
        self.assertContains(respuesta, "Seleccione quién recibe cada ejecución")

    # --- editar ---

    def test_programar_guarda_la_configuracion_y_saca_el_proceso_del_catalogo(self):
        self._entrar(self.admin)
        respuesta = self._guardar(**self._programado())
        programacion = ProgramacionProceso.objects.get(servicio=self.servicio)
        self.assertEqual(
            (programacion.activa, programacion.dia_creacion, programacion.periodo, programacion.responsable_inicial),
            (True, 25, "MES_SIGUIENTE", self.resp_servicio),  # reutiliza el responsable ya configurado
        )
        self.assertTrue(any("Información general actualizada" in m for m in self._mensajes(respuesta)))
        self.assertNotIn(self.servicio.pk, [s.pk for s in servicios_visibles_para(self.solicitante)])

    def test_volver_a_manual_pausa_conserva_la_fila_y_lo_devuelve_al_catalogo(self):
        self._entrar(self.admin)
        self._guardar(**self._programado(dia_creacion="5", periodo="MES_ACTUAL"))
        programacion = ProgramacionProceso.objects.get(servicio=self.servicio)
        ProgramacionProceso.objects.filter(pk=programacion.pk).update(activada_desde=date(2020, 1, 1))
        generar_ejecucion_programada(programacion, hoy=date(2026, 10, 7))
        self._guardar(modo="MANUAL")
        programacion.refresh_from_db()
        self.assertFalse(programacion.activa)
        self.assertEqual(programacion.dia_creacion, 5)
        self.assertEqual(EjecucionProgramada.objects.filter(servicio=self.servicio).count(), 1)
        self.assertIn(self.servicio.pk, [s.pk for s in servicios_visibles_para(self.solicitante)])

    def test_un_envio_sin_la_seccion_inicio_no_toca_la_programacion(self):
        self._entrar(self.admin)
        self._guardar(**self._programado())
        self.client.post(
            reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]),
            self._datos_generales(descripcion="Nueva descripción"),
        )
        self.assertTrue(ProgramacionProceso.objects.get(servicio=self.servicio).activa)

    def test_programado_exige_dia_periodo_y_responsable_y_no_guarda_nada(self):
        self._entrar(self.admin)
        self._guardar(modo="PROGRAMADO", frecuencia="MENSUAL", descripcion="No debe quedar")
        self.assertFalse(ProgramacionProceso.objects.exists())
        self._guardar(**self._programado(dia_creacion="29"), descripcion="No debe quedar")
        self.assertFalse(ProgramacionProceso.objects.exists())
        self.servicio.refresh_from_db()
        self.assertNotEqual(self.servicio.descripcion, "No debe quedar")

    def test_un_flujo_incompatible_explica_que_corregir_y_no_guarda_nada(self):
        servicio, _ = self._proceso("Con solicitante", flujo="solicitante")
        self._entrar(self.admin)
        respuesta = self._guardar(servicio, **self._programado(), descripcion="No debe quedar")
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=servicio).exists())
        self.assertTrue(any("Confirmar con quien solicitó" in m for m in self._mensajes(respuesta)))
        servicio.refresh_from_db()
        self.assertNotEqual(servicio.descripcion, "No debe quedar")
        pagina = self.client.get(reverse("catalogo:studio", args=[servicio.pk]))
        self.assertContains(pagina, "Para programarlo hay que corregir")

    def test_pasar_a_servicio_un_proceso_programado_se_rechaza(self):
        self._entrar(self.admin)
        self._guardar(**self._programado())
        respuesta = self.client.post(
            reverse("catalogo:studio_general_guardar", args=[self.servicio.pk]), self._datos_generales(tipo="SERVICIO")
        )
        self.assertTrue(any("programación activa" in m for m in self._mensajes(respuesta)))
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.tipo, "PROCESO")

    def test_solo_quien_administra_el_catalogo_puede_guardar(self):
        self._entrar(self.solicitante)
        respuesta = self._guardar(**self._programado())
        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(ProgramacionProceso.objects.exists())

    # --- ya no existe «Programados» como categoría del Diseñador ---

    def test_ya_no_existe_la_pestana_programados(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("core:disenador_programados")
        self._entrar(self.admin)
        self.assertEqual(self.client.get("/disenador/programados/").status_code, 404)
        pagina = self.client.get(reverse("core:disenador"))
        self.assertNotIn("programados", [t["clave"] for t in pagina.context["disenador_tabs"]])

    def test_el_error_de_la_ultima_corrida_es_visible_desde_la_configuracion(self):
        self._programar(self.servicio, self.resp_servicio)
        retirar_responsable(self.resp_servicio, self.admin)
        reconciliar_ejecuciones_programadas(hoy=date(2026, 10, 1))
        self._entrar(self.admin)
        pagina = self.client.get(reverse("catalogo:studio", args=[self.servicio.pk]))
        self.assertContains(pagina, "La última ejecución no se pudo generar")
        self.assertContains(pagina, "responsable inicial")

    def test_la_tarea_periodica_es_una_sola_y_delega_en_el_reconciliador(self):
        from unittest.mock import patch

        from apps.tickets import tasks

        with patch("apps.tickets.tasks.reconciliar_ejecuciones_programadas", return_value={"generadas": 0}) as mock:
            self.assertEqual(tasks.generar_ejecuciones_programadas(), {"generadas": 0})
        mock.assert_called_once_with()


class ProximaEjecucionProgramadaTests(_EscenarioProgramadoMixin, TestCase):
    def setUp(self):
        self._preparar_programado()
        self.servicio, self.resp = self._proceso(flujo="simple")

    def test_antes_de_la_fecha_de_creacion_la_proxima_es_futura(self):
        from apps.tickets.programadas import proxima_ejecucion

        programacion = self._programar(self.servicio, self.resp, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 10, 1))
        creacion, periodo, vencida = proxima_ejecucion(programacion, date(2026, 10, 7))
        self.assertEqual((creacion, periodo.etiqueta, vencida), (date(2026, 10, 25), "Noviembre 2026", False))

    def test_vencida_y_sin_generar_se_marca_y_al_generarla_pasa_a_la_siguiente(self):
        from apps.tickets.programadas import proxima_ejecucion

        programacion = self._programar(self.servicio, self.resp, dia=25, periodo="MES_SIGUIENTE", desde=date(2026, 10, 1))
        creacion, periodo, vencida = proxima_ejecucion(programacion, date(2026, 10, 26))
        self.assertEqual((creacion, periodo.etiqueta, vencida), (date(2026, 10, 25), "Noviembre 2026", True))
        generar_ejecucion_programada(programacion, hoy=date(2026, 10, 26))
        creacion, periodo, vencida = proxima_ejecucion(programacion, date(2026, 10, 26))
        self.assertEqual((creacion, periodo.etiqueta, vencida), (date(2026, 11, 25), "Diciembre 2026", False))

    def test_configurar_el_dia_de_hoy_deja_la_ejecucion_vencida_de_inmediato(self):
        from apps.tickets.programadas import proxima_ejecucion

        programacion = self._programar(self.servicio, self.resp, dia=7, periodo="MES_SIGUIENTE", desde=date(2026, 10, 7))
        _, periodo, vencida = proxima_ejecucion(programacion, date(2026, 10, 7))
        self.assertEqual((periodo.etiqueta, vencida), ("Noviembre 2026", True))
