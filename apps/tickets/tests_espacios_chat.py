"""Conversación del ticket en Google Chat (plan `.claude/plans/conversacion_google_chat.md`). Sin red: el cliente
de Google Chat (`apps.core.google_chat`) se sustituye por un doble en memoria."""

from unittest import mock

from django.test import TestCase, override_settings  # noqa: F401

from apps.core import google_chat as gc
from apps.tickets import conversacion, espacios_chat
from apps.tickets.models import ComentarioTicket, EspacioChatTicket, MensajeChat, MiembroEspacioChat, Ticket
from apps.tickets.operaciones import asignar_ticket
from apps.tickets.tests import _EscenarioAtencionMixin

EQUIPO, INTERESADOS = conversacion.EQUIPO, conversacion.INTERESADOS
CUENTA = "orbita@edilandina.test"


class _ChatFalso:
    """Doble de `apps.core.google_chat`: guarda lo que Órbita pidió."""

    def __init__(self):
        self.espacios, self.miembros, self.publicados, self.quitados, self.lectura = [], {}, [], [], []

    def crear_espacio(self, nombre, id_solicitud):
        nombre_espacio = f"spaces/S{len(self.espacios) + 1}"
        self.espacios.append((nombre_espacio, nombre, id_solicitud))
        return {"name": nombre_espacio, "spaceUri": f"https://chat.google.com/room/{nombre_espacio[7:]}"}

    def agregar_miembro(self, espacio, email):
        self.miembros.setdefault(espacio, set()).add(email)
        return {"membresia": f"{espacio}/members/{email}", "usuario": f"users/{email}"}

    def quitar_miembro(self, membresia):
        self.quitados.append(membresia)

    def listar_miembros(self, espacio):
        return []

    def publicar(self, espacio, texto, id_mensaje, *, como=None):
        self.publicados.append({"espacio": espacio, "texto": texto, "id": id_mensaje, "como": como})
        return f"{espacio}/messages/{len(self.publicados)}"

    def restringir_miembros(self, espacio):
        pass

    def solo_lectura(self, espacio, activar):
        self.lectura.append((espacio, activar))

    def instalar(self, prueba):
        for nombre in ("crear_espacio", "agregar_miembro", "quitar_miembro", "listar_miembros", "publicar",
                       "restringir_miembros", "solo_lectura"):
            parche = mock.patch.object(gc, nombre, getattr(self, nombre))
            parche.start()
            prueba.addCleanup(parche.stop)


AJUSTES = {
    "GOOGLE_CHAT_ACTIVO": True, "GOOGLE_CHAT_CUENTA": CUENTA, "GOOGLE_CHAT_PERMITIDOS": [],
    "GOOGLE_CHAT_PUBSUB_TEMA": "", "GOOGLE_CHAT_PUBSUB_SUSCRIPCION": "",
    "GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL": "sa@edilandina.test", "GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY": "clave",
}


class _ConChat(_EscenarioAtencionMixin):
    def setUp(self):
        ajustes = self.settings(**AJUSTES)
        ajustes.enable()
        self.addCleanup(ajustes.disable)
        self._preparar_escenario()
        for usuario in (self.solicitante, self.responsable_directo, self.miembro_equipo, self.gestor_area):
            usuario.email = usuario.email.replace("@pruebas.orbita", "@edilandina.test")
            usuario.save(update_fields=["email"])
        asignar_ticket(self.ticket, self.gestor_area, usuario=self.responsable_directo)
        self.ticket.refresh_from_db()
        self.chat = _ChatFalso()
        self.chat.instalar(self)
        sin_red = mock.patch.object(gc, "_sesion", side_effect=AssertionError("Una prueba intentó llamar a Google."))
        sin_red.start()
        self.addCleanup(sin_red.stop)
        # Sin Celery: las tareas se ejecutan a mano en cada prueba.
        from apps.tickets import tasks

        for tarea in (tasks.publicar_en_google_chat, tasks.sincronizar_ticket_google_chat):
            parche = mock.patch.object(tarea, "apply_async")
            parche.start()
            self.addCleanup(parche.stop)

    def _enviar(self, autor, texto, canal=INTERESADOS):
        with self.captureOnCommitCallbacks(execute=True):
            mensaje = conversacion.enviar(self.ticket, autor, texto, canal)
        return ComentarioTicket.objects.get(pk=mensaje["id"])

    def _publicar(self, comentario):
        return espacios_chat.publicar_pendiente(comentario.mensaje_chat.pk)


