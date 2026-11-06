"""Bloque 5.4 — reprogramación de fechas por ítem.

Escenario del Bloque 5.2 (`apps.gacetas.tests_gaceta_item`): por ítem, Producir pieza (sin fechas) → Revisar pieza
→ Revisión en gaceta → Publicar (fecha prevista y límite = columna «Entrega», FECHA). Cambiar «Entrega» mueve las
fechas de la tarea «Publicar» ABIERTA de ese ítem y de ninguna otra. Bloque 5.5: vaciar esa columna con la tarea
abierta se rechaza, y editar el ítem y completar su tarea a la vez se serializa en la instancia principal. Se
entregan SIN ejecutar.
"""

import threading
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.models import RegistroAuditoria
from apps.gacetas.tests_gaceta_item import _EscenarioGacetaItemMixin
from apps.tareas.models import HistorialTarea, Tarea
from apps.tareas.operaciones import cancelar_tarea, reprogramar_tarea
from apps.tickets import parrilla as parrilla_ops
from apps.workflows.integracion import completar_tarea_workflow


def _fin_del_dia(dia):
    """Una FECHA del ítem se toma al final de ese día en la hora local (`actividades.fecha_desde_valor`)."""
    return timezone.make_aware(datetime.combine(dia, datetime.max.time().replace(microsecond=0)))


class _EscenarioReprogramacionMixin(_EscenarioGacetaItemMixin):
    def _hasta_publicar(self, elemento, productor):
        self._hasta_la_gaceta(elemento, productor)
        self._decidir(self._participacion(self.gaceta, elemento), "APROBADA")
        tarea = self._tarea_de(self._hija(elemento))
        self.assertEqual(self._bloque_actual(self._hija(elemento)), "Publicar")
        return tarea

    def _cambiar(self, elemento, columna, valor, actor=None):
        parrilla_ops.actualizar_valores(elemento, actor or self.analista, {self.columnas[columna].pk: valor})

    def _fechas(self, tarea):
        tarea.refresh_from_db()
        return tarea.fecha_prevista, tarea.fecha_limite

    def _reprogramaciones(self, tarea):
        return tarea.historial.filter(tipo_evento=HistorialTarea.TipoEvento.REPROGRAMADA)


