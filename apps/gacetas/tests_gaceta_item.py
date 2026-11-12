"""Bloques 5.1, 5.2 y 5.3 — revisión colaborativa DEL ÍTEM (bloque GACETA dentro del recorrido de cada ítem).

Recorrido por ítem: Producir pieza → Revisar pieza (aprobación interna, devuelve a Producir) → Revisión en gaceta
(GACETA, rama ITEM) → Publicar (actividad con fecha del ítem y evidencia). La gaceta contiene varios ítems, pero
cada uno se revisa por su cuenta. REGLA DE ORO: en la gaceta nadie aprueba; un comentario manda el ítem a
corrección y, al cerrar el periodo, queda aceptado (y su recorrido continúa) lo que no tenga comentarios pendientes.
El Bloque 5.3 protege el cierre: la gaceta no cierra su periodo ni se resuelve mientras falten ítems por llegar.
Reutilizan el escenario de la Fase 3 (`apps.gacetas.tests._EscenarioGacetasMixin`). Se entregan SIN ejecutar.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.catalogo import intenciones
from apps.catalogo.gacetas import configurar_gaceta
from apps.catalogo.models import DefinicionGaceta, ServicioResponsable
from apps.gacetas import integracion, operaciones
from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
from apps.gacetas.tests import _EscenarioGacetasMixin
from apps.tareas.models import Tarea
from apps.tickets import parrilla as parrilla_ops
from apps.tickets.operaciones import cancelar_ticket
from apps.tickets.tests import _crear_servicio_con_formulario
from apps.workflows.integracion import reintentar_recorrido, resolver_aprobacion_workflow
from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaEtapa, InstanciaWorkflow, TareaWorkflow

Estado = ParticipacionItemGaceta.Estado
EN_ESPERA = InstanciaWorkflow.Estado.EN_ESPERA


class _EscenarioGacetaItemMixin(_EscenarioGacetasMixin):
    def _proceso_gaceta_item(self, *, columnas=None, ruta_sobre_gaceta=False, recorrido=None, publicar_cfg=None, **cfg_gaceta):
        """Proceso con la gaceta dentro del recorrido. La gaceta se define antes de publicar el flujo."""
        from apps.catalogo.configuracion_ejecucion import (
            agregar_bloque_operativo,
            conectar_bloques_operativos,
            crear_nueva_version_configuracion,
        )

        servicio, _, _ = _crear_servicio_con_formulario(self.admin, [])
        servicio.tipo = "PROCESO"
        servicio.workflow = self.workflow_e2
        servicio.save(update_fields=["tipo", "workflow", "actualizado_en"])
        responsable = ServicioResponsable.objects.create(servicio=servicio, tipo_responsable="EQUIPO", equipo=self.analitica)
        definicion, cols = self._definir_parrilla(servicio, columnas or self.COLUMNAS_F2)
        self.regla = self._gaceta(servicio, definicion, **cfg_gaceta)
        config = crear_nueva_version_configuracion(servicio, self.admin)

        def agregar(**datos):
            return agregar_bloque_operativo(config, self.admin, **datos)

        agregar(
            fase=self.f_recepcion, tipo="ACTIVIDAD", nombre="Planear",
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.analista.pk},
        )
        caja = agregar(
            fase=self.f_produccion, tipo="RECORRIDO_ITEMS", nombre="Parrilla por pieza",
            configuracion={"coleccion": definicion.clave, **(recorrido or {})},
        )
        producir = agregar(
            fase=self.f_produccion, tipo="ACTIVIDAD", nombre="Producir pieza", contenedor=caja, rama="ITEM",
            configuracion={"tipo_actor": "COLUMNA", "columna": cols["Responsable"].clave},
        )
        revisar = agregar(
            fase=self.f_produccion, tipo="APROBACION", nombre="Revisar pieza", contenedor=caja, rama="ITEM",
            configuracion={
                "modo": "SECUENCIAL", "politica": "",
                "participantes": [{"tipo": "USUARIO", "usuario_id": self.revisor.pk, "equipo_id": None}],
            },
        )
        conectar_bloques_operativos(revisar, producir, self.admin, resultado_aprobacion="DEVUELTA")
        self.paso_gaceta = agregar(
            fase=self.f_produccion, tipo="GACETA", nombre="Revisión en gaceta", contenedor=caja, rama="ITEM",
            configuracion={"gaceta": self.regla.clave},
        )
        agregar(
            fase=self.f_produccion, tipo="ACTIVIDAD", nombre="Publicar", contenedor=caja, rama="ITEM",
            configuracion=publicar_cfg(cols) if publicar_cfg else {
                "tipo_actor": "COLUMNA", "columna": cols["Responsable"].clave,
                "fecha_prevista_campo": f"item.{cols['Entrega'].clave}",
                "fecha_limite_campo": f"item.{cols['Entrega'].clave}", "requiere_evidencia": True,
            },
        )
        agregar(
            fase=self.f_entrega, tipo="ACTIVIDAD", nombre="Cierre",
            configuracion={"tipo_actor": "USUARIO", "usuario_id": self.analista.pk},
        )
        self.config = config
        self._activar(servicio, config)
        self.caja = caja
        return servicio, responsable, definicion, cols

    def _arrancar(self, *, planear=True, **kwargs):
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_gaceta_item(**kwargs)
        self.ticket = self._ticket(self.servicio, self.resp)
        self.gaceta = self._de(self.ticket, self.regla.clave)
        self.banner = self._item(self.ticket, self.columnas, "Banner", responsable=self.disenadora)
        self.video = self._item(self.ticket, self.columnas, "Video", responsable=self.disenador)
        if planear:  # completar «Planear» lanza el recorrido de cada ítem
            self._completar_como(self.ticket, "Planear", self.analista)

    # --- Recorrido interno ---

    def _aprobar_pieza(self, elemento, decision="APROBADA", observacion=""):
        ultima = self._hija(elemento).ejecuciones_etapa.order_by("-orden").first()
        aprobacion = EsquemaAprobacionWorkflow.objects.get(instancia_etapa=ultima).esquema.participaciones.get()
        resolver_aprobacion_workflow(aprobacion, self.revisor, decision=decision, observacion=observacion)

    def _hasta_la_gaceta(self, elemento, productor):
        self._producir(elemento, productor)
        self._aprobar_pieza(elemento)

    def _espera(self, elemento):
        hija = self._hija(elemento)
        return hija, hija.ejecuciones_etapa.order_by("-orden").first()

    def _resultado_publicado(self, elemento):
        return self._hija(elemento).contexto["resultados_bloques"]["gacetas"][self.paso_gaceta.clave]

    def _cerrar_gaceta(self):
        """Quien coordina cierra el periodo: lo que está en revisión sin comentarios queda aceptado."""
        operaciones.cerrar_periodo(GacetaTicket.objects.get(pk=self.gaceta.pk), self.analista)
        self.gaceta.refresh_from_db()

    def _comentar(self, elemento, texto="Ajustar el logo"):
        operaciones.observar(self._participacion(self.gaceta, elemento), self.observador, texto)


class GacetaPorItemEjecucionTests(_EscenarioGacetaItemMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        # La corrección de la gaceta concede «Pieza» a quien corrige (la única vía para editar un ítem aprobado).
        self._arrancar(columnas_editables=["pieza"])

    def test_al_llegar_se_presenta_solo_ese_item_y_su_recorrido_espera(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        hija, ejecucion = self._espera(self.banner)
        self.assertEqual((hija.estado, ejecucion.motivo_espera), (EN_ESPERA, InstanciaEtapa.MotivoEspera.GACETA))
        self.assertEqual(self._bloque_actual(hija), "Revisión en gaceta")
        participacion = self._participacion(self.gaceta, self.banner)
        self.assertEqual((participacion.origen, participacion.estado), (ParticipacionItemGaceta.Origen.FLUJO, Estado.EN_REVISION))
        # El otro ítem sigue produciendo y no entró a la gaceta.
        self.assertEqual(self._bloque_actual(self._hija(self.video)), "Producir pieza")
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta=self.gaceta, elemento=self.video).exists())

    def test_al_cerrar_el_periodo_solo_continua_lo_que_no_tiene_comentarios(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._comentar(self.video)
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Revisión en gaceta")  # nadie aprueba: espera
        self._cerrar_gaceta()
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Publicar")
        self.assertEqual(self._resultado_publicado(self.banner)["resultado"], Estado.APROBADA)
        self.assertEqual(self._bloque_actual(self._hija(self.video)), "Revisión en gaceta")
        self.assertEqual(self._participacion(self.gaceta, self.video).estado, Estado.EN_CORRECCION)
        # La principal sigue esperando el recorrido; la gaceta cerró su periodo pero no se resolvió.
        self.assertEqual(self._bloque_actual(self._principal(self.ticket)), "Parrilla por pieza")
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.PERIODO_CERRADO)

    def test_la_publicacion_se_crea_con_la_fecha_del_item_y_evidencia(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        parrilla_ops.excluir_elemento(self.video, self.analista, "Fuera de la campaña")  # ya no está por llegar
        self._cerrar_gaceta()
        tarea = self._tarea_de(self._hija(self.banner))
        self.assertEqual(tarea.usuario_responsable, self.disenadora)
        self.assertTrue(tarea.requiere_evidencia)
        self.assertIsNotNone(tarea.fecha_limite)

    def test_comentar_manda_a_correccion_y_el_recorrido_sigue_esperando(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._comentar(self.banner)
        participacion = self._participacion(self.gaceta, self.banner)
        self.assertEqual(participacion.estado, Estado.EN_CORRECCION)
        ronda = participacion.rondas.get(numero=1)
        self.assertEqual(ronda.tarea_correccion.usuario_responsable, self.disenadora)
        hija, ejecucion = self._espera(self.banner)
        self.assertEqual((hija.estado, ejecucion.motivo_espera), (EN_ESPERA, InstanciaEtapa.MotivoEspera.GACETA))
        # El otro ítem no cambia.
        self.assertEqual(self._participacion(self.gaceta, self.video).estado, Estado.EN_REVISION)
        self.assertEqual(self._bloque_actual(self._hija(self.video)), "Revisión en gaceta")

    def test_la_correccion_abre_otra_ronda_con_su_version_y_el_cierre_la_acepta(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._comentar(self.banner)
        participacion = self._participacion(self.gaceta, self.banner)
        # Devuelto con un comentario: lo corrige SOLO quien tiene la corrección; quien atiende ya no lo edita.
        with self.assertRaises(ValidationError):
            parrilla_ops.actualizar_valores(self.banner, self.analista, {self.columnas["Pieza"].pk: "Banner v2"})
        parrilla_ops.actualizar_valores(self.banner, self.disenadora, {self.columnas["Pieza"].pk: "Banner v2"})
        operaciones.reenviar(participacion, self.disenadora)
        participacion.refresh_from_db()
        self.assertEqual((participacion.ronda, participacion.estado), (2, Estado.EN_REVISION))
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Revisión en gaceta")  # sigue esperando
        self._cerrar_gaceta()
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Publicar")
        publicado = self._resultado_publicado(self.banner)
        self.assertEqual((publicado["ronda"], publicado["version"]), (2, 2))

    def test_la_correccion_que_llega_con_el_periodo_cerrado_queda_aceptada_al_reenviarse(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._comentar(self.banner)
        self._cerrar_gaceta()
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Revisión en gaceta")
        operaciones.reenviar(self._participacion(self.gaceta, self.banner), self.disenadora)
        self.assertEqual(self._participacion(self.gaceta, self.banner).estado, Estado.APROBADA)
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Publicar")

    def test_excluir_termina_solo_ese_recorrido_y_el_ultimo_libera_la_principal(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        operaciones.excluir_directo(self._participacion(self.gaceta, self.banner), self.analista, "Se cayó la pauta")
        self.assertEqual(self._hija(self.banner).contexto["resultado_recorrido"], "EXCLUIDO")
        self.assertEqual(self._hija(self.video).estado, EN_ESPERA)
        self._cerrar_gaceta()
        completar = self._tarea_de(self._hija(self.video))
        Tarea.objects.filter(pk=completar.pk).update(requiere_evidencia=False)  # la evidencia no es lo que se prueba
        self._producir(self.video, self.disenador)
        principal = self._principal(self.ticket)
        self.assertEqual(self._bloque_actual(principal), "Cierre")
        resultado = principal.contexto["resultados_bloques"]["recorridos"][self.caja.clave]
        self.assertEqual((resultado["completados"], resultado["excluidos"]), (1, 1))

    def test_un_aviso_repetido_no_duplica_tareas_ni_transiciones(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._cerrar_gaceta()
        participacion = self._participacion(self.gaceta, self.banner)
        hija = self._hija(self.banner)
        ejecuciones = hija.ejecuciones_etapa.count()
        participacion.refresh_from_db()
        participacion.gaceta = self.gaceta
        self.assertEqual(integracion.continuar_por_item(participacion), 0)
        self.assertEqual(hija.ejecuciones_etapa.count(), ejecuciones)
        self.assertEqual(
            TareaWorkflow.objects.filter(instancia_etapa__instancia_workflow=hija, instancia_etapa__bloque_operativo__nombre="Publicar").count(),
            1,
        )

    def test_reentrar_al_paso_no_duplica_la_participacion(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        esperar, datos = integracion.entrar_item(self.ticket, self.paso_gaceta, self.banner)
        self.assertTrue(esperar)
        self.assertEqual(datos["resultado"], Estado.EN_REVISION)
        self.assertEqual(ParticipacionItemGaceta.objects.filter(gaceta=self.gaceta, elemento=self.banner).count(), 1)
        self.assertEqual(self._participacion(self.gaceta, self.banner).rondas.count(), 1)

    def test_cancelar_el_ticket_detiene_el_recorrido_que_espera_la_gaceta(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        cancelar_ticket(self.ticket, self.analista, "Se canceló la campaña")
        self.assertEqual(self._hija(self.banner).estado, InstanciaWorkflow.Estado.CANCELADA)
        self.gaceta.refresh_from_db()
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.CANCELADA)


class GacetaPorItemIngresoTests(_EscenarioGacetaItemMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()

    def test_un_item_aprobado_e_incompleto_queda_detenido_y_ya_no_se_completa(self):
        columnas = (*self.COLUMNAS_F2, ("Pieza final", "TEXTO", {}, True))
        self._arrancar(columnas=columnas)
        self._hasta_la_gaceta(self.banner, self.disenadora)
        hija, ejecucion = self._espera(self.banner)
        self.assertEqual(hija.estado, InstanciaWorkflow.Estado.ERROR)
        self.assertEqual(ejecucion.error["tipo"], "FUNCIONAL")
        self.assertIn("incompleto", ejecucion.error["mensaje"])
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta=self.gaceta, elemento=self.banner).exists())
        # La principal no se libera con un recorrido detenido.
        self.assertEqual(self._bloque_actual(self._principal(self.ticket)), "Parrilla por pieza")
        # Regla vigente (`apps.tickets.en_revision`): ya aprobado, nadie lo edita por la colección; solo se corrige
        # si lo devuelven. Reintentar sin cambios lo deja detenido igual (la salida es excluirlo del alcance).
        from apps.tickets import en_revision

        with self.assertRaisesMessage(ValidationError, en_revision.MENSAJE_APROBADO):
            parrilla_ops.actualizar_valores(
                self.banner, self.analista, {self.columnas["Pieza final"].pk: "https://cdn/banner.png"}
            )
        self.assertNotIn(self.columnas["Pieza final"].clave, parrilla_ops.valores_de(self.banner))
        reintentar_recorrido(hija, self.analista)
        hija, ejecucion = self._espera(self.banner)
        self.assertEqual(hija.estado, InstanciaWorkflow.Estado.ERROR)
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta=self.gaceta, elemento=self.banner).exists())

    def test_una_gaceta_opcional_se_salta_si_no_se_cumple_la_condicion_y_lo_registra(self):
        self._arrancar(condicion_ingreso={"variable": "item.pieza", "operador": "IGUAL_A", "valor": "Banner"})
        self._hasta_la_gaceta(self.video, self.disenador)
        self.assertEqual(self._bloque_actual(self._hija(self.video)), "Publicar")
        self.assertEqual(self._resultado_publicado(self.video)["resultado"], integracion.NO_INGRESA)
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta=self.gaceta, elemento=self.video).exists())

    def test_una_gaceta_obligatoria_nunca_se_omite_por_la_condicion(self):
        self._arrancar(
            obligatoria=True, condicion_ingreso={"variable": "item.pieza", "operador": "IGUAL_A", "valor": "Banner"},
        )
        self._hasta_la_gaceta(self.video, self.disenador)
        hija, ejecucion = self._espera(self.video)
        self.assertEqual(hija.estado, InstanciaWorkflow.Estado.ERROR)
        self.assertIn("obligatoria", ejecucion.error["mensaje"])

    def test_una_gaceta_resuelta_no_recibe_el_item_y_lo_dice(self):
        self._arrancar()
        # Desde el Bloque 5.3 ya no se resuelve con ítems por llegar; aquí se simula una gaceta resuelta antes.
        GacetaTicket.objects.filter(pk=self.gaceta.pk).update(estado=GacetaTicket.Estado.RESUELTA)
        self._hasta_la_gaceta(self.banner, self.disenadora)
        hija, ejecucion = self._espera(self.banner)
        self.assertEqual(hija.estado, InstanciaWorkflow.Estado.ERROR)
        self.assertIn("ya se resolvió", ejecucion.error["mensaje"])

    def test_la_gaceta_por_flujo_se_abre_cuando_llega_el_primer_item(self):
        self._arrancar(apertura="POR_FLUJO")
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.PENDIENTE)
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self.gaceta.refresh_from_db()
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.ABIERTA)


class GacetaPorItemConfiguracionTests(_EscenarioGacetaItemMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()

    def _errores(self, config):
        from apps.catalogo.configuracion_ejecucion import validar_configuracion_ejecucion

        return " ".join(validar_configuracion_ejecucion(config))

    def _borrador(self, servicio):
        from apps.catalogo.configuracion_ejecucion import preparar_configuracion_ejecucion

        # Copia de la configuración activa (con el recorrido y su gaceta) en la que se edita.
        return preparar_configuracion_ejecucion(servicio, self.admin)

    def test_el_flujo_valido_se_publica(self):
        servicio, *_ = self._proceso_gaceta_item()
        servicio.refresh_from_db()
        self.assertEqual(self.config.__class__.objects.get(pk=self.config.pk).estado, "ACTIVA")

    def test_la_gaceta_debe_incluir_la_coleccion_recorrida(self):
        servicio, *_ = self._proceso_gaceta_item()
        DefinicionGaceta.objects.filter(pk=self.regla.pk).update(
            configuracion={**self.regla.configuracion, "colecciones": []}
        )
        self.assertIn("no incluye la colección", self._errores(self._borrador(servicio)))

    def test_no_admite_resolucion_automatica_ni_ingreso_automatico(self):
        servicio, *_ = self._proceso_gaceta_item()
        DefinicionGaceta.objects.filter(pk=self.regla.pk).update(configuracion={
            **self.regla.configuracion, "plazo_revision": {"cantidad": 3, "unidad": "DIAS", "habiles": False},
            "resolucion_automatica": True, "ingreso": "AL_CUMPLIR_CONDICION",
        })
        errores = self._errores(self._borrador(servicio))
        for texto in ("resolverse automáticamente", "no automáticamente"):
            self.assertIn(texto, errores)
        # Demo Imperandina: el periodo compartido con silencio positivo sí se admite.
        for texto in ("plazo de revisión", "aceptar ítems automáticamente"):
            self.assertNotIn(texto, errores)

    def test_editar_una_gaceta_en_uso_por_item_admite_periodo_y_rechaza_la_resolucion_automatica(self):
        servicio, *_ = self._proceso_gaceta_item()
        periodo = {"cantidad": 7, "unidad": "DIAS", "habiles": False}
        configurar_gaceta(
            servicio, self.admin, nombre=self.regla.nombre, definicion=self.regla,
            configuracion={**self.regla.configuracion, "plazo_revision": periodo},
        )
        self.regla.refresh_from_db()
        self.assertEqual(self.regla.configuracion["plazo_revision"], periodo)
        with self.assertRaises(ValidationError):
            configurar_gaceta(
                servicio, self.admin, nombre=self.regla.nombre, definicion=self.regla,
                configuracion={**self.regla.configuracion, "resolucion_automatica": True},
            )

    def test_una_ruta_que_vuelve_antes_de_la_gaceta_superada_se_rechaza(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, conectar_bloques_operativos

        servicio, *_ = self._proceso_gaceta_item()
        config = self._borrador(servicio)
        caja = config.bloques.get(tipo="RECORRIDO_ITEMS")
        producir = config.bloques.get(nombre="Producir pieza")
        validar = agregar_bloque_operativo(
            config, self.admin, fase=caja.fase, tipo="APROBACION", nombre="Validar publicación", contenedor=caja,
            rama="ITEM", configuracion={
                "modo": "SECUENCIAL", "politica": "",
                "participantes": [{"tipo": "USUARIO", "usuario_id": self.revisor.pk, "equipo_id": None}],
            },
        )
        self.assertNotIn("vuelve antes de la revisión", self._errores(config))
        conectar_bloques_operativos(validar, producir, self.admin, resultado_aprobacion="DEVUELTA")
        self.assertIn("vuelve antes de la revisión", self._errores(config))

    def test_la_misma_gaceta_dos_veces_en_un_recorrido_se_rechaza(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        servicio, *_ = self._proceso_gaceta_item()
        config = self._borrador(servicio)
        caja = config.bloques.get(tipo="RECORRIDO_ITEMS")
        agregar_bloque_operativo(
            config, self.admin, fase=caja.fase, tipo="GACETA", nombre="Otra vez", contenedor=caja, rama="ITEM",
            configuracion={"gaceta": self.regla.clave},
        )
        self.assertIn("dos veces en la misma gaceta", self._errores(config))

    def test_la_gaceta_no_va_en_una_rama_general(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        servicio, *_ = self._proceso_gaceta_item()
        config = self._borrador(servicio)
        caja = config.bloques.get(tipo="RECORRIDO_ITEMS")
        with self.assertRaises(ValidationError):
            agregar_bloque_operativo(
                config, self.admin, fase=caja.fase, tipo="GACETA", nombre="General", contenedor=caja, rama="GENERAL",
                configuracion={"gaceta": self.regla.clave},
            )

    # --- Bloque 5.4 (complemento del 5.3): espera circular con una gaceta del flujo principal ---

    def _con_gaceta_principal(self, *, fase, esperar="RESOLUCION", clave=None):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        servicio, *_ = self._proceso_gaceta_item()
        config = self._borrador(servicio)
        espera = agregar_bloque_operativo(
            config, self.admin, fase=fase, tipo="GACETA", nombre="Esperar la gaceta",
            configuracion={"gaceta": clave or self.regla.clave, "presentar_items": False, "esperar": esperar},
        )
        return servicio, config, espera

    def test_una_espera_de_la_gaceta_antes_del_recorrido_se_rechaza_y_nombra_los_pasos(self):
        _servicio, config, _espera = self._con_gaceta_principal(fase=self.f_recepcion)  # tras «Planear»
        errores = self._errores(config)
        for texto in ("«Esperar la gaceta»", "«Revisión en gaceta»", "«Parrilla por pieza»", "antes de terminar el recorrido"):
            self.assertIn(texto, errores)

    def test_una_espera_de_la_gaceta_despues_del_recorrido_es_valida(self):
        _servicio, config, _espera = self._con_gaceta_principal(fase=self.f_entrega)
        self.assertNotIn("antes de terminar el recorrido", self._errores(config))

    def test_una_gaceta_principal_que_no_espera_es_valida_aunque_vaya_antes(self):
        _servicio, config, _espera = self._con_gaceta_principal(fase=self.f_recepcion, esperar="NINGUNA")
        self.assertNotIn("antes de terminar el recorrido", self._errores(config))

    def test_otra_gaceta_en_el_flujo_principal_no_genera_el_ciclo(self):
        servicio, *_ = self._proceso_gaceta_item()
        otra = self._gaceta(servicio, self._definicion_de(servicio), nombre="Otra gaceta")
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

        config = self._borrador(servicio)
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_recepcion, tipo="GACETA", nombre="Esperar la otra",
            configuracion={"gaceta": otra.clave, "presentar_items": False, "esperar": "RESOLUCION"},
        )
        self.assertNotIn("antes de terminar el recorrido", self._errores(config))

    def test_una_ruta_que_salta_el_recorrido_hasta_la_espera_se_rechaza(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, conectar_bloques_operativos

        _servicio, config, espera = self._con_gaceta_principal(fase=self.f_entrega)
        caja = config.bloques.get(tipo="RECORRIDO_ITEMS")
        decision = agregar_bloque_operativo(
            config, self.admin, fase=self.f_recepcion, tipo="DECISION", nombre="¿Hay piezas?",
        )
        conectar_bloques_operativos(decision, espera, self.admin, variable="ticket.estado", operador="IGUAL_A", valor="X")
        conectar_bloques_operativos(decision, caja, self.admin, es_fallback=True)
        self.assertIn("antes de terminar el recorrido", self._errores(config))

    def test_la_publicacion_se_rechaza_con_el_ciclo(self):
        from apps.catalogo.configuracion_ejecucion import activar_configuracion_ejecucion

        servicio, config, _espera = self._con_gaceta_principal(fase=self.f_recepcion)
        with self.assertRaises(ValidationError):
            activar_configuracion_ejecucion(servicio, config, self.admin)

    def _definicion_de(self, servicio):
        from apps.catalogo.models import DefinicionParrilla

        return DefinicionParrilla.objects.filter(servicio=servicio).order_by("orden", "pk").first()

    def test_el_studio_la_ofrece_solo_dentro_del_recorrido_del_item(self):
        servicio, *_ = self._proceso_gaceta_item()

        def codigos(ambito):
            return {f["opcion"].codigo for i in intenciones.opciones_para(servicio, ambito=ambito) for f in i["opciones"]}

        self.assertIn("gaceta_item", codigos(intenciones.RAMA_ITEM))
        self.assertNotIn("gaceta", codigos(intenciones.RAMA_ITEM))
        self.assertIn("gaceta", codigos(intenciones.PRINCIPAL))
        self.assertNotIn("gaceta_item", codigos(intenciones.PRINCIPAL))
        self.assertEqual(intenciones.traducir("gaceta_item"), "GACETA")

    def test_el_formulario_por_item_solo_pide_la_gaceta(self):
        from apps.catalogo.forms import GacetaBloqueForm

        opciones = [("piezas", "Gaceta de piezas")]
        form = GacetaBloqueForm({"gaceta": "piezas"}, gacetas=opciones, por_item=True)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.configuracion(), {"gaceta": "piezas"})
        general = GacetaBloqueForm({"gaceta": "piezas", "esperar": "NINGUNA"}, gacetas=opciones)
        self.assertTrue(general.is_valid(), general.errors)
        self.assertEqual(general.configuracion(), {"gaceta": "piezas", "presentar_items": False, "esperar": "NINGUNA"})


class GacetaPorItemCierreTests(_EscenarioGacetaItemMixin, TestCase):
    """Bloque 5.3 — la gaceta usada como paso por ítem no se cierra antes de que lleguen sus ítems."""

    def setUp(self):
        self._preparar_gacetas()

    def _motivos(self, operacion):
        with self.assertRaises(ValidationError) as ctx:
            operacion()
        return " ".join(ctx.exception.messages)

    def _resolver(self):
        return operaciones.resolver(self.gaceta, self.analista)

    def _cerrar_periodo(self):
        return operaciones.cerrar_periodo(self.gaceta, self.analista)

    def _estado_gaceta(self):
        self.gaceta.refresh_from_db()
        return self.gaceta.estado

    def test_con_b_en_produccion_no_se_resuelve_y_a_espera_en_la_gaceta(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        # Nadie aprueba en la gaceta: A espera el cierre del periodo.
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Revisión en gaceta")
        self.assertEqual(self._bloque_actual(self._hija(self.video)), "Producir pieza")
        motivos = self._motivos(self._resolver)
        self.assertIn("todavía faltan ítems por llegar", motivos)
        self.assertIn(f"#{self.video.numero}", motivos)
        self.assertEqual(integracion.llegadas_pendientes(self.gaceta).por_llegar, (self.video.numero,))
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.ABIERTA)  # ni siquiera cerró el periodo

    def test_cerrar_el_periodo_con_b_por_llegar_tambien_se_bloquea(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self.assertIn("cerrar su periodo", self._motivos(self._cerrar_periodo))
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.ABIERTA)

    def test_b_excluido_antes_de_llegar_deja_de_bloquear(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        parrilla_ops.excluir_elemento(self.video, self.analista, "Se cayó la pauta")
        self.assertEqual(integracion.bloqueos_de_cierre(self.gaceta, resolver=True), [])
        self._resolver()
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.RESUELTA)
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Publicar")  # resolver cerró y la aceptó

    def test_b_rechazado_antes_de_llegar_deja_de_bloquear(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        parrilla_ops.rechazar_elemento(self.video, self.analista, "No va en esta campaña")
        self._resolver()
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.RESUELTA)

    def test_b_en_correccion_impide_resolver_pero_no_cerrar_el_periodo(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._comentar(self.video, "Ajustar el corte")
        motivos = self._motivos(self._resolver)
        self.assertIn("en corrección", motivos)
        self.assertNotIn("faltan ítems por llegar", motivos)
        self._cerrar_periodo()  # ya llegaron todos: cerrar los comentarios sí se puede
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.PERIODO_CERRADO)
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Publicar")
        operaciones.reenviar(self._participacion(self.gaceta, self.video), self.disenador)
        self.assertEqual(self._bloque_actual(self._hija(self.video)), "Publicar")
        self._resolver()
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.RESUELTA)

    def test_b_en_correccion_impide_resolver_aunque_la_regla_excluya_lo_pendiente(self):
        self._arrancar(al_resolver_pendientes="EXCLUIR")
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._comentar(self.video, "Ajustar el corte")
        self.assertIn("en corrección", self._motivos(self._resolver))
        self.assertEqual(self._participacion(self.gaceta, self.video).estado, Estado.EN_CORRECCION)

    def test_un_ingreso_fallido_sigue_contando_como_por_llegar(self):
        self._arrancar(columnas=(*self.COLUMNAS_F2, ("Pieza final", "TEXTO", {}, True)))
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self.assertEqual(self._hija(self.banner).estado, InstanciaWorkflow.Estado.ERROR)
        parrilla_ops.excluir_elemento(self.video, self.analista, "Fuera")
        self.assertIn(f"#{self.banner.numero}", self._motivos(self._resolver))

    def test_con_el_alcance_abierto_y_un_recorrido_que_admite_nuevos_no_se_resuelve(self):
        self._arrancar(recorrido={"incorporar_nuevos": True})
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        motivos = self._motivos(self._resolver)
        self.assertIn("sigue abierto", motivos)
        self.assertNotIn("faltan ítems por llegar", motivos)
        parrilla_ops.cerrar_alcance(self.ticket, self.analista, clave=self.definicion.clave)
        self._resolver()
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.RESUELTA)

    def test_un_item_nuevo_incorporado_tambien_debe_llegar(self):
        self._arrancar(recorrido={"incorporar_nuevos": True})
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        audio = self._item(self.ticket, self.columnas, "Audio", responsable=self.disenadora)
        self.assertEqual(self._bloque_actual(self._hija(audio)), "Producir pieza")
        parrilla_ops.cerrar_alcance(self.ticket, self.analista, clave=self.definicion.clave)
        self.assertIn(f"#{audio.numero}", self._motivos(self._resolver))

    def test_con_el_alcance_cerrado_y_todo_procesado_se_resuelve(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        parrilla_ops.cerrar_alcance(self.ticket, self.analista, clave=self.definicion.clave)
        self._resolver()
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.RESUELTA)

    def test_un_alcance_abierto_no_bloquea_si_el_recorrido_no_admite_items_nuevos(self):
        # Sin `incorporar_nuevos`, un ítem agregado ya no entraría a ningún recorrido de esta gaceta.
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._resolver()
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.RESUELTA)

    def test_antes_de_lanzar_el_recorrido_todos_los_items_estan_por_llegar(self):
        self._arrancar(planear=False)
        motivos = self._motivos(self._resolver)
        self.assertIn(f"#{self.banner.numero}, #{self.video.numero}", motivos)
        self.assertIn("sigue abierto", motivos)

    def test_cancelar_el_ticket_no_se_bloquea(self):
        self._arrancar()
        self._hasta_la_gaceta(self.banner, self.disenadora)
        cancelar_ticket(self.ticket, self.analista, "Se canceló la campaña")
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.CANCELADA)
        self.assertEqual(self._hija(self.video).estado, InstanciaWorkflow.Estado.CANCELADA)

    def test_la_vista_muestra_el_motivo_y_no_resuelve(self):
        from django.urls import reverse

        self._arrancar()
        self.client.force_login(self.analista)
        respuesta = self.client.post(reverse("gacetas:resolver", args=[self.gaceta.pk]), follow=True)
        self.assertContains(respuesta, "todavía faltan ítems por llegar")
        self.assertEqual(self._estado_gaceta(), GacetaTicket.Estado.ABIERTA)


class GacetaGeneralCompatibilidadTests(_EscenarioGacetasMixin, TestCase):
    """El bloque GACETA del flujo principal conserva sus opciones y su comportamiento."""

    def setUp(self):
        self._preparar_gacetas()
        self.servicio, self.resp, self.definicion, self.columnas = self._proceso_simple()

    def test_aceptar_en_una_gaceta_general_no_reanuda_ningun_recorrido(self):
        regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio, bloque_gaceta={"gaceta": regla.clave, "presentar_items": True, "esperar": "RESOLUCION"})
        ticket = self._ticket(self.servicio, self.resp)
        banner = self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        self._completar_como(ticket, "Planear", self.analista)
        gaceta = self._de(ticket, regla.clave)
        participacion = self._participacion(gaceta, banner)
        operaciones.cerrar_periodo(gaceta, self.analista)
        participacion.refresh_from_db()
        self.assertEqual(participacion.estado, Estado.APROBADA)
        self.assertEqual(self._bloque_actual(self._principal(ticket)), "Revisión en gaceta")  # espera la resolución
        participacion.gaceta = gaceta
        self.assertEqual(integracion.continuar_por_item(participacion), 0)

    def test_la_gaceta_general_sigue_validando_su_espera_y_admite_periodo(self):
        from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo, crear_nueva_version_configuracion
        from apps.catalogo.configuracion_ejecucion import validar_configuracion_ejecucion

        regla = self._gaceta(
            self.servicio, self.definicion, plazo_revision={"cantidad": 3, "unidad": "DIAS", "habiles": False},
        )
        config = crear_nueva_version_configuracion(self.servicio, self.admin)
        agregar_bloque_operativo(
            config, self.admin, fase=self.f_recepcion, tipo="GACETA", nombre="General",
            configuracion={"gaceta": regla.clave, "presentar_items": True, "esperar": "OTRA"},
        )
        errores = " ".join(validar_configuracion_ejecucion(config))
        self.assertIn("espera no válida", errores)
        self.assertNotIn("plazo de revisión", errores)

    def test_la_gaceta_general_no_tiene_los_bloqueos_del_cierre_por_item(self):
        regla = self._gaceta(self.servicio, self.definicion)
        self._activar_flujo(self.servicio, bloque_gaceta={"gaceta": regla.clave, "presentar_items": True, "esperar": "NINGUNA"})
        ticket = self._ticket(self.servicio, self.resp)
        self._item(ticket, self.columnas, "Banner", responsable=self.disenadora)
        gaceta = self._de(ticket, regla.clave)
        self.assertFalse(integracion.llegadas_pendientes(gaceta).usada_por_item)
        self.assertEqual(integracion.bloqueos_de_cierre(gaceta, resolver=True), [])
        operaciones.cerrar_periodo(gaceta, self.analista)  # como antes: sin ítems presentados, cierra y resuelve
        operaciones.resolver(gaceta, self.analista)
        gaceta.refresh_from_db()
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.RESUELTA)


class GacetaPorItemSilencioPositivoTests(_EscenarioGacetaItemMixin, TestCase):
    """Demo Imperandina — periodo COMPARTIDO de la gaceta por ítem (silencio positivo): corre desde la primera pieza
    (apertura POR_FLUJO) sin reiniciarse, no cierra mientras falten piezas por llegar (Bloque 5.3: la ventana se
    extiende hasta la última) y al cerrar acepta lo que está en revisión sin comentarios pendientes; lo que está en
    corrección queda aceptado al reenviarse (nadie aprueba en una gaceta)."""

    def setUp(self):
        self._preparar_gacetas()
        self._arrancar(
            apertura="POR_FLUJO", plazo_revision={"cantidad": 7, "unidad": "DIAS", "habiles": False},
        )

    def _vencer(self):
        from datetime import timedelta

        from django.utils import timezone

        GacetaTicket.objects.filter(pk=self.gaceta.pk).update(vence_revision_en=timezone.now() - timedelta(minutes=1))

    def _reconciliar(self):
        from apps.gacetas.tasks import reconciliar

        reconciliar()
        self.gaceta.refresh_from_db()

    def test_la_primera_pieza_abre_y_la_segunda_no_reinicia_el_plazo(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self.gaceta.refresh_from_db()
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.ABIERTA)
        vence = self.gaceta.vence_revision_en
        self.assertIsNotNone(vence)
        self._hasta_la_gaceta(self.video, self.disenador)
        self.gaceta.refresh_from_db()
        self.assertEqual(self.gaceta.vence_revision_en, vence)

    def test_al_vencer_espera_a_la_ultima_pieza_y_luego_acepta_por_silencio(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        self._vencer()
        self._reconciliar()  # el video aún no llega: la ventana se extiende, sin error
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.ABIERTA)
        self.assertEqual(self._participacion(self.gaceta, self.banner).estado, Estado.EN_REVISION)
        self._hasta_la_gaceta(self.video, self.disenador)
        self._reconciliar()
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.PERIODO_CERRADO)
        for elemento in (self.banner, self.video):
            participacion = self._participacion(self.gaceta, elemento)
            self.assertEqual(participacion.estado, Estado.APROBADA)
            self.assertTrue(participacion.aceptada_automaticamente)
            self.assertEqual(self._bloque_actual(self._hija(elemento)), "Publicar")

    def test_una_pieza_con_comentario_no_se_acepta_sola_y_queda_aceptada_al_reenviarse(self):
        self._hasta_la_gaceta(self.banner, self.disenadora)
        operaciones.observar(self._participacion(self.gaceta, self.banner), self.observador, "Ajustar el color")
        self._hasta_la_gaceta(self.video, self.disenador)
        self._vencer()
        self._reconciliar()
        self.assertEqual(self.gaceta.estado, GacetaTicket.Estado.PERIODO_CERRADO)
        self.assertEqual(self._participacion(self.gaceta, self.video).estado, Estado.APROBADA)
        banner = self._participacion(self.gaceta, self.banner)
        self.assertEqual(banner.estado, Estado.EN_CORRECCION)  # nunca se aprueba sola con pendientes
        self.assertFalse(banner.aceptada_automaticamente)
        self.assertEqual(self._espera(self.banner)[1].motivo_espera, InstanciaEtapa.MotivoEspera.GACETA)
        operaciones.reenviar(banner, self.disenadora)
        self.assertEqual(self._participacion(self.gaceta, self.banner).estado, Estado.APROBADA)
        self.assertEqual(self._bloque_actual(self._hija(self.banner)), "Publicar")
