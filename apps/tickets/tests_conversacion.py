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
from apps.gacetas.tests import _EscenarioGacetasMixin

EQUIPO, INTERESADOS = conversacion.EQUIPO, conversacion.INTERESADOS


class ConversacionHttpTests(_EscenarioAtencionMixin, TestCase):
    """Solicitante = interesado (solo su canal); responsable y supervisión = equipo (ambos canales)."""

    def setUp(self):
        self._preparar_escenario()

    def _url(self, nombre, ticket=None, **consulta):
        url = reverse(f"tickets:{nombre}", args=[(ticket or self.ticket).pk])
        return url + ("?" + "&".join(f"{k}={v}" for k, v in consulta.items()) if consulta else "")

    def _entrar(self, usuario):
        self.client.force_login(usuario)

    def test_cada_persona_ve_solo_sus_canales(self):
        self.assertEqual(conversacion.canales_de(self.solicitante, self.ticket), [INTERESADOS])
        self.assertEqual(conversacion.canales_de(self.gestor_area, self.ticket), [EQUIPO, INTERESADOS])
        self.assertEqual(conversacion.canales_de(self.ajeno, self.ticket), [])

    def test_el_canal_del_equipo_queda_aislado_de_los_interesados(self):
        conversacion.enviar(self.ticket, self.gestor_area, "Solo para el equipo", EQUIPO)
        conversacion.enviar(self.ticket, self.solicitante, "Hola", INTERESADOS)
        self._entrar(self.solicitante)
        self.assertEqual(self.client.get(self._url("conversacion", canal=EQUIPO)).status_code, 403)
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"canal": EQUIPO, "texto": "x"}).status_code, 403)
        visibles = self.client.get(self._url("conversacion", canal=INTERESADOS)).json()["mensajes"]
        self.assertEqual([m["texto"] for m in visibles], ["Hola"])
        self._entrar(self.gestor_area)
        self.assertEqual(
            [m["texto"] for m in self.client.get(self._url("conversacion", canal=EQUIPO)).json()["mensajes"]],
            ["Solo para el equipo"],
        )
        self._entrar(self.ajeno)
        self.assertEqual(self.client.get(self._url("conversacion", canal=INTERESADOS)).status_code, 403)

    def test_el_autor_es_la_sesion_y_con_el_ticket_cerrado_solo_se_lee(self):
        self._entrar(self.solicitante)
        datos = {"canal": INTERESADOS, "texto": "  Nota  ", "autor": self.ajeno.pk}
        respuesta = self.client.post(self._url("conversacion_enviar"), datos)
        self.assertEqual(respuesta.status_code, 201)
        comentario = ComentarioTicket.objects.get(pk=respuesta.json()["mensaje"]["id"])
        self.assertEqual((comentario.autor, comentario.contenido, comentario.canal), (self.solicitante, "Nota", INTERESADOS))
        Ticket.objects.filter(pk=self.ticket.pk).update(estado=Ticket.Estado.CANCELADO)
        self.assertEqual(self.client.post(self._url("conversacion_enviar"), {"canal": INTERESADOS, "texto": "x"}).status_code, 403)
        self.assertFalse(self.client.get(self._url("conversacion", canal=INTERESADOS)).json()["puede_escribir"])

    def test_texto_vacio_o_demasiado_largo_se_rechaza(self):
        self._entrar(self.solicitante)
        enviar = self._url("conversacion_enviar")
        self.assertEqual(self.client.post(enviar, {"canal": INTERESADOS, "texto": "   "}).status_code, 400)
        largo = "a" * (conversacion.LONGITUD_MAXIMA + 1)
        self.assertEqual(self.client.post(enviar, {"canal": INTERESADOS, "texto": largo}).status_code, 400)
        self.assertFalse(ComentarioTicket.objects.filter(ticket=self.ticket).exists())

    def test_la_conversacion_es_de_un_solo_ticket(self):
        otro = crear_borrador(self.solicitante, self.servicio)
        _completar_texto(otro, self.solicitante, self.campos)
        from apps.tickets.operaciones import radicar_ticket

        radicar_ticket(otro, self.solicitante)
        conversacion.enviar(self.ticket, self.solicitante, "Solo del primero", INTERESADOS)
        self._entrar(self.solicitante)
        self.assertEqual(self.client.get(self._url("conversacion", otro, canal=INTERESADOS)).json()["mensajes"], [])

    def test_recuperar_desde_un_id(self):
        primero = conversacion.enviar(self.ticket, self.solicitante, "uno", INTERESADOS)
        conversacion.enviar(self.ticket, self.solicitante, "dos", INTERESADOS)
        self._entrar(self.solicitante)
        datos = self.client.get(self._url("conversacion", canal=INTERESADOS, despues=primero["id"])).json()
        self.assertEqual([m["texto"] for m in datos["mensajes"]], ["dos"])

    def test_no_leidos_por_persona_y_canal(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        conversacion.enviar(ticket, self.solicitante, "¿Novedades?", INTERESADOS)
        conversacion.enviar(ticket, self.solicitante, "¿Hay fecha?", INTERESADOS)
        conversacion.enviar(ticket, self.gestor_area, "Revisemos internamente", EQUIPO)
        self.assertEqual(conversacion.no_leidos(self.responsable_directo, ticket, INTERESADOS), 2)
        self.assertEqual(conversacion.no_leidos(self.responsable_directo, ticket, EQUIPO), 1)
        self.assertEqual(conversacion.no_leidos(self.solicitante, ticket, INTERESADOS), 0)  # lo propio no cuenta
        Notificacion.objects.create(
            destinatario=self.responsable_directo, evento="COMENTARIO", grupo="TICKETS", titulo="t", resumen="r",
            ticket=ticket, hechos=[[conversacion.HECHO_CANAL, "Interesados"]],
        )
        self._entrar(self.responsable_directo)
        respuesta = self.client.post(self._url("conversacion_leido"), {"canal": INTERESADOS}).json()
        self.assertEqual(respuesta["no_leidos"], 0)
        self.assertEqual(conversacion.no_leidos(self.responsable_directo, ticket, EQUIPO), 1)  # el otro canal sigue
        self.assertFalse(
            Notificacion.objects.filter(destinatario=self.responsable_directo, ticket=ticket, leida_en__isnull=True).exists()
        )

    def test_el_aviso_va_a_todos_los_del_canal_y_nunca_al_otro(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        ticket = Ticket.objects.get(pk=self.ticket.pk)
        equipo = {u.pk for u in conversacion.destinatarios(ticket, EQUIPO, actor_id=self.gestor_area.pk)}
        self.assertIn(self.responsable_directo.pk, equipo)
        self.assertNotIn(self.solicitante.pk, equipo)
        interesados = {u.pk for u in conversacion.destinatarios(ticket, INTERESADOS, actor_id=self.responsable_directo.pk)}
        self.assertIn(self.solicitante.pk, interesados)
        self.assertNotIn(self.responsable_directo.pk, interesados)  # nunca el autor

    def test_el_espacio_ofrece_sus_canales_y_la_linea_de_tiempo_real(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        self._entrar(self.solicitante)
        pagina = self.client.get(self._url("espacio"))
        self.assertContains(pagina, "data-panel-ticket")
        self.assertContains(pagina, 'data-chat-lista="INTERESADOS"')
        self.assertNotContains(pagina, 'data-chat-lista="EQUIPO"')
        self.assertContains(pagina, 'class="tb-fijo"')
        # El Historial completo (decisiones y observaciones internas) es del equipo, no de los interesados.
        self.assertNotIn("linea_tiempo", pagina.context)
        self.assertNotContains(pagina, 'data-tp-vista="historial"')
        self._entrar(self.responsable_directo)
        eventos = self.client.get(self._url("espacio")).context["linea_tiempo"]
        self.assertEqual([e["accion"] for e in eventos][:2], ["tomó el ticket", "radicó el ticket"])
        pagina = self.client.get(self._url("espacio", panel="conversacion", canal=EQUIPO))
        self.assertContains(pagina, 'data-chat-canal="EQUIPO"')
        self.assertEqual(pagina.context["canal_inicial"], EQUIPO)

    def test_la_vista_completa_ya_no_tiene_comentarios(self):
        self._entrar(self.solicitante)
        # El detalle se retiró: su URL lleva al «Resumen» del espacio, que tampoco tiene comentarios (solo el panel).
        detalle = self.client.get(self._url("detalle"), follow=True)
        self.assertEqual(detalle.status_code, 200)
        self.assertNotContains(detalle, 'name="contenido"')
        self.assertContains(detalle, 'data-tp-abrir="conversacion"')

    def test_quien_no_consulta_el_ticket_no_recibe_el_panel(self):
        self._entrar(self.ajeno)
        self.assertNotEqual(self.client.get(self._url("espacio")).status_code, 200)


class ConversacionArchivosYPresenciaTests(_MediaAisladaMixin, _EscenarioAtencionMixin, TestCase):
    """Archivos en los mensajes (y su descarga solo para quien lee el canal), presencia de los compañeros y el correo
    de los mensajes: solo a quien sigue sin leer unos minutos después (esté o no en línea), uno por canal."""

    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket = Ticket.objects.get(pk=self.ticket.pk)

    def _archivo(self, nombre="nota.txt", contenido=b"hola"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(nombre, contenido, content_type="text/plain")

    def _enviar(self, usuario, canal, texto="", archivos=()):
        self.client.force_login(usuario)
        url = reverse("tickets:conversacion_enviar", args=[self.ticket.pk])
        return self.client.post(url, {"canal": canal, "texto": texto, "archivos": list(archivos)})

    def test_un_mensaje_lleva_archivos_y_puede_ir_sin_texto(self):
        respuesta = self._enviar(self.solicitante, INTERESADOS, archivos=[self._archivo(), self._archivo("b.txt")])
        self.assertEqual(respuesta.status_code, 201)
        mensaje = respuesta.json()["mensaje"]
        self.assertEqual(mensaje["texto"], "")
        self.assertEqual([a["nombre"] for a in mensaje["archivos"]], ["nota.txt", "b.txt"])
        comentario = ComentarioTicket.objects.get(pk=mensaje["id"])
        self.assertEqual(comentario.adjuntos.count(), 2)
        self.assertEqual({a.tipo_relacion for a in comentario.adjuntos.all()}, {"COMENTARIO"})
        # Lo vuelven a recibir quienes cargan la conversación.
        cargados = conversacion.mensajes(self.ticket, INTERESADOS)
        self.assertEqual(len(cargados[-1]["archivos"]), 2)
        # Vacío y sin archivos sigue sin poder enviarse.
        self.assertEqual(self._enviar(self.solicitante, INTERESADOS, texto="  ").status_code, 400)

    def test_limite_de_archivos_por_mensaje(self):
        demasiados = [self._archivo(f"{i}.txt") for i in range(conversacion.MAX_ARCHIVOS + 1)]
        self.assertEqual(self._enviar(self.solicitante, INTERESADOS, texto="x", archivos=demasiados).status_code, 400)
        self.assertFalse(ComentarioTicket.objects.filter(ticket=self.ticket).exists())

    def test_el_archivo_del_equipo_no_lo_descarga_quien_solo_es_interesado(self):
        del_equipo = self._enviar(self.gestor_area, EQUIPO, texto="Interno", archivos=[self._archivo("interno.txt")])
        publico = self._enviar(self.gestor_area, INTERESADOS, texto="Para todos", archivos=[self._archivo("todos.txt")])
        url = lambda r: reverse("tickets:descargar_adjunto", args=[r.json()["mensaje"]["archivos"][0]["id"]])  # noqa: E731
        self.client.force_login(self.solicitante)  # consulta el ticket, pero solo lee «Interesados»
        self.assertEqual(self.client.get(url(del_equipo)).status_code, 403)
        self.assertEqual(self.client.get(url(publico)).status_code, 200)
        self.client.force_login(self.ajeno)
        self.assertEqual(self.client.get(url(publico)).status_code, 403)
        self.client.force_login(self.responsable_directo)
        self.assertEqual(self.client.get(url(del_equipo)).status_code, 200)

    def test_presencia_de_los_compañeros_del_canal(self):
        from django.utils import timezone

        from apps.core.models import Usuario

        Usuario.objects.filter(pk=self.responsable_directo.pk).update(visto_en=timezone.now())
        Usuario.objects.filter(pk=self.gestor_area.pk).update(visto_en=None)
        datos = conversacion.participantes(self.solicitante, self.ticket, INTERESADOS)
        en_linea = {p["id"]: p["en_linea"] for p in datos["personas"]}
        self.assertTrue(en_linea[self.responsable_directo.pk])
        self.assertNotIn(self.solicitante.pk, en_linea)  # uno mismo no aparece
        self.assertEqual(datos["en_linea"], sum(en_linea.values()))
        self.client.force_login(self.solicitante)
        url = reverse("tickets:conversacion_presencia", args=[self.ticket.pk])
        self.assertEqual(self.client.get(url, {"canal": INTERESADOS}).status_code, 200)
        self.assertEqual(self.client.get(url, {"canal": EQUIPO}).status_code, 403)

    def test_cualquier_peticion_y_la_senal_confirman_la_presencia(self):
        from apps.core.models import Usuario

        Usuario.objects.filter(pk=self.solicitante.pk).update(visto_en=None)
        self.client.force_login(self.solicitante)
        self.assertEqual(self.client.post(reverse("core:presencia")).status_code, 204)
        self.assertIsNotNone(Usuario.objects.get(pk=self.solicitante.pk).visto_en)

    def test_el_correo_solo_sale_fuera_de_linea_sin_leer_y_agrupado(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.core.models import Usuario
        from apps.notificaciones.correo_conversacion import ESPERA, enviar_pendientes
        from apps.notificaciones.models import EnvioCorreo

        de_conversacion = EnvioCorreo.objects.filter(notificacion__evento="COMENTARIO")
        Usuario.objects.filter(pk=self.solicitante.pk).update(email="sol@ejemplo.test", visto_en=None)  # fuera de línea
        Usuario.objects.filter(pk=self.responsable_directo.pk).update(email="res@ejemplo.test")
        Usuario.objects.filter(pk=self.responsable_directo.pk).update(visto_en=timezone.now())  # en línea
        with self.captureOnCommitCallbacks(execute=True):
            conversacion.enviar(self.ticket, self.solicitante, "Ya lo reviso", INTERESADOS)
        del_conectado = Notificacion.objects.filter(destinatario=self.responsable_directo, evento="COMENTARIO")
        # Estar en línea ya no impide el correo (2026-11-12): solo leerlo a tiempo.
        self.assertTrue(del_conectado.filter(correo_pendiente_desde__isnull=False, leida_en__isnull=True).exists())
        with self.captureOnCommitCallbacks(execute=True):
            conversacion.enviar(self.ticket, self.responsable_directo, "¿Puedes revisar?", INTERESADOS)
        with self.captureOnCommitCallbacks(execute=True):
            conversacion.enviar(self.ticket, self.responsable_directo, "Te dejé una nota", INTERESADOS)
        # Quien responde en el canal ya vio lo anterior: su aviso queda leído y no le llegará por correo.
        self.assertFalse(del_conectado.filter(leida_en__isnull=True).exists())
        avisos = Notificacion.objects.filter(destinatario=self.solicitante, evento="COMENTARIO")
        self.assertEqual(avisos.count(), 2)
        self.assertTrue(all(a.correo_pendiente_desde for a in avisos))  # espera por si lo lee
        self.assertFalse(de_conversacion.exists())  # nunca al instante
        self.assertEqual(enviar_pendientes(), 0)  # todavía puede leerlo
        self.assertEqual(enviar_pendientes(ahora=timezone.now() + ESPERA + timedelta(seconds=1)), 1)
        envio = de_conversacion.get()  # el responsable lo leyó: solo sale el del solicitante
        self.assertEqual(envio.correo, "sol@ejemplo.test")
        self.assertEqual(envio.notificacion.titulo, "2 mensajes nuevos")  # uno por canal, con todo lo pendiente
        self.assertFalse(Notificacion.objects.filter(correo_pendiente_desde__isnull=False).exists())
        self.assertEqual(enviar_pendientes(ahora=timezone.now() + ESPERA * 2), 0)  # idempotente
        # El correo es como un chat: quién escribió y qué, uno debajo del otro, y un botón al ticket.
        from apps.notificaciones.correo import contenido

        _asunto, texto, html, _imagenes = contenido(envio.notificacion)
        self.responsable_directo.refresh_from_db()  # su correo cambió arriba en la base
        nombre = self.responsable_directo.nombre_visible
        for parte in ("¿Puedes revisar?", "Te dejé una nota", nombre, "cuando no estabas", "Ir al ticket"):
            self.assertIn(parte, html)
        self.assertNotIn("Ya lo reviso", html)  # lo suyo no se le repite
        self.assertIn("Te dejé una nota", texto)

    def test_si_lo_lee_a_tiempo_no_sale_correo(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.core.models import Usuario
        from apps.notificaciones.correo_conversacion import ESPERA, enviar_pendientes
        from apps.notificaciones.models import EnvioCorreo

        Usuario.objects.filter(pk=self.solicitante.pk).update(email="sol@ejemplo.test", visto_en=None)
        with self.captureOnCommitCallbacks(execute=True):
            conversacion.enviar(self.ticket, self.responsable_directo, "Hola", INTERESADOS)
        conversacion.marcar_leido(self.solicitante, self.ticket, INTERESADOS)
        self.assertEqual(enviar_pendientes(ahora=timezone.now() + ESPERA + timedelta(seconds=1)), 0)
        self.assertFalse(EnvioCorreo.objects.filter(notificacion__evento="COMENTARIO").exists())


class ConversacionGacetaTests(_EscenarioGacetasMixin, TestCase):
    """Quien solo revisa la gaceta es INTERESADO: nunca lee lo que el diseñador habló con el aprobador."""

    def setUp(self):
        from apps.gacetas import operaciones as gacetas_ops

        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        gacetas_ops.presentar(self.gaceta, self.banner, self.analista)

    def test_el_revisor_de_la_gaceta_solo_ve_la_conversacion_de_interesados(self):
        self.assertEqual(conversacion.canales_de(self.observador, self.ticket), [INTERESADOS])
        self.assertIn(EQUIPO, conversacion.canales_de(self.analista, self.ticket))
        conversacion.enviar(self.ticket, self.analista, "Diseñadora, ajusta el logo", EQUIPO)
        self.client.force_login(self.observador)
        url = reverse("tickets:conversacion", args=[self.ticket.pk])
        self.assertEqual(self.client.get(url + "?canal=EQUIPO").status_code, 403)
        self.assertEqual(self.client.get(url + "?canal=INTERESADOS").json()["mensajes"], [])
        pagina = self.client.get(reverse("tickets:espacio", args=[self.ticket.pk]))
        self.assertNotContains(pagina, 'data-chat-lista="EQUIPO"')


class ConversacionDePiezasTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    """Quien solo interviene en una pieza del ticket (aquí, responsable de actividades de sus ítems) también
    conversa: el chat es del ticket entero. No gana el historial ni el resto de la información (UX-1)."""

    def setUp(self):
        self._escenario()
        self.client.force_login(self.persona)

    def test_participa_en_la_conversacion_sin_ver_el_historial(self):
        from apps.tickets.autorizacion import puede_consultar_ticket

        self.assertFalse(puede_consultar_ticket(self.persona, self.ticket))
        # Quien hace una actividad del ticket es del EQUIPO: conversa con el equipo (y con los interesados).
        self.assertEqual(conversacion.canales_de(self.persona, self.ticket), [EQUIPO, INTERESADOS])
        enviar = self.client.post(
            reverse("tickets:conversacion_enviar", args=[self.ticket.pk]), {"canal": EQUIPO, "texto": "Listo el copy"}
        )
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