class SalidaTests(_ConChat, TestCase):
    @override_settings(GOOGLE_CHAT_ACTIVO=False)
    def test_apagado_no_deja_nada_en_la_bandeja(self):
        comentario = self._enviar(self.solicitante, "Hola")
        self.assertFalse(MensajeChat.objects.filter(comentario=comentario).exists())

    def test_el_primer_mensaje_crea_el_espacio_del_canal_con_sus_miembros(self):
        comentario = self._enviar(self.solicitante, "Hola")
        mensaje = self._publicar(comentario)
        self.assertEqual(mensaje.estado, MensajeChat.Estado.ENVIADO)
        espacio = EspacioChatTicket.objects.get(ticket=self.ticket, canal=INTERESADOS)
        self.assertEqual((espacio.nombre, espacio.estado), ("spaces/S1", EspacioChatTicket.Estado.ACTIVO))
        self.assertIn(self.ticket.codigo, self.chat.espacios[0][1])
        # R1: quienes intervienen directamente y pertenecen al canal (no la supervisión solo por alcance).
        self.assertIn(self.solicitante.email, self.chat.miembros["spaces/S1"])
        self.assertIn(self.responsable_directo.email, self.chat.miembros["spaces/S1"])
        self.assertNotIn(CUENTA, self.chat.miembros["spaces/S1"])
        # D2: publicado como su autor, con un id propio.
        ultimo = self.chat.publicados[-1]
        self.assertEqual((ultimo["texto"], ultimo["como"]), ("Hola", self.solicitante.email))
        self.assertEqual(ultimo["id"], f"client-orbita-c{comentario.pk}")

    def test_reintentar_no_duplica_ni_crea_otro_espacio(self):
        comentario = self._enviar(self.solicitante, "Hola")
        self._publicar(comentario)
        publicados = len(self.chat.publicados)
        self._publicar(comentario)
        self.assertEqual(len(self.chat.publicados), publicados)
        self._publicar(self._enviar(self.responsable_directo, "Recibido"))
        self.assertEqual(len(self.chat.espacios), 1)

    def test_el_espacio_del_equipo_nunca_tiene_a_quien_solo_es_interesado(self):
        self._publicar(self._enviar(self.responsable_directo, "Interno", EQUIPO))
        espacio = EspacioChatTicket.objects.get(ticket=self.ticket, canal=EQUIPO)
        self.assertNotIn(self.solicitante.email, self.chat.miembros.get(espacio.nombre, set()))

    def test_quien_no_es_miembro_publica_por_la_cuenta_de_orbita_con_su_nombre(self):
        comentario = self._enviar(self.gestor_area, "Supervisando", EQUIPO)  # supervisión: lee, no entra al espacio
        solo_responsable = {self.responsable_directo.pk: self.responsable_directo}
        with mock.patch.object(espacios_chat, "_miembros_deseados", return_value=solo_responsable):
            self._publicar(comentario)
        ultimo = self.chat.publicados[-1]
        self.assertIsNone(ultimo["como"])
        self.assertIn("(desde Órbita): Supervisando", ultimo["texto"])

    def test_en_pruebas_solo_entran_los_correos_permitidos(self):
        with self.settings(GOOGLE_CHAT_PERMITIDOS=[self.solicitante.email]):
            self._publicar(self._enviar(self.solicitante, "Hola"))
        self.assertEqual(self.chat.miembros["spaces/S1"], {self.solicitante.email})

    def test_un_fallo_de_google_queda_para_reintentar(self):
        comentario = self._enviar(self.solicitante, "Hola")
        with mock.patch.object(gc, "crear_espacio", side_effect=gc.ErrorChat("caído")):
            mensaje = self._publicar(comentario)
        self.assertEqual(mensaje.estado, MensajeChat.Estado.FALLIDO)
        self.assertEqual(self._publicar(comentario).estado, MensajeChat.Estado.ENVIADO)


