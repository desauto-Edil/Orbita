"""UX-1 — Espacio unificado del Ticket y bandeja de Trabajo agrupada por ticket.

Cubren: vínculos tarea/aprobación → ticket, agrupación de Mi trabajo (una entrada por ticket, sin duplicados,
atención vs. seguimiento, pendientes sin ticket aparte), acceso al espacio por la relación con el ticket (sin ganar
el resto del ticket), composición adaptativa (pestañas y acciones según `puede_*`), acciones dentro del espacio con
`next` y compatibilidad de las URL anteriores. Reutilizan los escenarios de `apps.tickets.tests`,
`apps.tickets.tests_edicion_por_tarea` y `apps.gacetas.tests`. UX-2 suma la bandeja de solo tickets (búsqueda, filtro,
sin tareas completadas), el panel derecho bajo demanda, la selección por defecto y Mis tickets por secciones.
Se entregan SIN ejecutar.
"""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.gacetas import operaciones as gacetas_ops
from apps.gacetas.tests import _EscenarioGacetasMixin
from apps.tareas.models import Tarea
from apps.tareas.operaciones import crear_tarea
from apps.tickets import bandeja as bandeja_ops
from apps.tickets import espacio as espacio_ops
from apps.tickets import vinculos
from apps.tickets.models import SolicitudInformacion, Ticket
from apps.tickets.operaciones import solicitar_informacion, tomar_ticket
from apps.tickets.tests import CLAVE_PRUEBA, Usuario, _EscenarioAtencionMixin, _MediaAisladaMixin
from apps.tickets.tests_edicion_por_tarea import _TareasConEdicionMixin


def _espacio(ticket, **parametros):
    url = reverse("tickets:espacio", args=[ticket.pk])
    if parametros:
        url += "?" + "&".join(f"{k}={v}" for k, v in parametros.items())
    return url


# --- Vínculos -------------------------------------------------------------------------------------------


class VinculosTareaTicketTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()

    def test_una_tarea_por_elemento_pertenece_a_su_ticket_y_a_su_item(self):
        origen = vinculos.origen_de_tareas([self.tarea.pk, self.tarea_ajena.pk])
        self.assertEqual(origen[self.tarea.pk]["ticket_id"], self.ticket.pk)
        self.assertEqual(origen[self.tarea.pk]["elemento_id"], self.elemento.pk)
        self.assertEqual(origen[self.tarea.pk]["origen"], "ELEMENTO")
        self.assertEqual(origen[self.tarea_ajena.pk]["elemento_id"], self.ajeno_el.pk)

    def test_una_tarea_independiente_no_tiene_ticket(self):
        suelta = crear_tarea(titulo="Suelta UX1", creada_por=self.persona, usuario_responsable=self.persona)
        self.assertNotIn(suelta.pk, vinculos.origen_de_tareas([suelta.pk]))

    def test_tareas_del_ticket_incluye_las_del_ticket_y_no_otras(self):
        suelta = crear_tarea(titulo="Suelta UX1", creada_por=self.persona, usuario_responsable=self.persona)
        ids = set(vinculos.tareas_del_ticket(self.ticket).values_list("pk", flat=True))
        self.assertTrue({self.tarea.pk, self.tarea_ajena.pk} <= ids)
        self.assertNotIn(suelta.pk, ids)


class VinculosGacetaTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion, columnas_editables=["pieza"])
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.p_banner = gacetas_ops.presentar(self.gaceta, self.banner, self.analista)

    def test_la_correccion_de_una_ronda_apunta_al_ticket_y_no_hay_aprobacion(self):
        self.assertIsNone(self.p_banner.rondas.get().esquema)  # nadie aprueba en una gaceta
        self.assertFalse(vinculos.aprobaciones_del_ticket(self.ticket).exists())
        gacetas_ops.observar(self.p_banner, self.observador, "Falta el logo")
        correccion = Tarea.objects.get(ronda_gaceta__participacion=self.p_banner)
        self.assertEqual(vinculos.origen_de_tareas([correccion.pk])[correccion.pk]["origen"], "GACETA")


# --- Bandeja «Mi trabajo» por ticket ---------------------------------------------------------------------


class BandejaPorTicketTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()

    def test_una_entrada_por_ticket_con_sus_pendientes(self):
        segunda = crear_tarea(titulo="Otra del mismo ticket", creada_por=self.persona, usuario_responsable=self.persona)
        self.client.force_login(self.persona)
        bandeja = self.client.get(reverse("core:mi_trabajo")).context["bandeja"]
        tickets = [g.ticket.pk for g in bandeja["atencion"]]
        self.assertEqual(tickets.count(self.ticket.pk), 1)  # sin duplicados
        grupo = next(g for g in bandeja["atencion"] if g.ticket.pk == self.ticket.pk)
        self.assertIn(f"tarea-{self.tarea.pk}", [p.clave for p in grupo.acciones])
        self.assertNotIn(f"tarea-{self.tarea_ajena.pk}", [p.clave for p in grupo.pendientes])
        # Una tarea independiente no pertenece a ningún ticket: una sola vista de trabajo, no se lista.
        pendientes = [p.clave for g in bandeja["atencion"] + bandeja["seguimiento"] for p in g.pendientes]
        self.assertNotIn(f"tarea-{segunda.pk}", pendientes)

    def test_abrir_lleva_al_espacio_y_destaca_la_unica_accion(self):
        self.client.force_login(self.persona)
        grupo = next(
            g for g in self.client.get(reverse("core:mi_trabajo")).context["bandeja"]["atencion"]
            if g.ticket.pk == self.ticket.pk
        )
        self.assertEqual(grupo.url, _espacio(self.ticket, accion=f"tarea-{self.tarea.pk}"))

    def test_el_responsable_ve_el_ticket_y_la_participacion(self):
        resultado = bandeja_ops.construir(
            self.analista, tareas=[], aprobaciones=[], revisiones=[], colecciones=[], a_cargo=[self.ticket]
        )
        grupos = resultado["atencion"] + resultado["seguimiento"]
        self.assertEqual([g.ticket.pk for g in grupos], [self.ticket.pk])
        self.assertIn("Responsable", grupos[0].participacion)

    def test_las_pestanas_tecnicas_se_retiraron(self):
        self.client.force_login(self.persona)
        pagina = self.client.get(reverse("core:mi_trabajo"), {"tab": "tareas"})
        self.assertIsNotNone(pagina.context["bandeja"])  # `?tab=` ya no cambia nada: siempre la bandeja por ticket
        self.assertNotContains(pagina, reverse("tareas:detalle", args=[self.tarea.pk]))


class BandejaGacetaTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.p_banner = gacetas_ops.presentar(self.gaceta, self.banner, self.analista)

    def test_el_revisor_encuentra_la_gaceta_en_trabajo(self):
        self.assertTrue(bandeja_ops.hay_revision_de_gaceta(self.observador))
        self.client.force_login(self.observador)
        bandeja = self.client.get(reverse("core:mi_trabajo")).context["bandeja"]
        grupo = next(g for g in bandeja["atencion"] if g.ticket.pk == self.ticket.pk)
        self.assertIn(f"gaceta-{self.gaceta.pk}", [p.clave for p in grupo.acciones])
        self.assertIn("Revisión de gaceta", grupo.participacion)
        # El destino Trabajo del dock existe para el revisor.
        self.assertContains(self.client.get(reverse("core:inicio")), reverse("core:mi_trabajo"))

    def test_en_la_gaceta_nadie_tiene_una_aprobacion_pendiente(self):
        self.client.force_login(self.revisor)  # el aprobador del flujo no participa en la gaceta
        bandeja = self.client.get(reverse("core:mi_trabajo")).context["bandeja"]
        self.assertFalse(any(g.ticket.pk == self.ticket.pk for g in bandeja["atencion"]))


# --- Acceso y composición del espacio ------------------------------------------------------------------


class EspacioAccesoTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()

    def test_sin_relacion_con_el_ticket_no_entra(self):
        ajeno = Usuario.objects.create_user("ux1_ajeno@pruebas.orbita")
        self.client.force_login(ajeno)
        self.assertEqual(self.client.get(_espacio(self.ticket)).status_code, 403)

    def test_quien_trabaja_una_tarea_entra_sin_ganar_el_resto_del_ticket(self):
        self.client.force_login(self.persona)
        pagina = self.client.get(_espacio(self.ticket))
        self.assertEqual(pagina.status_code, 200)
        self.assertIsNone(pagina.context["lateral"])  # sin responsable, solicitante ni fechas del ticket
        claves = [clave for clave, _ in pagina.context["pestanas"]]
        self.assertNotIn("informacion", claves)
        self.assertNotIn("proceso", claves)
        # Pedir una pestaña no autorizada cae en «Mi actividad».
        self.assertEqual(self.client.get(_espacio(self.ticket, pestana="informacion")).context["pestana"], "actividad")

    def test_la_unica_accion_se_destaca_con_su_guia(self):
        self.client.force_login(self.persona)
        pagina = self.client.get(_espacio(self.ticket))
        seleccionada = pagina.context["seleccionada"]
        self.assertEqual(seleccionada["clave"], f"tarea-{self.tarea.pk}")
        self.assertIsNotNone(seleccionada["ficha"])  # edita su ítem desde el espacio
        self.assertEqual([c["campo"].clave for c in seleccionada["ficha"]["celdas_valor"]], ["alterno"])
        # UX-4b: la actividad es la misión (sola o en la tabla de su paso).
        self.assertIn(seleccionada, pagina.context["mision"]["acciones"])

    def test_la_ficha_solo_de_su_item(self):
        self.client.force_login(self.persona)
        self.assertEqual(self.client.get(_espacio(self.ticket, elemento=self.elemento.pk)).status_code, 200)
        self.assertEqual(self.client.get(_espacio(self.ticket, elemento=self.ajeno_el.pk)).status_code, 403)

    def test_el_responsable_ve_todas_las_pestanas_que_aplican(self):
        self.client.force_login(self.analista)
        pagina = self.client.get(_espacio(self.ticket))
        claves = [clave for clave, _ in pagina.context["pestanas"]]
        self.assertIn("elementos", claves)
        self.assertIn("informacion", claves)
        self.assertIsNotNone(pagina.context["lateral"])
        self.assertEqual(self.client.get(_espacio(self.ticket, pestana="elementos")).status_code, 200)


class EspacioAccionesConNextTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()
        self.client.force_login(self.persona)

    def test_editar_el_item_vuelve_al_espacio(self):
        destino = _espacio(self.ticket)
        respuesta = self.client.post(reverse("tickets:elemento_guardar", args=[self.ticket.pk, self.elemento.pk]), {
            f"campo_{self.campos['Alterno'].pk}": "Desde el espacio", "next": destino,
        })
        self.assertRedirects(respuesta, destino, fetch_redirect_response=False)

    def test_completar_la_tarea_vuelve_al_espacio(self):
        destino = _espacio(self.ticket)
        respuesta = self.client.post(reverse("tareas:completar", args=[self.tarea.pk]), {"next": destino})
        self.assertRedirects(respuesta, destino, fetch_redirect_response=False)

    def test_un_next_externo_se_ignora(self):
        respuesta = self.client.post(
            reverse("tareas:comentar", args=[self.tarea.pk]), {"contenido": "Hola", "next": "https://otro.example/"}
        )
        self.assertRedirects(respuesta, reverse("tareas:detalle", args=[self.tarea.pk]), fetch_redirect_response=False)


class EspacioGacetaTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.p_banner = gacetas_ops.presentar(self.gaceta, self.banner, self.analista)

    def test_el_revisor_observa_desde_el_espacio(self):
        self.client.force_login(self.observador)
        pagina = self.client.get(_espacio(self.ticket, accion=f"gaceta-{self.gaceta.pk}"))
        self.assertEqual(pagina.status_code, 200)
        self.assertEqual(pagina.context["seleccionada"]["tipo"], "gaceta")
        self.assertIsNone(pagina.context["lateral"])  # ve la gaceta, no el resto del ticket
        # UX-4b: la gaceta es UNA misión con una galería de tarjetas, y el revisor puede observar en la suya.
        self.assertEqual(pagina.context["mision"]["tipo"], "gaceta")
        tarjeta = pagina.context["galeria"]["tarjetas"][0]
        self.assertEqual(tarjeta["elemento"], self.banner)
        self.assertTrue(tarjeta["puede_observar"])
        # Sin audiovisuales la gaceta es una lista compacta (la información protagonista), no tarjetas con portada.
        self.assertEqual(pagina.context["galeria"]["modo"], "datos")
        self.assertContains(pagina, 'class="pi-lista"')
        self.assertNotContains(pagina, 'class="pz pz--')
        destino = _espacio(self.ticket)
        respuesta = self.client.post(
            reverse("gacetas:observar", args=[self.p_banner.pk]), {"texto": "Ajustar color", "next": destino}
        )
        self.assertRedirects(respuesta, destino, fetch_redirect_response=False)

    def test_la_gaceta_muestra_solo_las_columnas_visibles(self):
        entrega = self.columnas["Entrega"]
        self.gaceta.configuracion = {**self.gaceta.configuracion, "columnas_visibles": [entrega.clave]}
        self.gaceta.save(update_fields=["configuracion"])
        self.client.force_login(self.observador)
        pagina = self.client.get(_espacio(self.ticket, accion=f"gaceta-{self.gaceta.pk}"))
        tarjeta = pagina.context["galeria"]["tarjetas"][0]
        self.assertEqual([v["clave"] for v in tarjeta["valores"]], [entrega.clave])
        # Sin configurar (gacetas congeladas antes de UX-4b) se ve todo, como antes.
        self.gaceta.configuracion = {k: v for k, v in self.gaceta.configuracion.items() if k != "columnas_visibles"}
        self.gaceta.save(update_fields=["configuracion"])
        pagina = self.client.get(_espacio(self.ticket, accion=f"gaceta-{self.gaceta.pk}"))
        self.assertEqual(len(pagina.context["galeria"]["tarjetas"][0]["valores"]), len(self.columnas))

    def test_el_revisor_solo_comenta_desde_el_espacio(self):
        self.client.force_login(self.observador)
        pagina = self.client.get(_espacio(self.ticket, accion=f"gaceta-{self.gaceta.pk}"))
        self.assertEqual(pagina.context["mision"]["tipo"], "gaceta")
        tarjeta = pagina.context["galeria"]["tarjetas"][0]
        # Regla de oro: la tarjeta ofrece comentar, nunca decidir.
        self.assertTrue(tarjeta["puede_observar"])
        self.assertIsNone(tarjeta["decision"])
        self.assertEqual(tarjeta["salidas"], [])
        self.assertNotContains(pagina, "Aprobar")
        gacetas_ops.cerrar_periodo(self.gaceta, self.analista)
        # 2026-11-12: sin nada pendiente, la vista de la misión queda VACÍA (la gaceta ya no se pinta debajo).
        pagina = self.client.get(_espacio(self.ticket))
        self.assertContains(pagina, "Nada pendiente para ti ahora")
        self.assertNotContains(pagina, f'id="gaceta-{self.gaceta.pk}"')
        # Pero se abre a propósito con `?gaceta=` (enlaces del Resumen, avisos, «Coordinar»).
        pagina = self.client.get(_espacio(self.ticket) + f"?gaceta={self.gaceta.pk}")
        self.assertContains(pagina, f'id="gaceta-{self.gaceta.pk}"')


# --- Servicio sin flujo y perspectiva del solicitante ----------------------------------------------------


class EspacioServicioYSolicitanteTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_sin_flujo_quien_atiende_resuelve_en_el_espacio(self):
        self.client.force_login(self.responsable_directo)
        pagina = self.client.get(_espacio(self.ticket))
        self.assertNotIn("proceso", [c for c, _ in pagina.context["pestanas"]])
        accion = next(a for a in pagina.context["acciones"] if a["clave"] == "atencion")
        self.assertTrue(accion["puede_resolver"])
        destino = _espacio(self.ticket)
        respuesta = self.client.post(reverse("tickets:resolver", args=[self.ticket.pk]), {"descripcion": "Listo", "next": destino})
        self.assertRedirects(respuesta, destino, fetch_redirect_response=False)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, Ticket.Estado.RESUELTO)

    def test_el_solicitante_responde_una_solicitud_de_informacion(self):
        solicitud = solicitar_informacion(self.ticket, self.responsable_directo, "¿Qué formato prefieres?")
        self.client.force_login(self.solicitante)
        mis = self.client.get(reverse("tickets:mis_tickets"))
        self.assertTrue(next(t for t in mis.context["tickets"] if t.pk == self.ticket.pk).requiere_respuesta)
        pagina = self.client.get(_espacio(self.ticket))
        self.assertEqual(pagina.context["seleccionada"]["clave"], f"solicitud-{solicitud.pk}")
        self.assertIn("informacion", [c for c, _ in pagina.context["pestanas"]])
        self.assertTrue(pagina.context["desde_mis_tickets"])
        destino = _espacio(self.ticket)
        respuesta = self.client.post(
            reverse("tickets:responder_solicitud", args=[self.ticket.pk, solicitud.pk]),
            {"contenido": "PDF, por favor", "next": destino},
        )
        self.assertRedirects(respuesta, destino, fetch_redirect_response=False)
        self.assertEqual(SolicitudInformacion.objects.get(pk=solicitud.pk).estado, SolicitudInformacion.Estado.RESPONDIDA)

    def test_la_navegacion_sigue_la_perspectiva(self):
        self.client.force_login(self.solicitante)
        self.assertEqual(self.client.get(_espacio(self.ticket)).context["nav_item_activo"], "tickets:mis_tickets")
        self.client.force_login(self.responsable_directo)
        self.assertEqual(self.client.get(_espacio(self.ticket)).context["nav_item_activo"], "core:mi_trabajo")

    def test_el_solicitante_sin_pendientes_ve_el_estado(self):
        self.client.force_login(self.solicitante)
        pagina = self.client.get(_espacio(self.ticket))
        self.assertEqual(pagina.context["acciones"], [])
        self.assertTrue(pagina.context["espera"])

    def test_el_solicitante_no_ve_acciones_de_atencion(self):
        self.client.force_login(self.solicitante)
        acciones = self.client.get(_espacio(self.ticket)).context["acciones"]
        self.assertFalse(any(a["tipo"] in ("atencion", "tomar", "entregable") for a in acciones))

    def test_relacion_combina_varias_capacidades(self):
        rel = espacio_ops.relacion(self.responsable_directo, self.ticket)
        self.assertTrue(rel.responsable_individual and rel.consulta)
        self.assertIn("Responsable", rel.roles)


class CompatibilidadUrlsTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)

    def test_las_vistas_anteriores_siguen_respondiendo_y_enlazan_al_espacio(self):
        self.client.force_login(self.responsable_directo)
        pagina = self.client.get(reverse("tickets:trabajo", args=[self.ticket.pk]))
        self.assertEqual(pagina.status_code, 200)
        self.assertContains(pagina, f'href="{_espacio(self.ticket)}"')
        # El detalle aislado se retiró: su URL lleva a la gestión DENTRO del espacio (enlaces ya enviados).
        self.assertRedirects(
            self.client.get(reverse("tickets:detalle", args=[self.ticket.pk])),
            _espacio(self.ticket) + "?vista=resumen",
        )
        self.client.force_login(self.solicitante)
        self.assertEqual(self.client.get(reverse("tickets:seguimiento", args=[self.ticket.pk])).status_code, 200)

    def test_sin_next_las_acciones_vuelven_al_espacio(self):
        self.client.force_login(self.responsable_directo)
        respuesta = self.client.post(
            reverse("tickets:solicitar_informacion", args=[self.ticket.pk]), {"mensaje": "¿Formato?"}
        )
        self.assertRedirects(respuesta, _espacio(self.ticket), fetch_redirect_response=False)

    def test_un_borrador_lleva_a_su_formulario(self):
        from apps.tickets.operaciones import crear_borrador

        borrador = crear_borrador(self.solicitante, self.servicio)
        self.client.force_login(self.solicitante)
        self.assertRedirects(
            self.client.get(_espacio(borrador)), reverse("tickets:borrador", args=[borrador.pk]),
            fetch_redirect_response=False,
        )


# --- UX-2: distribución, filtros y panel de contexto ------------------------------------------------------


