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

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import (
    Campo,
    Categoria,
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
from apps.tickets.models import (
    Adjunto,
    ArchivoRespuestaCampo,
    ComentarioTicket,
    HistorialTicket,
    ResolucionTicket,
    RespuestaCampo,
    RespuestaFormulario,
    RespuestaSolicitudInformacion,
    SolicitudInformacion,
    Ticket,
    TicketServicio,
)
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
        # zona horaria activa al guardarlo y lo devuelve aware al leerlo
        # — se compara sin tzinfo, no la representación con offset.
        self.assertEqual(respuesta.valor_fecha_hora.replace(tzinfo=None).isoformat(), "2026-03-05T10:30:00")

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