class ReprogramacionDeFechasTests(_EscenarioReprogramacionMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()
        self._arrancar()

    def test_la_tarea_abierta_toma_la_nueva_fecha(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 20)),) * 2)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 25)),) * 2)

    def test_adelantar_y_aplazar(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self._cambiar(self.banner, "Entrega", "2026-11-10")  # adelanta
        self.assertEqual(self._fechas(tarea)[1], _fin_del_dia(date(2026, 11, 10)))
        self._cambiar(self.banner, "Entrega", "2026-12-02")  # aplaza
        self.assertEqual(self._fechas(tarea)[1], _fin_del_dia(date(2026, 12, 2)))
        self.assertEqual(self._reprogramaciones(tarea).count(), 2)

    def test_una_tarea_completada_o_cancelada_no_cambia(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        Tarea.objects.filter(pk=tarea.pk).update(requiere_evidencia=False)  # la evidencia no es lo que se prueba
        completar_tarea_workflow(tarea, self.disenadora)
        tarea.refresh_from_db()
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)
        self._cambiar(self.banner, "Entrega", "2026-12-01")
        self.assertEqual(self._fechas(tarea)[1], _fin_del_dia(date(2026, 11, 20)))

        otra = self._hasta_publicar(self.video, self.disenador)
        cancelar_tarea(otra, self.analista, motivo="Prueba")
        self._cambiar(self.video, "Entrega", "2026-12-01")
        self.assertEqual(self._fechas(otra)[1], _fin_del_dia(date(2026, 11, 20)))
        self.assertFalse(self._reprogramaciones(otra).exists())

    def test_dos_publicaciones_con_fechas_distintas_no_se_mezclan(self):
        banner = self._hasta_publicar(self.banner, self.disenadora)
        video = self._hasta_publicar(self.video, self.disenador)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self._cambiar(self.video, "Entrega", "2026-11-28")
        self.assertEqual(self._fechas(banner)[1], _fin_del_dia(date(2026, 11, 25)))
        self.assertEqual(self._fechas(video)[1], _fin_del_dia(date(2026, 11, 28)))
        self.assertEqual(self._reprogramaciones(banner).count(), 1)
        self.assertEqual(self._reprogramaciones(video).count(), 1)

    def test_una_columna_no_relacionada_no_reprograma(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self._cambiar(self.banner, "Pieza", "Banner cuadrado")
        self.assertEqual(self._fechas(tarea)[1], _fin_del_dia(date(2026, 11, 20)))
        self.assertFalse(self._reprogramaciones(tarea).exists())

    def test_una_tarea_cuya_fecha_no_sale_del_item_no_cambia(self):
        # «Producir pieza» no tiene fechas: cambiar la entrega con esa tarea abierta no le pone ninguna.
        producir = self._tarea_de(self._hija(self.banner))
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self.assertEqual(self._fechas(producir), (None, None))
        self.assertFalse(self._reprogramaciones(producir).exists())

    def test_un_cambio_antes_de_crear_la_tarea_se_usa_al_llegar(self):
        self._cambiar(self.banner, "Entrega", "2026-11-27")  # aún está en «Producir pieza»
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 27)),) * 2)
        self.assertFalse(self._reprogramaciones(tarea).exists())  # nació con la fecha vigente

    def test_auditoria_e_historial_con_quien_y_los_valores(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        evento = self._reprogramaciones(tarea).get()
        self.assertEqual(evento.actor, self.analista)
        self.assertEqual(evento.datos["anterior"]["fecha_limite"], _fin_del_dia(date(2026, 11, 20)).isoformat())
        self.assertEqual(evento.datos["nuevo"]["fecha_limite"], _fin_del_dia(date(2026, 11, 25)).isoformat())
        self.assertIn(f"#{self.banner.numero}", evento.datos["motivo"])
        self.assertTrue(
            RegistroAuditoria.objects.filter(
                modelo="tareas.tarea", object_id=tarea.pk, usuario=self.analista,
                datos_nuevos__fecha_limite=_fin_del_dia(date(2026, 11, 25)).isoformat(),
            ).exists()
        )

    def test_sin_permiso_de_edicion_no_cambia_nada(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        with self.assertRaises(PermissionDenied):
            self._cambiar(self.banner, "Entrega", "2026-11-25", actor=self.ajeno)
        self.assertEqual(self._fechas(tarea)[1], _fin_del_dia(date(2026, 11, 20)))
        self.assertFalse(self._reprogramaciones(tarea).exists())

    def test_es_idempotente(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self._cambiar(self.banner, "Entrega", "2026-11-25")  # mismo valor: no es un cambio
        self.assertEqual(self._reprogramaciones(tarea).count(), 1)
        self.assertFalse(reprogramar_tarea(tarea, self.analista, fecha_limite=_fin_del_dia(date(2026, 11, 25))))
        self.assertEqual(self._reprogramaciones(tarea).count(), 1)
        self.assertEqual(Tarea.objects.filter(titulo__startswith="Publicar").count(), 1)

    def test_vaciar_la_fecha_con_la_tarea_abierta_se_rechaza(self):
        # Bloque 5.5: la fecha se cambia, no se quita; nada de la edición se guarda (tampoco las otras columnas).
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        with self.assertRaisesMessage(ValidationError, "Cambia la fecha en lugar de dejarla vacía"):
            parrilla_ops.actualizar_valores(self.banner, self.analista, {
                self.columnas["Entrega"].pk: "", self.columnas["Pieza"].pk: "Banner cuadrado",
            })
        valores = parrilla_ops.valores_de(self.banner)
        self.assertEqual(valores[self.columnas["Entrega"].clave], date(2026, 11, 20))
        self.assertEqual(valores[self.columnas["Pieza"].clave], "Banner")
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 20)),) * 2)
        self.assertFalse(self._reprogramaciones(tarea).exists())

    def test_cambiar_la_fecha_sigue_permitido_despues_del_rechazo(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        with self.assertRaises(ValidationError):
            self._cambiar(self.banner, "Entrega", "")
        self._cambiar(self.banner, "Entrega", "2026-11-26")
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 26)),) * 2)

    def test_vaciar_la_fecha_sin_tarea_que_dependa_de_ella_se_permite(self):
        # Aún en «Producir pieza» (sin fechas): no hay tarea abierta que dependa de la columna.
        self._cambiar(self.banner, "Entrega", "")
        self.assertNotIn(self.columnas["Entrega"].clave, parrilla_ops.valores_de(self.banner))

    def test_vaciar_la_fecha_con_la_tarea_ya_cerrada_se_permite(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        cancelar_tarea(tarea, self.analista, motivo="Prueba")
        self._cambiar(self.banner, "Entrega", "")
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 20)),) * 2)

    def test_vaciar_la_fecha_de_otro_item_no_depende_de_esta_tarea(self):
        self._hasta_publicar(self.banner, self.disenadora)
        self._cambiar(self.video, "Entrega", "")  # el video sigue en «Producir pieza»
        self.assertNotIn(self.columnas["Entrega"].clave, parrilla_ops.valores_de(self.video))