class MiembrosYEstadoTests(_ConChat, TestCase):
    def setUp(self):
        super().setUp()
        self._publicar(self._enviar(self.solicitante, "Hola"))
        self.espacio = EspacioChatTicket.objects.get(ticket=self.ticket, canal=INTERESADOS)

    def test_quien_pierde_la_relacion_sale_del_espacio(self):
        with mock.patch.object(espacios_chat, "_miembros_deseados", return_value={}):
            espacios_chat.sincronizar_miembros(self.espacio)
        self.assertFalse(MiembroEspacioChat.objects.filter(espacio=self.espacio, activo=True).exists())
        self.assertTrue(self.chat.quitados)

    def test_al_cancelar_queda_en_solo_lectura_y_al_reabrir_vuelve(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.CANCELADO)
        espacios_chat.sincronizar_ticket(self.ticket.pk)
        self.espacio.refresh_from_db()
        self.assertEqual(self.espacio.estado, EspacioChatTicket.Estado.SOLO_LECTURA)
        self.assertEqual(self.chat.lectura[-1], (self.espacio.nombre, True))
        self.assertIn("queda de consulta", self.chat.publicados[-1]["texto"])
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.EN_ATENCION)
        espacios_chat.sincronizar_ticket(self.ticket.pk)
        self.espacio.refresh_from_db()
        self.assertEqual(self.espacio.estado, EspacioChatTicket.Estado.ACTIVO)
        self.assertEqual(self.chat.lectura[-1], (self.espacio.nombre, False))

    def test_el_panel_ofrece_el_espacio_solo_a_quien_esta_en_el(self):
        self.assertEqual(espacios_chat.enlaces_para(self.solicitante, self.ticket), {INTERESADOS: self.espacio.uri})
        self.assertEqual(espacios_chat.enlaces_para(self.ajeno, self.ticket), {})


class EntradaTests(_ConChat, TestCase):
    def setUp(self):
        super().setUp()
        self._publicar(self._enviar(self.solicitante, "Hola"))
        self.espacio = EspacioChatTicket.objects.get(ticket=self.ticket, canal=INTERESADOS)

    def _desde_chat(self, usuario, texto, nombre="spaces/S1/messages/X1", **extra):
        datos = {"name": nombre, "sender": {"name": f"users/{usuario.email}", "type": "HUMAN"}, "text": texto,
                 "createTime": "2026-11-09T15:00:00.000000Z", **extra}
        with self.captureOnCommitCallbacks(execute=True):
            return espacios_chat.importar_mensaje(self.espacio, datos)

    def test_lo_escrito_en_chat_entra_a_la_conversacion_una_sola_vez(self):
        mensaje = self._desde_chat(self.responsable_directo, "Ya lo reviso")
        self.assertEqual(mensaje.estado, MensajeChat.Estado.IMPORTADO)
        comentario = mensaje.comentario
        self.assertEqual((comentario.autor, comentario.canal, comentario.origen),
                         (self.responsable_directo, INTERESADOS, ComentarioTicket.Origen.GOOGLE_CHAT))
        self.assertFalse(MensajeChat.objects.filter(comentario=comentario, direccion="SALIDA").exists())  # sin eco
        self.assertIsNone(self._desde_chat(self.responsable_directo, "Ya lo reviso"))
        self.assertEqual(ComentarioTicket.objects.filter(contenido="Ya lo reviso").count(), 1)

    def test_lo_que_publico_orbita_no_vuelve_a_entrar(self):
        self.assertIsNone(self._desde_chat(self.solicitante, "Hola", nombre="spaces/S1/messages/X2",
                                           clientAssignedMessageId="client-orbita-c1"))

    def test_quien_no_es_miembro_o_ya_no_puede_escribir_queda_rechazado(self):
        mensaje = self._desde_chat(self.ajeno, "Me colé", nombre="spaces/S1/messages/X3")
        self.assertEqual(mensaje.estado, MensajeChat.Estado.RECHAZADO)
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.CERRADO)
        mensaje = self._desde_chat(self.solicitante, "Tarde", nombre="spaces/S1/messages/X4")
        self.assertEqual(mensaje.estado, MensajeChat.Estado.RECHAZADO)
        self.assertFalse(ComentarioTicket.objects.filter(contenido__in=("Me colé", "Tarde")).exists())

    def test_adjuntos_y_textos_largos(self):
        mensaje = self._desde_chat(self.solicitante, "x" * 2500, nombre="spaces/S1/messages/X5", attachment=[{}])
        texto = mensaje.comentario.contenido
        self.assertLessEqual(len(texto), conversacion.LONGITUD_MAXIMA)
        self.assertTrue(texto.endswith(espacios_chat.MARCA_RECORTE))
