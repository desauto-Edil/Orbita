"""Bandeja compartida de «Mi trabajo» y «Mis tickets» con inspector lateral.

Cubren: una fila por ticket aunque la persona tenga varias actividades en él, contadores de TICKETS, clasificación
«Requiere atención / En seguimiento» intacta (la de `bandeja.construir`), segmentos y búsqueda de Mis tickets,
borradores que se continúan, inspector con SUS bloques (sin avance, fases ni entregables), datos reales y la misma
autorización de la vista que abre cada fila, etiquetas semánticas globales (sin puntos) y la fase actual calculada en
lote igual que `seguimiento.fase_actual`."""

from django.test import TestCase
from django.urls import reverse

from apps.tareas.models import Tarea
from apps.tickets import seguimiento
from apps.tickets.models import Ticket
from apps.tickets.operaciones import crear_borrador, solicitar_informacion, tomar_ticket
from apps.tickets.tests import CLAVE_PRUEBA, Usuario, _EscenarioAtencionMixin, _MediaAisladaMixin
from apps.tickets.tests_edicion_por_tarea import _TareasConEdicionMixin

FETCH = {"HTTP_X_REQUESTED_WITH": "fetch"}


def _inspector(ticket, desde, **extra):
    url = reverse("tickets:inspector", args=[ticket.pk]) + f"?desde={desde}"
    return url + "".join(f"&{k}={v}" for k, v in extra.items())


class BandejaMiTrabajoTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()
        # La misma persona con DOS actividades del mismo ticket.
        Tarea.objects.filter(pk=self.tarea_ajena.pk).update(usuario_responsable=self.persona)
        self.client.force_login(self.persona)

    def _bandeja(self, **parametros):
        return self.client.get(reverse("core:mi_trabajo"), parametros).context["bandeja"]

    def test_un_ticket_con_varias_actividades_aparece_una_sola_vez(self):
        pagina = self.client.get(reverse("core:mi_trabajo"))
        filas = pagina.context["bandeja"]["filas"]
        self.assertEqual([g.ticket.pk for g in filas].count(self.ticket.pk), 1)
        grupo = next(g for g in filas if g.ticket.pk == self.ticket.pk)
        self.assertTrue({f"tarea-{self.tarea.pk}", f"tarea-{self.tarea_ajena.pk}"} <= {p.clave for p in grupo.acciones})
        self.assertContains(pagina, f'data-fila="{self.ticket.pk}"', count=1)

    def test_los_contadores_cuentan_tickets_no_actividades(self):
        bandeja = self._bandeja()
        tickets = {g.ticket.pk for g in bandeja["filas"]}
        self.assertEqual(bandeja["total"], len(tickets))
        self.assertEqual(bandeja["total"], bandeja["total_atencion"] + bandeja["total_seguimiento"])

    def test_la_clasificacion_es_la_de_la_bandeja_y_la_atencion_va_primero(self):
        bandeja = self._bandeja()
        situaciones = [g.situacion for g in bandeja["filas"]]
        for grupo in bandeja["filas"]:
            self.assertEqual(grupo.situacion, "atencion" if grupo.requiere_atencion else "seguimiento")
        self.assertEqual(situaciones, sorted(situaciones, key=lambda s: s != "atencion"))
        self.assertEqual(self._bandeja(ver="seguimiento")["atencion"], [])

    def test_la_busqueda_y_el_filtro_se_conservan(self):
        servicio = self.ticket.detalle_servicio.servicio.nombre
        self.assertIn(self.ticket.pk, [g.ticket.pk for g in self._bandeja(q=servicio[:4].lower())["filas"]])
        self.assertEqual(self._bandeja(q="zzz-no-existe")["filas"], [])

    def test_la_fila_es_un_enlace_al_espacio_y_ofrece_el_inspector(self):
        pagina = self.client.get(reverse("core:mi_trabajo"))
        grupo = next(g for g in pagina.context["bandeja"]["filas"] if g.ticket.pk == self.ticket.pk)
        self.assertContains(pagina, f'<a class="bt-fila__enlace" href="{grupo.url}"')
        self.assertContains(pagina, _inspector(self.ticket, "trabajo", situacion=grupo.situacion).replace("&", "&amp;"))

    def test_el_inspector_muestra_los_pendientes_y_abre_el_espacio(self):
        respuesta = self.client.get(_inspector(self.ticket, "trabajo", situacion="atencion"), **FETCH)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Requiere tu atención")
        self.assertContains(respuesta, self.tarea.titulo)
        self.assertContains(respuesta, "Abrir espacio de trabajo")
        self.assertContains(respuesta, reverse("tickets:espacio", args=[self.ticket.pk]))

    def test_el_inspector_tiene_solo_sus_bloques_y_etiquetas_semanticas(self):
        respuesta = self.client.get(_inspector(self.ticket, "trabajo", situacion="atencion"), **FETCH)
        html = respuesta.content.decode()
        self.assertIn("Tus pendientes", html)
        # Relación solo con sus piezas (no consulta el ticket): ni solicitud ni historial (UX-1).
        for reservado in ("Información general", "Descripción", "Actividad reciente"):
            self.assertNotIn(reservado, html)
        for retirado in ("Avance", "Entregables", "Fases del proceso", "Fase actual", "estado-punto", "bt-situacion"):
            self.assertNotIn(retirado, html)
        self.assertIn('class="estado-etiqueta estado-etiqueta--atencion"', html)
        self.assertIn(f'estado-etiqueta--{self.ticket.estado.lower()}', html)
        # La acción principal va en el pie fijo, fuera del cuerpo que se desplaza.
        self.assertLess(html.index('class="bt-ins__cuerpo"'), html.index('class="bt-ins__pie"'))
        self.assertIn("Abrir espacio de trabajo", html[html.index('class="bt-ins__pie"'):])

    def test_las_filas_usan_etiquetas_semanticas_sin_puntos(self):
        pagina = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(pagina, "estado-etiqueta estado-etiqueta--")
        self.assertNotContains(pagina, "estado-punto")
        self.assertNotContains(pagina, "bt-situacion")

    def test_sin_js_el_inspector_lleva_al_espacio(self):
        respuesta = self.client.get(_inspector(self.ticket, "trabajo"))
        self.assertRedirects(respuesta, reverse("tickets:espacio", args=[self.ticket.pk]), fetch_redirect_response=False)

    def test_el_inspector_respeta_la_relacion_con_el_ticket(self):
        ajena = Usuario.objects.create_user(username="sin_relacion_bt", password=CLAVE_PRUEBA)
        self.client.force_login(ajena)
        self.assertEqual(self.client.get(_inspector(self.ticket, "trabajo"), **FETCH).status_code, 403)
        self.assertEqual(self.client.get(_inspector(self.ticket, "tickets"), **FETCH).status_code, 403)


class BandejaMisTicketsTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        self.borrador = crear_borrador(self.solicitante, self.servicio)
        self.client.login(username="solicitante23", password=CLAVE_PRUEBA)

    def _pagina(self, **parametros):
        return self.client.get(reverse("tickets:mis_tickets"), parametros)

    def test_los_segmentos_separan_borradores_y_enviados(self):
        self.assertEqual([t.pk for t in self._pagina(ver="borradores").context["filas"]], [self.borrador.pk])
        enviados = [t.pk for t in self._pagina(ver="curso").context["filas"]]
        self.assertIn(self.ticket.pk, enviados)
        self.assertNotIn(self.borrador.pk, enviados)
        totales = {s["clave"]: s["total"] for s in self._pagina().context["segmentos"]}
        self.assertEqual(totales["todos"], 2)
        self.assertEqual(totales["borradores"], 1)

    def test_lo_que_espera_respuesta_va_primero_y_se_marca(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        solicitar_informacion(Ticket.objects.get(pk=self.ticket.pk), self.responsable_directo, "¿Formato?")
        pagina = self._pagina()
        self.assertEqual(pagina.context["filas"][0].pk, self.ticket.pk)
        self.assertContains(pagina, "Requiere tu respuesta")

    def test_busqueda_por_codigo(self):
        self.assertEqual([t.pk for t in self._pagina(q=self.ticket.codigo).context["filas"]], [self.ticket.pk])
        self.assertContains(self._pagina(q="zzz-no-existe"), "Ningún ticket coincide")

    def test_el_borrador_se_continua(self):
        pagina = self._pagina()
        self.assertContains(pagina, f'href="{reverse("tickets:borrador", args=[self.borrador.pk])}"')
        respuesta = self.client.get(_inspector(self.borrador, "tickets"), **FETCH)
        self.assertContains(respuesta, "Continuar")
        self.assertContains(respuesta, reverse("tickets:borrador", args=[self.borrador.pk]))

    def test_el_inspector_del_ticket_enviado_muestra_datos_reales(self):
        respuesta = self.client.get(_inspector(self.ticket, "tickets"), **FETCH)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.ticket.codigo)
        self.assertContains(respuesta, "Ver ticket")
        self.assertContains(respuesta, "Radicado")  # actividad reciente: el historial real
        self.assertContains(respuesta, reverse("tickets:espacio", args=[self.ticket.pk]))

    def test_el_inspector_de_mis_tickets_no_muestra_tu_situacion_ni_pendientes(self):
        tomar_ticket(self.ticket, self.responsable_directo)
        solicitar_informacion(Ticket.objects.get(pk=self.ticket.pk), self.responsable_directo, "¿Formato?")
        html = self.client.get(_inspector(self.ticket, "tickets"), **FETCH).content.decode()
        self.assertNotIn("Tu situación", html)
        self.assertNotIn("Tus pendientes", html)
        self.assertIn("Información general", html)
        self.assertIn("Radicado el", html)
        self.assertIn("Descripción", html)

    def test_nadie_mas_ve_el_inspector_de_mis_tickets(self):
        self.client.login(username="ajeno23", password=CLAVE_PRUEBA)
        self.assertEqual(self.client.get(_inspector(self.ticket, "tickets"), **FETCH).status_code, 403)
        self.assertEqual(self.client.get(_inspector(self.borrador, "tickets"), **FETCH).status_code, 403)
        self.assertEqual(self.client.get(_inspector(self.borrador, "trabajo"), **FETCH).status_code, 403)

    def test_la_fase_en_lote_coincide_con_la_fase_actual(self):
        tickets = list(Ticket.objects.filter(solicitante=self.solicitante))
        en_lote = seguimiento.fases_actuales(tickets)
        for ticket in tickets:
            self.assertEqual(en_lote.get(ticket.pk, ""), seguimiento.fase_actual(ticket))
