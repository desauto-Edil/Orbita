"""Bloque 3.1 — edición de un ítem por la tarea abierta que lo trabaja.

El responsable de una tarea ABIERTA asociada a un ítem (actividad de un recorrido por ítem, tarea por elemento o
corrección de gaceta) edita solo las columnas que esa tarea autoriza y solo en ESE ítem; no se vuelve responsable
del ticket ni participante de la colección. Completar, cancelar o reasignar la tarea revoca la edición. Reutilizan
los escenarios de `apps.tickets.tests` y `apps.gacetas.tests`. Se entregan SIN ejecutar.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.catalogo import actividades
from apps.catalogo.tareas_elemento import configurar_tareas_elemento
from apps.core.models import MiembroEquipo, RegistroAuditoria
from apps.gacetas import operaciones as gacetas_ops
from apps.gacetas.tests import _EscenarioGacetasMixin
from apps.tareas.models import Tarea
from apps.tareas.operaciones import cancelar_tarea
from apps.tickets import parrilla as parrilla_ops
from apps.tickets import tareas_elemento as tareas_ops
from apps.tickets.autorizacion import (
    puede_consultar_coleccion,
    puede_consultar_ticket,
    puede_editar_parrilla,
    puede_operar_coleccion,
)
from apps.tickets.edicion_por_tarea import columnas_por_tarea, elemento_de_tarea
from apps.tickets.models import ConfiguracionTareasTicket
from apps.tickets.tests import (
    _EscenarioRecorridosMixin,
    _EscenarioTareasElementoMixin,
    _MediaAisladaMixin,
)


class _TareasConEdicionMixin(_EscenarioTareasElementoMixin):
    COLUMNAS = _EscenarioTareasElementoMixin.COLUMNAS + (("Soporte", "ARCHIVO", {"extensiones_permitidas": ["pdf"]}),)
    EDITABLES = ["alterno", "soporte"]

    def _escenario(self):
        self._preparar_revision()
        self.ticket = self._ticket_tareas(columnas_editables=self.EDITABLES)
        self.elemento = self._el(self.ticket, tema="Reel")
        self.ajeno_el = self._el(self.ticket, tema="Carrusel", responsable=self.otro)
        creadas = {v.elemento_id: v.tarea for v in tareas_ops.materializar_tareas(self.ticket, self.analista).creadas}
        self.tarea, self.tarea_ajena = creadas[self.elemento.pk], creadas[self.ajeno_el.pk]

    def _pdf(self, nombre="soporte.pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(nombre, b"%PDF-1.4 contenido", content_type="application/pdf")


class TareaPorElementoPorColumnaTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()

    def test_edita_solo_las_columnas_autorizadas_de_su_elemento(self):
        self.assertEqual(self.tarea.usuario_responsable, self.persona)
        parrilla_ops.actualizar_valores(self.elemento, self.persona, {self.campos["Alterno"]: "Versión corta"})
        self.assertEqual(parrilla_ops.valores_de(self.elemento)["alterno"], "Versión corta")
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.elemento, self.persona, {self.campos["Canal"]: "ig"})

    def test_reenviar_el_valor_actual_de_otra_columna_no_es_un_cambio(self):
        # El formulario de edición puede enviar columnas ajenas con su valor de hoy: no se rechaza.
        parrilla_ops.actualizar_valores(
            self.elemento, self.persona, {self.campos["Tema"]: "Reel", self.campos["Alterno"]: "Otro texto"}
        )

    def test_no_edita_otro_elemento(self):
        self.assertFalse(columnas_por_tarea(self.persona, self.ajeno_el))
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.ajeno_el, self.persona, {self.campos["Alterno"]: "Intruso"})

    def test_no_gana_permisos_generales(self):
        self.assertFalse(puede_editar_parrilla(self.persona, self.ticket))
        self.assertFalse(puede_operar_coleccion(self.persona, self.elemento.parrilla))
        self.assertFalse(puede_consultar_ticket(self.persona, self.ticket))
        self.assertFalse(puede_consultar_coleccion(self.persona, self.elemento.parrilla))
        with self.assertRaises(PermissionDenied):
            parrilla_ops.crear_elemento(self.ticket, self.persona, {})
        with self.assertRaises(PermissionDenied):
            parrilla_ops.retirar_elemento(self.elemento, self.persona)
        with self.assertRaises(PermissionDenied):
            parrilla_ops.cerrar_alcance(self.ticket, self.persona)

    def test_completar_cancelar_o_reasignar_la_tarea_revoca_la_edicion(self):
        Tarea.objects.filter(pk=self.tarea.pk).update(usuario_responsable=self.otro)
        self.assertFalse(columnas_por_tarea(self.persona, self.elemento))
        self.assertTrue(columnas_por_tarea(self.otro, self.elemento))  # ahora es del nuevo responsable
        cancelar_tarea(Tarea.objects.get(pk=self.tarea.pk), None)
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.elemento, self.otro, {self.campos["Alterno"]: "Tarde"})
        Tarea.objects.filter(pk=self.tarea_ajena.pk).update(estado=Tarea.Estado.COMPLETADA)
        self.assertFalse(columnas_por_tarea(self.otro, self.ajeno_el))

    def test_un_equipo_responsable_edita_por_sus_miembros_activos(self):
        Tarea.objects.filter(pk=self.tarea.pk).update(usuario_responsable=None, equipo_responsable=self.equipo_revision)
        parrilla_ops.actualizar_valores(self.elemento, self.miembro, {self.campos["Alterno"]: "Del equipo"})
        MiembroEquipo.objects.filter(equipo=self.equipo_revision, usuario=self.miembro).update(activo=False)
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.elemento, self.miembro, {self.campos["Alterno"]: "Ya no"})

    def test_adjuntos_solo_en_columnas_autorizadas(self):
        adjunto = parrilla_ops.adjuntar_archivo_elemento(self.elemento, self.campos["Soporte"], self.persona, self._pdf())
        parrilla_ops.retirar_archivo_elemento(adjunto, self.persona)
        # Sin «soporte» entre las editables, ni adjunta ni retira.
        adjunto = parrilla_ops.adjuntar_archivo_elemento(self.elemento, self.campos["Soporte"], self.analista, self._pdf())
        ConfiguracionTareasTicket.objects.filter(parrilla=self.elemento.parrilla).update(columnas_editables=["alterno"])
        with self.assertRaises(PermissionDenied):
            parrilla_ops.adjuntar_archivo_elemento(self.elemento, self.campos["Soporte"], self.persona, self._pdf())
        with self.assertRaises(PermissionDenied):
            parrilla_ops.retirar_archivo_elemento(adjunto, self.persona)

    def test_registra_versiones_sin_alterar_las_anteriores(self):
        primera = parrilla_ops.registrar_version(self.elemento, self.analista, "Base")
        parrilla_ops.actualizar_valores(self.elemento, self.persona, {self.campos["Alterno"]: "Cambio"})
        segunda = parrilla_ops.registrar_version(self.elemento, self.persona, "Ajuste de la tarea")
        self.assertEqual((segunda.numero, segunda.creada_por), (2, self.persona))
        primera.refresh_from_db()
        self.assertNotEqual(primera.huella, segunda.huella)  # la foto anterior no cambia
        self.assertTrue(
            RegistroAuditoria.objects.filter(modelo="tickets.elementoparrilla", object_id=self.elemento.pk, usuario=self.persona)
            .exists()
        )

    def test_las_columnas_que_originan_la_tarea_no_se_conceden(self):
        with self.assertRaises(ValidationError):
            configurar_tareas_elemento(self.servicio, self.admin, **{**self.CONFIG, "columnas_editables": ["tema"]})
        with self.assertRaises(ValidationError):
            configurar_tareas_elemento(self.servicio, self.admin, **{**self.CONFIG, "columnas_editables": ["no_existe"]})

    def test_el_ticket_conserva_su_copia_congelada(self):
        congelada = ConfiguracionTareasTicket.objects.get(parrilla=self.elemento.parrilla)
        self.assertEqual(congelada.columnas_editables, self.EDITABLES)
        configurar_tareas_elemento(self.servicio, self.admin, **{**self.CONFIG, "columnas_editables": []})
        self.assertTrue(columnas_por_tarea(self.persona, self.elemento))  # el ticket existente no cambia
        congelada.columnas_editables = []
        with self.assertRaises(ValidationError):
            congelada.save()


class VistasDeEdicionPorTareaTests(_MediaAisladaMixin, _TareasConEdicionMixin, TestCase):
    def setUp(self):
        self._escenario()
        self.client.force_login(self.persona)

    def test_la_tarea_ofrece_editar_su_elemento(self):
        self.assertEqual(elemento_de_tarea(self.tarea, self.persona), self.elemento)
        pagina = self.client.get(reverse("tareas:detalle", args=[self.tarea.pk]))
        self.assertContains(pagina, reverse("tickets:elemento_tarea", args=[self.ticket.pk, self.elemento.pk]))

    def test_la_pantalla_solo_ofrece_las_columnas_autorizadas(self):
        pagina = self.client.get(reverse("tickets:elemento_tarea", args=[self.ticket.pk, self.elemento.pk]))
        self.assertEqual(pagina.status_code, 200)
        ficha = pagina.context["ficha"]
        self.assertEqual([c["campo"].clave for c in ficha["celdas_valor"]], ["alterno"])
        self.assertEqual([c["campo"].clave for c in ficha["celdas_archivo"]], ["soporte"])
        ajena = reverse("tickets:elemento_tarea", args=[self.ticket.pk, self.ajeno_el.pk])
        self.assertEqual(self.client.get(ajena).status_code, 403)

    def test_guardar_ignora_las_columnas_no_autorizadas_del_formulario(self):
        destino = reverse("tickets:elemento_tarea", args=[self.ticket.pk, self.elemento.pk])
        self.client.post(reverse("tickets:elemento_guardar", args=[self.ticket.pk, self.elemento.pk]), {
            f"campo_{self.campos['Alterno'].pk}": "Desde la tarea",
            f"campo_{self.campos['Canal'].pk}": "ig",  # no autorizada: no se lee
            "next": destino,
        })
        valores = parrilla_ops.valores_de(self.elemento)
        self.assertEqual(valores["alterno"], "Desde la tarea")
        self.assertNotIn("canal", valores)

    def test_con_la_tarea_cerrada_la_pantalla_se_niega(self):
        Tarea.objects.filter(pk=self.tarea.pk).update(estado=Tarea.Estado.COMPLETADA)
        pagina = self.client.get(reverse("tickets:elemento_tarea", args=[self.ticket.pk, self.elemento.pk]))
        self.assertEqual(pagina.status_code, 403)


class ActividadDeRecorridoPorColumnaTests(_EscenarioRecorridosMixin, TestCase):
    def setUp(self):
        self._preparar_recorridos()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_recorrido(producir_editables=("Pieza",))
        self.ticket = self._ticket(self.servicio, self.resp)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.video = self._item(self.ticket, self.columnas, "Video", responsable=self.disenador)
        self._completar_como(self.ticket, "Planear", self.analista)

    def test_el_responsable_de_la_actividad_edita_solo_su_item_y_sus_columnas(self):
        parrilla_ops.actualizar_valores(self.banner, self.disenadora, {self.columnas["Pieza"].pk: "Banner v2"})
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.banner, self.disenadora, {self.columnas["Entrega"].pk: "2026-12-01"})
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.video, self.disenadora, {self.columnas["Pieza"].pk: "Ajeno"})

    def test_al_completar_la_actividad_pierde_la_edicion(self):
        self._producir(self.banner, self.disenadora)
        self.assertFalse(columnas_por_tarea(self.disenadora, self.banner))
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.banner, self.disenadora, {self.columnas["Pieza"].pk: "Tarde"})

    def test_una_actividad_fuera_de_un_recorrido_no_puede_conceder_columnas(self):
        with self.assertRaises(ValidationError):
            self._proceso_recorrido(actividad_extra={"columnas_editables": ["pieza"]})

    def test_el_contrato_valida_la_forma_de_las_claves(self):
        self.assertTrue(actividades.errores_de_actividad({"columnas_editables": ["No Válida"]}))
        self.assertEqual(actividades.normalizar({"columnas_editables": ["pieza", "pieza"]})[0]["columnas_editables"], ["pieza"])


class CorreccionDeGacetaPorColumnaTests(_EscenarioGacetasMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()
        self.regla = self._gaceta(self.servicio, self.definicion, columnas_editables=["pieza"])
        self._activar_flujo(self.servicio)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.video = self._item(self.ticket, self.columnas, "Video", responsable=self.disenador)
        self.p_banner = gacetas_ops.presentar(self.gaceta, self.banner, self.analista)
        gacetas_ops.presentar(self.gaceta, self.video, self.analista)

    def test_sin_correccion_abierta_no_hay_edicion(self):
        self.assertFalse(columnas_por_tarea(self.disenadora, self.banner))

    def test_quien_corrige_edita_solo_las_columnas_de_la_gaceta_y_reenvia(self):
        gacetas_ops.observar(self.p_banner, self.observador, "Falta el logo")
        self.assertEqual(self.gaceta.configuracion.get("columnas_editables"), ["pieza"])  # congelada en el ticket
        parrilla_ops.actualizar_valores(self.banner, self.disenadora, {self.columnas["Pieza"].pk: "Banner con logo"})
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.banner, self.disenadora, {self.columnas["Entrega"].pk: "2026-12-01"})
        with self.assertRaises(PermissionDenied):
            parrilla_ops.actualizar_valores(self.video, self.disenadora, {self.columnas["Pieza"].pk: "Ajeno"})
        gacetas_ops.reenviar(self.p_banner, self.disenadora, "Listo")
        self.p_banner.refresh_from_db()
        self.assertEqual(self.p_banner.ronda, 2)  # la ronda nueva fotografía la versión corregida
        self.assertFalse(columnas_por_tarea(self.disenadora, self.banner))  # la corrección terminó

    def test_la_gaceta_valida_las_columnas_contra_sus_colecciones(self):
        with self.assertRaises(ValidationError):
            self._gaceta(self.servicio, self.definicion, nombre="Otra", columnas_editables=["no_existe"])