class ReprogramacionOrdenDeBloqueosTests(_EscenarioReprogramacionMixin, TestCase):
    """Editar el ítem y completar su tarea toman PRIMERO la instancia principal y la Tarea al final: el mismo
    sentido, así que no pueden esperarse en sentido contrario."""

    def setUp(self):
        self._preparar_gacetas()
        self._arrancar()

    @staticmethod
    def _primer_lock(consultas, tabla):
        return next(
            i for i, consulta in enumerate(consultas)
            if "FOR UPDATE" in consulta["sql"] and f'FROM "{tabla}"' in consulta["sql"]
        )

    def _orden(self, operacion):
        with CaptureQueriesContext(connection) as capturadas:
            operacion()
        consultas = capturadas.captured_queries
        return (
            self._primer_lock(consultas, "workflows_instanciaworkflow"),
            self._primer_lock(consultas, "tareas_tarea"),
        )

    def test_editar_el_item_bloquea_la_instancia_antes_que_la_tarea(self):
        self._hasta_publicar(self.banner, self.disenadora)
        instancia, tarea = self._orden(lambda: self._cambiar(self.banner, "Entrega", "2026-11-25"))
        self.assertLess(instancia, tarea)

    def test_completar_la_tarea_bloquea_la_instancia_antes_que_la_tarea(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        Tarea.objects.filter(pk=tarea.pk).update(requiere_evidencia=False)
        instancia, bloqueo_tarea = self._orden(lambda: completar_tarea_workflow(tarea, self.disenadora))
        self.assertLess(instancia, bloqueo_tarea)


class ReprogramacionConcurrenciaTests(_EscenarioReprogramacionMixin, TransactionTestCase):
    """Hilos reales contra PostgreSQL: cambiar la fecha y completar la tarea a la vez terminan los dos, sin
    interbloqueo, y el resultado es uno de los dos órdenes posibles (nunca una tarea cerrada reprogramada)."""

    def setUp(self):
        self._preparar_gacetas()
        self._arrancar()

    def test_cambiar_la_fecha_y_completar_la_tarea_a_la_vez(self):
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        Tarea.objects.filter(pk=tarea.pk).update(requiere_evidencia=False)
        resultados = []
        barrera = threading.Barrier(2)

        def correr(funcion):
            barrera.wait()
            try:
                funcion()
                resultados.append("ok")
            except Exception as exc:  # noqa: BLE001 — cualquier error (incluido un interbloqueo) se reporta
                resultados.append(exc)
            finally:
                connection.close()

        hilos = [
            threading.Thread(target=correr, args=(lambda: completar_tarea_workflow(
                Tarea.objects.get(pk=tarea.pk), self.disenadora),)),
            threading.Thread(target=correr, args=(lambda: self._cambiar(self.banner, "Entrega", "2026-11-25"),)),
        ]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(timeout=30)
        self.assertFalse(any(hilo.is_alive() for hilo in hilos))
        self.assertEqual(resultados, ["ok", "ok"])
        tarea.refresh_from_db()
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)
        self.assertEqual(
            parrilla_ops.valores_de(self.banner)[self.columnas["Entrega"].clave], date(2026, 11, 25)
        )
        reprogramaciones = self._reprogramaciones(tarea)
        if reprogramaciones.exists():  # la fecha cambió primero: la tarea se completó ya reprogramada
            self.assertEqual(reprogramaciones.count(), 1)
            self.assertEqual(tarea.fecha_limite, _fin_del_dia(date(2026, 11, 25)))
            completada = tarea.historial.get(tipo_evento=HistorialTarea.TipoEvento.COMPLETADA)
            self.assertLess(reprogramaciones.get().pk, completada.pk)
        else:  # se completó primero: la tarea cerrada conserva su fecha
            self.assertEqual(tarea.fecha_limite, _fin_del_dia(date(2026, 11, 20)))