class BandejaUx2Tests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    """Mi trabajo muestra SOLO tickets agrupados (fila compacta), con búsqueda y filtro locales."""

    def setUp(self):
        self._escenario()
        self.client.force_login(self.persona)

    def _bandeja(self, **parametros):
        return self.client.get(reverse("core:mi_trabajo"), parametros).context["bandeja"]

    def test_la_fila_trae_la_siguiente_accion_y_el_enlace_al_espacio(self):
        pagina = self.client.get(reverse("core:mi_trabajo"))
        grupo = next(g for g in pagina.context["bandeja"]["atencion"] if g.ticket.pk == self.ticket.pk)
        self.assertEqual(grupo.siguiente.clave, f"tarea-{self.tarea.pk}")
        self.assertContains(pagina, 'class="bt-fila is-atencion')
        self.assertContains(pagina, f'href="{grupo.url}"')

    def test_busqueda_por_codigo_o_servicio(self):
        servicio = self.ticket.detalle_servicio.servicio.nombre
        self.assertIn(self.ticket.pk, [g.ticket.pk for g in self._bandeja(q=servicio[:4].lower())["atencion"]])
        self.assertEqual(self._bandeja(q="zzz-no-existe")["atencion"], [])
        # El total del filtro sigue siendo el real (lo que hay, no lo que coincide).
        self.assertGreaterEqual(self._bandeja(q="zzz-no-existe")["total_atencion"], 1)

    def test_filtro_por_grupo(self):
        self.assertEqual(self._bandeja(ver="seguimiento")["atencion"], [])
        self.assertTrue(self._bandeja(ver="atencion")["atencion"])
        self.assertEqual(self._bandeja(ver="otra-cosa")["ver"], "todos")  # un filtro desconocido no oculta nada

    def test_una_tarea_completada_no_aparece_como_pendiente(self):
        hecha = crear_tarea(titulo="Ya terminada UX2", creada_por=self.persona, usuario_responsable=self.persona)
        Tarea.objects.filter(pk=hecha.pk).update(estado=Tarea.Estado.COMPLETADA)
        bandeja = self._bandeja()
        for grupo in bandeja["atencion"] + bandeja["seguimiento"]:
            self.assertNotIn(f"tarea-{hecha.pk}", [p.clave for p in grupo.pendientes])

    def test_lo_que_no_tiene_ticket_no_se_lista(self):
        crear_tarea(titulo="Independiente UX2", creada_por=self.persona, usuario_responsable=self.persona)
        pagina = self.client.get(reverse("core:mi_trabajo"))
        self.assertNotContains(pagina, "Sin ticket asociado")
        self.assertNotContains(pagina, "Independiente UX2")


class EspacioUx2Tests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    """Tres zonas: el panel derecho solo existe cuando se pide y respeta la misma autorización que la ficha."""

    def setUp(self):
        self._escenario()

    def test_sin_panel_no_se_reserva_espacio(self):
        self.client.force_login(self.persona)
        pagina = self.client.get(_espacio(self.ticket))
        self.assertEqual(pagina.context["panel"], "")
        self.assertNotContains(pagina, "tb-pagina--con-panel")
        self.assertNotContains(pagina, 'class="esp-derecho"')

    def test_panel_de_solicitud_e_historial_solo_para_quien_consulta(self):
        self.client.force_login(self.persona)  # trabaja una tarea, no consulta el ticket
        for panel in ("solicitud", "historial"):
            with self.subTest(panel=panel):
                self.assertEqual(self.client.get(_espacio(self.ticket, panel=panel)).context["panel"], "")
        self.client.force_login(self.analista)  # responsable del ticket
        # El Historial vive en el panel global del ticket (Conversación | Historial), no en el panel lateral.
        pagina = self.client.get(_espacio(self.ticket, panel="historial"))
        self.assertEqual(pagina.context["panel"], "")
        self.assertEqual(pagina.context["panel_global"], "historial")
        pagina = self.client.get(_espacio(self.ticket, panel="solicitud"))
        self.assertEqual(pagina.context["panel"], "solicitud")
        self.assertContains(pagina, "tb-pagina--con-panel")
        self.assertEqual(pagina.context["url_sin_panel"], _espacio(self.ticket))

    def test_panel_de_un_item_exige_ver_su_ficha(self):
        self.client.force_login(self.persona)
        propio = self.client.get(_espacio(self.ticket, panel="elemento", item=self.elemento.pk))
        self.assertEqual(propio.status_code, 200)
        self.assertEqual(propio.context["panel_ficha"]["elemento"].pk, self.elemento.pk)
        self.assertEqual(self.client.get(_espacio(self.ticket, panel="elemento", item=self.ajeno_el.pk)).status_code, 403)

    def test_cerrar_el_panel_conserva_la_pestana(self):
        self.client.force_login(self.analista)
        pagina = self.client.get(_espacio(self.ticket, pestana="elementos", panel="historial"))
        self.assertEqual(pagina.context["url_sin_panel"], _espacio(self.ticket, pestana="elementos"))


