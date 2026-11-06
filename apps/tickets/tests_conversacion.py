"""Conversación del ticket (chat en tiempo real) e historial como línea de tiempo.

Cubren: la conversación es del ticket ENTERO — participa quien lo consulta o interviene en una de sus piezas (la
población del espacio) y escribe mientras el ticket está abierto —, en HTTP y en WebSocket (al conectar y en cada
operación); quien no participa no lee ni escribe; el autor lo pone el servidor; texto validado (vacío,
longitud); conversación aislada por ticket; no leídos por persona (la marca de lectura, lo propio no cuenta, marcar
leído también marca los avisos COMENTARIO); entrega en tiempo real entre dos sesiones y recuperación tras
reconectar; el espacio del ticket con el panel global (Conversación | Historial) y la línea de tiempo con los
eventos reales de `HistorialTicket`."""

from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.notificaciones.models import Notificacion
from apps.tickets import conversacion
from apps.tickets.models import ComentarioTicket, Ticket
from apps.tickets.operaciones import crear_borrador, tomar_ticket
from apps.tickets.routing import websocket_urlpatterns
from apps.tickets.tests import CLAVE_PRUEBA, _completar_texto, _EscenarioAtencionMixin, _MediaAisladaMixin
from apps.tickets.tests_edicion_por_tarea import _TareasConEdicionMixin


class ConversacionHttpTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()

    def _url(self, nombre, ticket=None):
        return reverse(f"tickets:{nombre}", args=[(ticket or self.ticket).pk])

    def _entrar(self, usuario):
        self.client.force_login(usuario)

    def test_todo_el_que_participa_lee_y_escribe_y_quien_no_recibe_403(self):
        conversacion.enviar(self.ticket, self.solicitante, "Hola")
        self._entrar(self.gestor_area)  # supervisa por alcance: también participa de la conversación del ticket
        datos = self.client.get(self._url("conversacion")).json()
        self.assertEqual([m["texto"] for m in datos["mensajes"]], ["Hola"])
        self.assertTrue(datos["puede_escribir"])
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"texto": "Visto"}).status_code, 201)
        self._entrar(self.ajeno)
        self.assertEqual(self.client.get(self._url("conversacion")).status_code, 403)
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"texto": "x"}).status_code, 403)

    def test_el_autor_es_la_sesion_y_con_el_ticket_cerrado_solo_se_lee(self):
        self._entrar(self.solicitante)
        respuesta = self.client.post(self._url("conversacion_enviar"), {"texto": "  Nota  ", "autor": self.ajeno.pk})
        self.assertEqual(respuesta.status_code, 201)
        comentario = ComentarioTicket.objects.get(pk=respuesta.json()["mensaje"]["id"])
        self.assertEqual((comentario.autor, comentario.contenido), (self.solicitante, "Nota"))
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.CANCELADO)
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"texto": "x"}).status_code, 403)
        self.assertFalse(self.client.get(self._url("conversacion")).json()["puede_escribir"])

    def test_texto_vacio_o_demasiado_largo_se_rechaza(self):
        self._entrar(self.solicitante)
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"texto": "   "}).status_code, 400)
        largo = "a" * (conversacion.LONGITUD_MAXIMA + 1)
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"texto": largo}).status_code, 400)
        self.assertFalse(ComentarioTicket.objects.filter(ticket=self.ticket).exists())

    def test_la_conversacion_es_de_un_solo_ticket(self):
        otro = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(otro, self.solicitante, self.campos)
        from apps.tickets.operaciones import radicar_ticket

        radicar_ticket(otro, self.solicitante)
        conversacion.enviar(self.ticket, self.solicitante, "Solo del primero")
        self._entrar(self.solicitante)
        self.assertEqual(self.client.get(self._url("conversacion", otro)).json()["mensajes"], [])

    def test_recuperar_desde_un_id(self):
        primero = conversacion.enviar(self.ticket, self.solicitante, "uno")
        conversacion.enviar(self.ticket, self.solicitante, "dos")
        self._entrar(self.solicitante)
        datos = self.client.get(self._url("conversacion") + f"?despues={primero['id']}").json()
        self.assertEqual([m["texto"] for m in datos["mensajes"]], ["dos"])

    def test_no_leidos_por_persona_y_marcar_leido(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        conversacion.enviar(ticket, self.solicitante, "¿Novedades?")
        conversacion.enviar(ticket, self.solicitante, "¿Hay fecha?")
        self.assertEqual(conversacion.no_leidos(self.responsable_directo, ticket), 2)
        self.assertEqual(conversacion.no_leidos(self.solicitante, ticket), 0)  # lo propio no cuenta
        Notificacion.objects.create(
            destinatario=self.responsable_directo, evento="COMENTARIO", grupo="TICKETS", titulo="t", resumen="r",
            ticket=ticket,
        )
        self._entrar(self.responsable_directo)
        self.assertEqual(self.client.post(self._url("conversacion_leido"), {}).json()["no_leidos"], 0)
        self.assertFalse(
            Notificacion.objects.filter(destinatario=self.responsable_directo, ticket=ticket, leida_en__isnull=True).exists()
        )
        conversacion.enviar(ticket, self.solicitante, "Otra")
        self.assertEqual(conversacion.no_leidos(self.responsable_directo, ticket), 1)

    def test_el_espacio_ofrece_el_panel_y_la_linea_de_tiempo_real(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self._entrar(self.solicitante)
        pagina = self.client.get(self._url("espacio"))
        self.assertContains(pagina, 'data-panel-ticket')
        self.assertContains(pagina, 'data-tp-abrir="conversacion"')
        self.assertNotContains(pagina, 'name="contenido"')  # el formulario de comentarios inferior se retiró
        self.assertContains(pagina, 'class="tb-fijo"')
        self.assertContains(pagina, 'class="tb-cuerpo"')
        eventos = pagina.context["linea_tiempo"]
        self.assertEqual([e["accion"] for e in eventos][:2], ["tomó el ticket", "radicó el ticket"])  # del más reciente al más antiguo
        historial = self.client.get(self._url("espacio") + "?panel=historial")
        self.assertEqual(historial.context["panel_global"], "historial")
        self.assertContains(historial, 'class="tl"')

    def test_quien_no_consulta_el_ticket_no_recibe_el_panel(self):
        self._entrar(self.ajeno)
        self.assertNotEqual(self.client.get(self._url("espacio")).status_code, 200)


class ConversacionWebSocketTests(_EscenarioAtencionMixin, TransactionTestCase):
    """Tiempo real: dos sesiones conectadas al mismo ticket (capa de canales en memoria)."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)

    def _comunicador(self, usuario, ticket=None):
        ruta = f"/ws/tickets/{(ticket or self.ticket).pk}/conversacion/"
        comunicador = WebsocketCommunicator(URLRouter(websocket_urlpatterns), ruta)
        comunicador.scope["user"] = usuario
        return comunicador

    def test_sin_acceso_la_conexion_se_cierra(self):
        async def flujo():
            comunicador = self._comunicador(self.ajeno)
            conectado, codigo = await comunicador.connect()
            self.assertFalse(conectado)
            self.assertEqual(codigo, 4403)

        async_to_sync(flujo)()

    def test_un_mensaje_llega_a_la_otra_sesion_y_se_recupera_al_reconectar(self):
        async def flujo():
            solicitante = self._comunicador(self.solicitante)
            responsable = self._comunicador(self.responsable_directo)
            self.assertTrue((await solicitante.connect())[0])
            self.assertTrue((await responsable.connect())[0])

            await solicitante.send_json_to({"tipo": "enviar", "texto": "Hola equipo", "cliente_id": "c1", "autor_id": 999})
            confirmacion = await solicitante.receive_json_from(timeout=5)
            recibidos = [confirmacion, await solicitante.receive_json_from(timeout=5)]
            self.assertIn("c1", [r.get("cliente_id") for r in recibidos])  # el emisor reconoce su mensaje
            llegada = await responsable.receive_json_from(timeout=5)
            self.assertEqual(llegada["mensaje"]["texto"], "Hola equipo")
            self.assertEqual(llegada["mensaje"]["autor_id"], self.solicitante.pk)  # nunca el del cliente
            ultimo = llegada["mensaje"]["id"]
            await responsable.disconnect()

            await solicitante.send_json_to({"tipo": "enviar", "texto": "¿Me lees?", "cliente_id": "c2"})
            await solicitante.receive_json_from(timeout=5)
            await solicitante.receive_json_from(timeout=5)

            de_nuevo = self._comunicador(self.responsable_directo)
            self.assertTrue((await de_nuevo.connect())[0])
            await de_nuevo.send_json_to({"tipo": "sincronizar", "despues": ultimo})
            lote = await de_nuevo.receive_json_from(timeout=5)
            self.assertEqual([m["texto"] for m in lote["mensajes"]], ["¿Me lees?"])
            contador = await de_nuevo.receive_json_from(timeout=5)
            self.assertEqual(contador, {"tipo": "no_leidos", "total": 2})
            await de_nuevo.send_json_to({"tipo": "leido"})
            self.assertEqual(await de_nuevo.receive_json_from(timeout=5), {"tipo": "no_leidos", "total": 0})
            await de_nuevo.disconnect()
            await solicitante.disconnect()

        async_to_sync(flujo)()

    def test_con_el_ticket_cerrado_no_se_escribe_por_websocket(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.CANCELADO)

        async def flujo():
            gestor = self._comunicador(self.solicitante)
            self.assertTrue((await gestor.connect())[0])  # quien solicitó sigue leyendo
            await gestor.send_json_to({"tipo": "enviar", "texto": "intento", "cliente_id": "g1"})
            respuesta = await gestor.receive_json_from(timeout=5)
            self.assertEqual((respuesta["tipo"], respuesta["cliente_id"]), ("error", "g1"))
            await gestor.disconnect()

        async_to_sync(flujo)()
        self.assertFalse(ComentarioTicket.objects.filter(ticket=self.ticket).exists())


class ConversacionDePiezasTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    """Quien solo interviene en una pieza del ticket (aquí, responsable de actividades de sus ítems) también
    conversa: el chat es del ticket entero. No gana el historial ni el resto de la información (UX-1)."""

    def setUp(self):
        self._escenario()
        self.client.force_login(self.persona)

    def test_participa_en_la_conversacion_sin_ver_el_historial(self):
        from apps.tickets.autorizacion import puede_consultar_ticket

        self.assertFalse(puede_consultar_ticket(self.persona, self.ticket))
        enviar = self.client.post(reverse("tickets:conversacion_enviar", args=[self.ticket.pk]), {"texto": "Listo el copy"})
        self.assertEqual(enviar.status_code, 201)
        pagina = self.client.get(reverse("tickets:espacio", args=[self.ticket.pk]))
        self.assertContains(pagina, 'data-tp-abrir="conversacion"')
        self.assertNotContains(pagina, 'data-tp-abrir="historial"')
        self.assertNotContains(pagina, 'data-tp-vista="historial"')
        self.assertNotIn("linea_tiempo", pagina.context)


class LineaDeTiempoTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    """El historial reúne lo ocurrido en las piezas del ticket, no solo `HistorialTicket`."""

    def setUp(self):
        self._escenario()

    def test_incluye_las_actividades_del_flujo_con_su_actor(self):
        from apps.tickets import linea_tiempo

        eventos = linea_tiempo.eventos(self.ticket)
        actividades = [e for e in eventos if e["accion"] == "creó la actividad"]
        self.assertTrue(actividades)
        self.assertIn(self.tarea.titulo, {e["objeto"] for e in actividades})
        fechas = [e["fecha"] for e in eventos]
        self.assertEqual(fechas, sorted(fechas, reverse=True))