class ReprogramacionConfiguracionTests(_EscenarioReprogramacionMixin, TestCase):
    def setUp(self):
        self._preparar_gacetas()

    def test_un_plazo_relativo_no_se_mueve_y_la_fecha_del_item_si(self):
        self._arrancar(publicar_cfg=lambda cols: {
            "tipo_actor": "COLUMNA", "columna": cols["Responsable"].clave,
            "fecha_prevista_campo": f"item.{cols['Entrega'].clave}",
            "plazo_limite": {"cantidad": 2, "unidad": "DIAS"},
        })
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        _prevista, limite = self._fechas(tarea)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self.assertEqual(self._fechas(tarea), (_fin_del_dia(date(2026, 11, 25)), limite))

    def test_una_fecha_con_hora_se_toma_tal_cual_en_la_hora_local(self):
        columnas = (*self.COLUMNAS_F2, ("Salida", "FECHA_HORA", {}))
        self._arrancar(columnas=columnas, publicar_cfg=lambda cols: {
            "tipo_actor": "COLUMNA", "columna": cols["Responsable"].clave,
            "fecha_limite_campo": f"item.{cols['Salida'].clave}",
        })
        self._cambiar(self.banner, "Salida", "2026-11-25T10:30")
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self.assertEqual(self._fechas(tarea)[1], timezone.make_aware(datetime(2026, 11, 25, 10, 30)))
        self._cambiar(self.banner, "Salida", "2026-11-26T18:45")
        self.assertEqual(self._fechas(tarea)[1], timezone.make_aware(datetime(2026, 11, 26, 18, 45)))
        self.assertEqual(timezone.localtime(self._fechas(tarea)[1]).hour, 18)

    def test_la_configuracion_congelada_manda_aunque_el_studio_cambie(self):
        from apps.catalogo.configuracion_ejecucion import preparar_configuracion_ejecucion

        self._arrancar()
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        # Una versión NUEVA del flujo quita la fecha del ítem a «Publicar»: el ticket conserva la suya.
        borrador = preparar_configuracion_ejecucion(self.servicio, self.admin)
        publicar = borrador.bloques.get(nombre="Publicar")
        type(publicar).objects.filter(pk=publicar.pk).update(configuracion={
            "tipo_actor": "COLUMNA", "columna": self.columnas["Responsable"].clave,
        })
        self._activar(self.servicio, borrador)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self.assertEqual(self._fechas(tarea)[1], _fin_del_dia(date(2026, 11, 25)))

    def test_una_configuracion_historica_sin_fechas_del_item_no_cambia(self):
        self._arrancar(publicar_cfg=lambda cols: {"tipo_actor": "COLUMNA", "columna": cols["Responsable"].clave})
        tarea = self._hasta_publicar(self.banner, self.disenadora)
        self._cambiar(self.banner, "Entrega", "2026-11-25")
        self.assertEqual(self._fechas(tarea), (None, None))
        self.assertFalse(self._reprogramaciones(tarea).exists())