class EspacioVariasAccionesTests(_EscenarioAtencionMixin, TestCase):
    def setUp(self):
        self._preparar_escenario()
        tomar_ticket(self.ticket, self.responsable_directo)
        self.ticket.refresh_from_db()

    def test_con_varias_acciones_la_primera_queda_seleccionada(self):
        solicitar_informacion(self.ticket, self.responsable_directo, "¿Algo más?")
        solicitar_informacion(self.ticket, self.responsable_directo, "¿Y esto?")
        self.client.force_login(self.solicitante)
        pagina = self.client.get(_espacio(self.ticket))
        acciones = pagina.context["acciones"]
        self.assertGreater(len(acciones), 1)
        self.assertEqual(pagina.context["seleccionada"]["clave"], acciones[0]["clave"])
        self.assertGreater(len(pagina.context["misiones"]), 1)  # UX-4b: las demás se eligen arriba
        self.assertContains(pagina, 'class="ms-otras"')
        segunda = acciones[1]["clave"]
        self.assertEqual(self.client.get(_espacio(self.ticket, accion=segunda)).context["seleccionada"]["clave"], segunda)

    def test_mis_tickets_pone_primero_lo_que_requiere_respuesta(self):
        solicitar_informacion(self.ticket, self.responsable_directo, "¿Formato?")
        self.client.force_login(self.solicitante)
        secciones = self.client.get(reverse("tickets:mis_tickets")).context["secciones"]
        self.assertEqual(secciones[0][0], "respuesta")
        self.assertIn(self.ticket.pk, [t.pk for t in secciones[0][2]])
        self.assertNotIn(self.ticket.pk, [t.pk for _, _, lista in secciones[1:] for t in lista])

    def test_la_cola_no_reserva_el_resumen(self):
        self.client.force_login(self.responsable_directo)
        pagina = self.client.get(reverse("tickets:cola"))
        self.assertEqual(pagina.status_code, 200)
        self.assertNotContains(pagina, "Pulsa «Ver resumen»")  # sin columna vacía permanente


# --- UX-3: identidad visual (estructura que la sostiene) ---------------------------------------------------


class Ux3PresentacionTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()
        self.client.force_login(self.persona)

    def test_la_fila_de_mi_trabajo_es_un_enlace_sin_boton_por_fila(self):
        pagina = self.client.get(reverse("core:mi_trabajo"))
        grupo = next(g for g in pagina.context["bandeja"]["atencion"] if g.ticket.pk == self.ticket.pk)
        self.assertContains(pagina, f'<a class="bt-fila__enlace" href="{grupo.url}"')
        self.assertNotContains(pagina, "btn btn--primary btn--sm")  # sin botones verdes repetidos

    def test_guardar_y_completar_comparten_la_barra_de_acciones(self):
        pagina = self.client.get(_espacio(self.ticket))
        seleccionada = pagina.context["seleccionada"]
        elemento = seleccionada["elemento"]
        # UX-4b: «Guardar borrador» (sin avanzar) y el avance (guarda y completa) sobre el mismo ítem.
        self.assertContains(pagina, "Guardar borrador")
        self.assertContains(pagina, reverse("tickets:tarea_avanzar", args=[self.ticket.pk, seleccionada["tarea"].pk]))
        self.assertContains(pagina, f'name="elemento" value="{elemento.pk}"')

    def test_junto_a_la_accion_solo_lo_que_falta(self):
        seleccionada = self.client.get(_espacio(self.ticket)).context["seleccionada"]
        self.assertTrue(all(r["cumplido"] is not True for r in seleccionada["requisitos_pendientes"]))
        en_verificacion = {item.texto for item in seleccionada.get("verificaciones") or []}
        self.assertFalse(en_verificacion & {r["texto"] for r in seleccionada["requisitos_pendientes"]})

    def test_sin_monospace_en_los_codigos(self):
        pagina = self.client.get(_espacio(self.ticket))
        self.assertNotContains(pagina, "font-mono")


class VistaPreviaArchivosTests(SimpleTestCase):
    """UX-4b — solo imágenes, video y PDF se muestran en línea, decidido por la extensión; nada ejecutable."""

    def test_clases_y_tipos_en_linea(self):
        from apps.tickets import vista_previa

        self.assertEqual(vista_previa.clase_de("Pieza final.PNG"), "imagen")
        self.assertEqual(vista_previa.clase_de("reel.mov"), "video")
        self.assertEqual(vista_previa.tipo_mime_en_linea("brief.pdf"), "application/pdf")
        for nombre in ("logo.svg", "pagina.html", "script.js", "sin_extension", ""):
            self.assertIsNone(vista_previa.tipo_mime_en_linea(nombre))
            self.assertEqual(vista_previa.clase_de(nombre), "")
