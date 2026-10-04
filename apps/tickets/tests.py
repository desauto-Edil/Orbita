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
from datetime import timedelta

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
    EntregaTicket,
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

    def test_gestor_de_area_ve_el_ticket_en_su_cola(self):
        self.client.login(username="gestorarea23", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tickets:cola"))
        self.assertIn(self.ticket, respuesta.context["tickets"])

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
        self.assertContains(respuesta, "Solicitud enviada")
        self.assertContains(respuesta, str(self.ticket.radicado))
        self.assertContains(respuesta, self.servicio.nombre)
        self.assertContains(respuesta, reverse("tickets:detalle", args=[self.ticket.pk]))
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
