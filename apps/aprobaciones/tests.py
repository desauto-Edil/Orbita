"""Pruebas de `apps.aprobaciones` — Sprint 3.4 (CU-024/025, RQF-078 a 084,
RN-025/026) + 3.UI.2 (interfaz HTTP).

Único archivo de pruebas de esta app (mismo criterio que
`apps.tareas`/`apps.workflows`: sin paquete `tests/`). El dominio (hasta
`ConcurrenciaResultadosIncompatiblesTests`) **no importa nada de
`apps.workflows`**: prueba viva de que `apps.aprobaciones` es
independiente. Las pruebas de integración Aprobación↔Workflow a nivel de
MOTOR (Strategy, motivo de espera, `resolver_aprobacion_workflow`,
selección de `resultado_aprobacion`) viven en `apps/workflows/tests.py`,
donde esa dependencia ya existe de forma legítima.

3.UI.2 agrega, en dos bloques: (1) pruebas HTTP de `apps/aprobaciones/
views.py` que no requieren Workflow (bandeja, detalle, decidir,
reasignar, autorización) — sin importar `apps.workflows`, mismo criterio
que el resto del archivo; y (2) al final, una única clase con la misma
excepción documentada ya usada en `apps/tareas/tests.py::
CompletarTareaWorkflowIntegracionTests`: para probar que `decidir_view`
invoca correctamente `apps.workflows.integracion.
resolver_aprobacion_workflow` cuando corresponde, esa clase sí importa
`apps.workflows` — aislada al final del archivo, nunca mezclada con el
resto."""

import threading

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.aprobaciones.autorizacion import (
    PERMISO_CONSULTAR,
    PERMISO_GESTIONAR,
    es_aprobador_directo,
    puede_aprobar,
    puede_reasignar_aprobacion,
)
from apps.aprobaciones.consultas import aprobaciones_pendientes_para, aprobaciones_visibles_para
from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion, ReasignacionAprobacion
from apps.aprobaciones.operaciones import crear_esquema_aprobacion, reasignar_aprobacion, resolver_aprobacion
from apps.core.models import AsignacionRol, Equipo, MiembroEquipo, Permiso, RegistroAuditoria, RolFuncional, RolPermiso

Usuario = get_user_model()
CLAVE_PRUEBA = "Clave-Segura-123"


def _otorgar_permiso(usuario, codigo):
    permiso, _ = Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": codigo})
    rol, _ = RolFuncional.objects.get_or_create(nombre=f"Rol {codigo}")
    RolPermiso.objects.get_or_create(rol=rol, permiso=permiso)
    AsignacionRol.objects.create(usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL)


def _crear_esquema_simple(*, modo=EsquemaAprobacion.Modo.PARALELA, politica=EsquemaAprobacion.Politica.CUALQUIERA, participantes):
    return crear_esquema_aprobacion(modo=modo, politica=politica, participantes=participantes)


class CrearEsquemaAprobacionTests(TestCase):
    def setUp(self):
        self.a = Usuario.objects.create_user("crear_a", password=CLAVE_PRUEBA)

    def test_requiere_al_menos_un_participante(self):
        with self.assertRaises(ValidationError):
            crear_esquema_aprobacion(modo=EsquemaAprobacion.Modo.SECUENCIAL, participantes=[])

    def test_paralela_requiere_politica(self):
        with self.assertRaises(ValidationError):
            crear_esquema_aprobacion(
                modo=EsquemaAprobacion.Modo.PARALELA,
                participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a)],
            )

    def test_secuencial_no_admite_politica(self):
        with self.assertRaises(ValidationError):
            crear_esquema_aprobacion(
                modo=EsquemaAprobacion.Modo.SECUENCIAL,
                politica=EsquemaAprobacion.Politica.TODOS,
                participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a)],
            )

    def test_orden_se_deriva_de_la_posicion(self):
        b = Usuario.objects.create_user("crear_b", password=CLAVE_PRUEBA)
        esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.SECUENCIAL,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a), (Aprobacion.TipoAprobador.USUARIO, b)],
        )
        ordenes = list(esquema.participaciones.order_by("orden").values_list("orden", flat=True))
        self.assertEqual(ordenes, [1, 2])


class AuditoriaOperacionesTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user("auditoria_aprobador")
        self.otro = Usuario.objects.create_user("auditoria_otro")
        self.gestor = Usuario.objects.create_user("auditoria_gestor")
        self.equipo = Equipo.objects.create(nombre="Equipo auditoría")
        _otorgar_permiso(self.gestor, PERMISO_GESTIONAR)

    def _crear(self, **kwargs):
        return crear_esquema_aprobacion(
            modo="PARALELA", politica="CUALQUIERA",
            participantes=[("USUARIO", self.actor), ("USUARIO", self.otro)],
            **kwargs,
        )

    def _evento_unico(self, anteriores, objeto, accion, actor):
        nuevos = RegistroAuditoria.objects.exclude(pk__in=anteriores)
        self.assertEqual(nuevos.count(), 1)
        evento = nuevos.get()
        self.assertEqual(evento.objeto, objeto)
        self.assertEqual(evento.accion, accion)
        self.assertEqual(evento.usuario, actor)
        self.assertEqual(evento.origen, "USUARIO" if actor is not None else "SISTEMA")
        return evento

    def test_creacion_un_evento_por_esquema_con_actor_o_sistema(self):
        for actor in (self.gestor, None):
            with self.subTest(actor=actor):
                anteriores = list(RegistroAuditoria.objects.values_list("pk", flat=True))
                esquema = self._crear(actor=actor)
                evento = self._evento_unico(anteriores, esquema, "CREAR", actor)
                self.assertIsNone(evento.datos_anteriores)
                self.assertEqual(evento.datos_nuevos, {
                    "modo": "PARALELA", "politica": "CUALQUIERA", "cantidad_aprobaciones": 2,
                })

    def test_decisiones_auditan_aprobacion_sin_evento_extra_por_cierre(self):
        for decision in ("APROBADA", "RECHAZADA", "DEVUELTA"):
            with self.subTest(decision=decision):
                esquema = self._crear()
                aprobacion = esquema.participaciones.get(aprobador_usuario=self.actor)
                anteriores = list(RegistroAuditoria.objects.values_list("pk", flat=True))
                aprobacion, esquema = resolver_aprobacion(
                    aprobacion, self.actor, decision=decision, observacion="Revisado",
                )
                self.assertEqual(esquema.resultado, decision)
                evento = self._evento_unico(anteriores, aprobacion, "ACTUALIZAR", self.actor)
                self.assertEqual(evento.datos_anteriores, {"estado": "PENDIENTE"})
                self.assertEqual(evento.datos_nuevos, {"estado": decision, "observacion": "Revisado"})

    def test_reasignacion_usuario_equipo_y_equipo_usuario(self):
        aprobacion = self._crear().participaciones.get(aprobador_usuario=self.actor)
        for destino, kwargs in (
            (self.equipo, {"nuevo_aprobador_equipo": self.equipo}),
            (self.otro, {"nuevo_aprobador_usuario": self.otro}),
        ):
            with self.subTest(destino=destino):
                datos_antes = {
                    "tipo_aprobador": aprobacion.tipo_aprobador,
                    "aprobador_usuario_id": aprobacion.aprobador_usuario_id,
                    "aprobador_equipo_id": aprobacion.aprobador_equipo_id,
                }
                anteriores = list(RegistroAuditoria.objects.values_list("pk", flat=True))
                historial = ReasignacionAprobacion.objects.count()
                aprobacion = reasignar_aprobacion(aprobacion, self.gestor, motivo="Ausencia", **kwargs)
                evento = self._evento_unico(anteriores, aprobacion, "ACTUALIZAR", self.gestor)
                self.assertEqual(evento.datos_anteriores, datos_antes)
                self.assertEqual(evento.datos_nuevos, {
                    "tipo_aprobador": aprobacion.tipo_aprobador,
                    "aprobador_usuario_id": aprobacion.aprobador_usuario_id,
                    "aprobador_equipo_id": aprobacion.aprobador_equipo_id,
                    "motivo": "Ausencia",
                })
                self.assertEqual(ReasignacionAprobacion.objects.count(), historial + 1)

    def test_creacion_rechazada_revierte_participaciones_sin_evento(self):
        anteriores = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            crear_esquema_aprobacion(
                modo="SECUENCIAL", actor=self.gestor,
                participantes=[("USUARIO", self.actor), ("INVALIDO", self.otro)],
            )
        self.assertFalse(EsquemaAprobacion.objects.exists())
        self.assertFalse(Aprobacion.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), anteriores)

    def test_decision_y_reasignacion_rechazadas_no_mutan_ni_auditan(self):
        aprobacion = self._crear().participaciones.get(aprobador_usuario=self.actor)
        anterior = Aprobacion.objects.filter(pk=aprobacion.pk).values().get()
        eventos = RegistroAuditoria.objects.count()
        intentos = (
            (ValidationError, lambda: resolver_aprobacion(aprobacion, self.actor, decision="RECHAZADA")),
            (PermissionDenied, lambda: resolver_aprobacion(aprobacion, self.otro, decision="APROBADA")),
            (ValidationError, lambda: reasignar_aprobacion(aprobacion, self.gestor)),
            (PermissionDenied, lambda: reasignar_aprobacion(
                aprobacion, self.otro, nuevo_aprobador_usuario=self.otro,
            )),
        )
        for error, intentar in intentos:
            with self.subTest(error=error, intentar=intentar):
                with self.assertRaises(error):
                    intentar()
                self.assertEqual(Aprobacion.objects.filter(pk=aprobacion.pk).values().get(), anterior)
                self.assertEqual(RegistroAuditoria.objects.count(), eventos)
                self.assertFalse(ReasignacionAprobacion.objects.exists())

    def test_aprobacion_decidida_no_genera_eventos_por_intentos_posteriores(self):
        aprobacion = self._crear().participaciones.get(aprobador_usuario=self.actor)
        resolver_aprobacion(aprobacion, self.actor, decision="APROBADA")
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            resolver_aprobacion(aprobacion, self.actor, decision="APROBADA")
        with self.assertRaises(ValidationError):
            reasignar_aprobacion(aprobacion, self.gestor, nuevo_aprobador_usuario=self.otro)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_rollback_externo_revierte_mutaciones_y_eventos(self):
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                esquema = self._crear(actor=self.gestor)
                aprobacion = esquema.participaciones.get(aprobador_usuario=self.actor)
                reasignar_aprobacion(aprobacion, self.gestor, nuevo_aprobador_usuario=self.gestor)
                resolver_aprobacion(aprobacion, self.gestor, decision="APROBADA")
                self.assertEqual(RegistroAuditoria.objects.count(), eventos + 3)
                raise RuntimeError("Rollback solicitado")
        self.assertFalse(EsquemaAprobacion.objects.exists())
        self.assertFalse(Aprobacion.objects.exists())
        self.assertFalse(ReasignacionAprobacion.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_fallo_de_auditoria_revierte_cada_operacion(self):
        from unittest.mock import patch

        with patch("apps.aprobaciones.operaciones.registrar_evento", side_effect=RuntimeError("Auditoría")):
            with self.assertRaises(RuntimeError):
                self._crear()
        self.assertFalse(EsquemaAprobacion.objects.exists())
        self.assertFalse(Aprobacion.objects.exists())
        aprobacion = self._crear().participaciones.get(aprobador_usuario=self.actor)
        anterior = Aprobacion.objects.filter(pk=aprobacion.pk).values().get()
        eventos = RegistroAuditoria.objects.count()
        with patch("apps.aprobaciones.operaciones.registrar_evento", side_effect=RuntimeError("Auditoría")):
            with self.assertRaises(RuntimeError):
                resolver_aprobacion(aprobacion, self.actor, decision="APROBADA")
            with self.assertRaises(RuntimeError):
                reasignar_aprobacion(aprobacion, self.gestor, nuevo_aprobador_usuario=self.otro)
        self.assertEqual(Aprobacion.objects.filter(pk=aprobacion.pk).values().get(), anterior)
        aprobacion.esquema.refresh_from_db()
        self.assertIsNone(aprobacion.esquema.resultado)
        self.assertIsNone(aprobacion.esquema.resuelto_en)
        self.assertFalse(ReasignacionAprobacion.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)


class ResolverAprobacionSimpleTests(TestCase):
    """Esquema de 1 solo participante — el caso base de CU-024."""

    def setUp(self):
        self.aprobador = Usuario.objects.create_user("simple_aprobador", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_aprobar_cierra_esquema_aprobada(self):
        _, esquema = resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.APROBADA)
        self.assertIsNotNone(esquema.resuelto_en)

    def test_rechazar_exige_observacion(self):
        with self.assertRaises(ValidationError):
            resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.RECHAZADA)

    def test_rechazar_con_observacion_cierra_rechazada(self):
        _, esquema = resolver_aprobacion(
            self.aprobacion, self.aprobador, decision=Aprobacion.Estado.RECHAZADA, observacion="No cumple."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.RECHAZADA)

    def test_devolver_exige_observacion(self):
        with self.assertRaises(ValidationError):
            resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.DEVUELTA)

    def test_devolver_con_observacion_cierra_devuelta(self):
        _, esquema = resolver_aprobacion(
            self.aprobacion, self.aprobador, decision=Aprobacion.Estado.DEVUELTA, observacion="Ajustar monto."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.DEVUELTA)

    def test_aprobar_sin_observacion_es_valido(self):
        aprobacion, _ = resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)
        self.assertEqual(aprobacion.observacion, "")

    def test_decision_invalida_rechazada(self):
        with self.assertRaises(ValidationError):
            resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.PENDIENTE)


class InmutabilidadTests(TestCase):
    """RN-026 — protegida por la API de dominio, no por señales."""

    def setUp(self):
        self.aprobador = Usuario.objects.create_user("inmut_aprobador", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_no_se_puede_decidir_dos_veces(self):
        resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)
        aprobacion = Aprobacion.objects.get(pk=self.aprobacion.pk)
        with self.assertRaises(ValidationError):
            resolver_aprobacion(aprobacion, self.aprobador, decision=Aprobacion.Estado.RECHAZADA, observacion="x")

    def test_decision_original_no_cambia_tras_intento_de_sobrescritura(self):
        resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)
        aprobacion = Aprobacion.objects.get(pk=self.aprobacion.pk)
        try:
            resolver_aprobacion(aprobacion, self.aprobador, decision=Aprobacion.Estado.RECHAZADA, observacion="x")
        except ValidationError:
            pass
        aprobacion.refresh_from_db()
        self.assertEqual(aprobacion.estado, Aprobacion.Estado.APROBADA)

    def test_check_constraint_decision_coherente(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Aprobacion.objects.create(
                    esquema=self.esquema,
                    orden=99,
                    tipo_aprobador=Aprobacion.TipoAprobador.USUARIO,
                    aprobador_usuario=self.aprobador,
                    estado=Aprobacion.Estado.APROBADA,
                )


class ParalelaTodosTests(TestCase):
    def setUp(self):
        self.a = Usuario.objects.create_user("todos_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("todos_b", password=CLAVE_PRUEBA)
        self.c = Usuario.objects.create_user("todos_c", password=CLAVE_PRUEBA)
        self.esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.TODOS,
            participantes=[
                (Aprobacion.TipoAprobador.USUARIO, self.a),
                (Aprobacion.TipoAprobador.USUARIO, self.b),
                (Aprobacion.TipoAprobador.USUARIO, self.c),
            ],
        )

    def _participacion(self, usuario):
        return self.esquema.participaciones.get(aprobador_usuario=usuario)

    def test_no_cierra_hasta_que_todos_aprueban(self):
        _, esquema = resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        self.assertIsNone(esquema.resultado)
        _, esquema = resolver_aprobacion(self._participacion(self.b), self.b, decision=Aprobacion.Estado.APROBADA)
        self.assertIsNone(esquema.resultado)
        _, esquema = resolver_aprobacion(self._participacion(self.c), self.c, decision=Aprobacion.Estado.APROBADA)
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.APROBADA)

    def test_cualquier_rechazo_cierra_inmediato_y_deja_no_requerida_al_resto(self):
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        _, esquema = resolver_aprobacion(
            self._participacion(self.b), self.b, decision=Aprobacion.Estado.RECHAZADA, observacion="No procede."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.RECHAZADA)
        c = Aprobacion.objects.get(pk=self._participacion(self.c).pk)
        self.assertEqual(c.estado, Aprobacion.Estado.NO_REQUERIDA)
        self.assertIsNone(c.decidido_por)
        self.assertIsNone(c.decidida_en)

    def test_rechazo_ya_grabado_prevalece_sobre_aprobacion_ya_grabada_aunque_no_haya_cerrado(self):
        """Demuestra la prioridad RECHAZADA > DEVUELTA > APROBADA de forma
        determinista (sin depender de una carrera de hilos reales): A
        aprueba primero (TODOS, no cierra porque faltan B y C); B rechaza
        después — el esquema cierra RECHAZADA de inmediato, aunque la
        aprobación de A ya estaba grabada antes y el esquema nunca llegó a
        evaluar el cierre con la política TODOS satisfecha."""
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        _, esquema = resolver_aprobacion(
            self._participacion(self.b), self.b, decision=Aprobacion.Estado.RECHAZADA, observacion="No."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.RECHAZADA)

    def test_decision_tardia_sobre_no_requerida_falla(self):
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        resolver_aprobacion(
            self._participacion(self.b), self.b, decision=Aprobacion.Estado.RECHAZADA, observacion="No."
        )
        c = Aprobacion.objects.get(pk=self._participacion(self.c).pk)
        with self.assertRaises(ValidationError):
            resolver_aprobacion(c, self.c, decision=Aprobacion.Estado.APROBADA)


class ParalelaCualquieraTests(TestCase):
    def setUp(self):
        self.a = Usuario.objects.create_user("cualq_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("cualq_b", password=CLAVE_PRUEBA)
        self.esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a), (Aprobacion.TipoAprobador.USUARIO, self.b)],
        )

    def _participacion(self, usuario):
        return self.esquema.participaciones.get(aprobador_usuario=usuario)

    def test_primera_aprobada_cierra_de_inmediato(self):
        _, esquema = resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.APROBADA)
        b = Aprobacion.objects.get(pk=self._participacion(self.b).pk)
        self.assertEqual(b.estado, Aprobacion.Estado.NO_REQUERIDA)

    def test_rechazo_cierra_de_inmediato(self):
        _, esquema = resolver_aprobacion(
            self._participacion(self.a), self.a, decision=Aprobacion.Estado.RECHAZADA, observacion="No."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.RECHAZADA)


class SecuencialTests(TestCase):
    def setUp(self):
        self.a = Usuario.objects.create_user("sec_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("sec_b", password=CLAVE_PRUEBA)
        self.c = Usuario.objects.create_user("sec_c", password=CLAVE_PRUEBA)
        self.esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.SECUENCIAL,
            participantes=[
                (Aprobacion.TipoAprobador.USUARIO, self.a),
                (Aprobacion.TipoAprobador.USUARIO, self.b),
                (Aprobacion.TipoAprobador.USUARIO, self.c),
            ],
        )

    def _participacion(self, usuario):
        return self.esquema.participaciones.get(aprobador_usuario=usuario)

    def test_solo_el_de_menor_orden_es_accionable(self):
        self.assertTrue(puede_aprobar(self.a, self._participacion(self.a)))
        self.assertFalse(puede_aprobar(self.b, self._participacion(self.b)))
        self.assertFalse(puede_aprobar(self.c, self._participacion(self.c)))

    def test_decidir_fuera_de_orden_falla(self):
        with self.assertRaises(PermissionDenied):
            resolver_aprobacion(self._participacion(self.b), self.b, decision=Aprobacion.Estado.APROBADA)

    def test_aprobada_habilita_al_siguiente(self):
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        self.assertTrue(puede_aprobar(self.b, self._participacion(self.b)))

    def test_ultima_aprobada_cierra_esquema_aprobada(self):
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        resolver_aprobacion(self._participacion(self.b), self.b, decision=Aprobacion.Estado.APROBADA)
        _, esquema = resolver_aprobacion(self._participacion(self.c), self.c, decision=Aprobacion.Estado.APROBADA)
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.APROBADA)

    def test_rechazo_intermedio_cierra_de_inmediato_sin_activar_siguientes(self):
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        _, esquema = resolver_aprobacion(
            self._participacion(self.b), self.b, decision=Aprobacion.Estado.RECHAZADA, observacion="No."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.RECHAZADA)
        c = Aprobacion.objects.get(pk=self._participacion(self.c).pk)
        self.assertEqual(c.estado, Aprobacion.Estado.NO_REQUERIDA)

    def test_devolucion_intermedia_cierra_devuelta(self):
        resolver_aprobacion(self._participacion(self.a), self.a, decision=Aprobacion.Estado.APROBADA)
        _, esquema = resolver_aprobacion(
            self._participacion(self.b), self.b, decision=Aprobacion.Estado.DEVUELTA, observacion="Ajustar."
        )
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.DEVUELTA)


class EquipoTests(TestCase):
    """Decisión aprobada F — un único puesto de aprobación por equipo,
    cualquier miembro activo lo resuelve."""

    def setUp(self):
        self.equipo = Equipo.objects.create(nombre="Equipo Aprobador")
        self.miembro1 = Usuario.objects.create_user("equipo_m1", password=CLAVE_PRUEBA)
        self.miembro2 = Usuario.objects.create_user("equipo_m2", password=CLAVE_PRUEBA)
        self.no_miembro = Usuario.objects.create_user("equipo_ajeno", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.miembro1, activo=True)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.miembro2, activo=True)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.EQUIPO, self.equipo)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_una_sola_fila_por_equipo(self):
        self.assertEqual(self.esquema.participaciones.count(), 1)

    def test_cualquier_miembro_puede_decidir(self):
        self.assertTrue(puede_aprobar(self.miembro1, self.aprobacion))
        self.assertTrue(puede_aprobar(self.miembro2, self.aprobacion))

    def test_no_miembro_no_puede_decidir(self):
        self.assertFalse(puede_aprobar(self.no_miembro, self.aprobacion))

    def test_decidido_por_registra_al_miembro_concreto(self):
        aprobacion, _ = resolver_aprobacion(self.aprobacion, self.miembro2, decision=Aprobacion.Estado.APROBADA)
        self.assertEqual(aprobacion.decidido_por_id, self.miembro2.id)
        self.assertIsNone(aprobacion.aprobador_usuario_id)

    def test_primer_miembro_consume_la_aprobacion_del_equipo(self):
        resolver_aprobacion(self.aprobacion, self.miembro1, decision=Aprobacion.Estado.APROBADA)
        aprobacion = Aprobacion.objects.get(pk=self.aprobacion.pk)
        with self.assertRaises(ValidationError):
            resolver_aprobacion(aprobacion, self.miembro2, decision=Aprobacion.Estado.RECHAZADA, observacion="x")


class RN025AutorizacionTests(TestCase):
    def setUp(self):
        self.aprobador = Usuario.objects.create_user("rn025_aprobador", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user("rn025_ajeno", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_usuario_sin_relacion_no_puede_decidir(self):
        self.assertFalse(puede_aprobar(self.ajeno, self.aprobacion))
        with self.assertRaises(PermissionDenied):
            resolver_aprobacion(self.aprobacion, self.ajeno, decision=Aprobacion.Estado.APROBADA)

    def test_permiso_global_gestionar_no_permite_decidir_trabajo_ajeno(self):
        """`aprobaciones.gestionar` es una función supervisora (reasignar)
        — nunca sustituye la relación directa (RN-025), mismo criterio
        que `puede_completar_tarea`."""
        _otorgar_permiso(self.ajeno, PERMISO_GESTIONAR)
        self.assertFalse(puede_aprobar(self.ajeno, self.aprobacion))
        with self.assertRaises(PermissionDenied):
            resolver_aprobacion(self.aprobacion, self.ajeno, decision=Aprobacion.Estado.APROBADA)

    def test_es_aprobador_directo_usuario_no_autenticado(self):
        anonimo = type("Anonimo", (), {"is_authenticated": False})()
        self.assertFalse(es_aprobador_directo(anonimo, self.aprobacion))


class ReasignacionAprobacionTests(TestCase):
    def setUp(self):
        self.gestor = Usuario.objects.create_user("reasig_gestor", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.gestor, PERMISO_GESTIONAR)
        self.original = Usuario.objects.create_user("reasig_original", password=CLAVE_PRUEBA)
        self.nuevo = Usuario.objects.create_user("reasig_nuevo", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.original)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_reasignar_sin_permiso_falla(self):
        sin_permiso = Usuario.objects.create_user("reasig_sinpermiso", password=CLAVE_PRUEBA)
        with self.assertRaises(PermissionDenied):
            reasignar_aprobacion(self.aprobacion, sin_permiso, nuevo_aprobador_usuario=self.nuevo)

    def test_reasignar_conserva_trazabilidad(self):
        aprobacion = reasignar_aprobacion(
            self.aprobacion, self.gestor, nuevo_aprobador_usuario=self.nuevo, motivo="Vacaciones"
        )
        self.assertEqual(aprobacion.aprobador_usuario_id, self.nuevo.id)
        registro = ReasignacionAprobacion.objects.get(aprobacion=aprobacion)
        self.assertEqual(registro.aprobador_anterior_usuario_id, self.original.id)
        self.assertEqual(registro.aprobador_nuevo_usuario_id, self.nuevo.id)
        self.assertEqual(registro.reasignado_por_id, self.gestor.id)
        self.assertEqual(registro.motivo, "Vacaciones")

    def test_no_se_puede_reasignar_una_decidida(self):
        resolver_aprobacion(self.aprobacion, self.original, decision=Aprobacion.Estado.APROBADA)
        aprobacion = Aprobacion.objects.get(pk=self.aprobacion.pk)
        with self.assertRaises(ValidationError):
            reasignar_aprobacion(aprobacion, self.gestor, nuevo_aprobador_usuario=self.nuevo)

    def test_reasignada_es_decidible_por_el_nuevo_aprobador(self):
        reasignar_aprobacion(self.aprobacion, self.gestor, nuevo_aprobador_usuario=self.nuevo)
        aprobacion = Aprobacion.objects.get(pk=self.aprobacion.pk)
        self.assertTrue(puede_aprobar(self.nuevo, aprobacion))
        self.assertFalse(puede_aprobar(self.original, aprobacion))


class ConsultasTests(TestCase):
    def setUp(self):
        self.aprobador = Usuario.objects.create_user("consulta_aprobador", password=CLAVE_PRUEBA)
        self.otro = Usuario.objects.create_user("consulta_otro", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_aprobaciones_pendientes_para_aprobador(self):
        self.assertIn(self.aprobacion, list(aprobaciones_pendientes_para(self.aprobador)))
        self.assertNotIn(self.aprobacion, list(aprobaciones_pendientes_para(self.otro)))

    def test_aprobaciones_pendientes_desaparece_tras_decidir(self):
        resolver_aprobacion(self.aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)
        self.assertNotIn(self.aprobacion.pk, list(aprobaciones_pendientes_para(self.aprobador).values_list("pk", flat=True)))

    def test_aprobaciones_visibles_por_permiso_gestionar(self):
        _otorgar_permiso(self.otro, PERMISO_GESTIONAR)
        self.assertIn(self.aprobacion, list(aprobaciones_visibles_para(self.otro)))


class ConcurrenciaEsquemaTests(TransactionTestCase):
    """PARALELA + CUALQUIERA con dos aprobadores decidiendo APROBADA
    verdaderamente en paralelo — mismo patrón (`TransactionTestCase` +
    `threading.Barrier`) que `ConcurrenciaCompletarTareaWorkflowTests` en
    `apps/workflows/tests.py`. Demuestra ausencia de duplicados y un cierre
    único, no una precedencia entre dos resultados INCOMPATIBLES (esa
    prioridad se demuestra de forma determinista, sin depender de timing
    de hilos, en `ParalelaTodosTests.
    test_rechazo_ya_grabado_prevalece_sobre_aprobacion_ya_grabada_aunque_no_haya_cerrado`
    — ver la nota de alcance sobre esto en el informe final)."""

    def setUp(self):
        self.a = Usuario.objects.create_user("conc_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("conc_b", password=CLAVE_PRUEBA)
        self.esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a), (Aprobacion.TipoAprobador.USUARIO, self.b)],
        )

    def test_dos_aprobadas_concurrentes_cierran_una_sola_vez(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _decidir(usuario, clave):
            barrera.wait()
            try:
                aprobacion = self.esquema.participaciones.get(aprobador_usuario=usuario)
                resolver_aprobacion(aprobacion, usuario, decision=Aprobacion.Estado.APROBADA)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_cerrada"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_decidir, args=(self.a, "a"))
        hilo_b = threading.Thread(target=_decidir, args=(self.b, "b"))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        esquema = EsquemaAprobacion.objects.get(pk=self.esquema.pk)
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.APROBADA)
        # Ambas decisiones quedaron grabadas (ninguna se pierde, RN-026):
        # una APROBADA (la que cerró) y la otra NO_REQUERIDA (o también
        # APROBADA si alcanzó a decidir antes del cierre) — nunca PENDIENTE.
        estados = set(self.esquema.participaciones.values_list("estado", flat=True))
        self.assertFalse(estados & {Aprobacion.Estado.PENDIENTE})

    def test_doble_click_misma_aprobacion(self):
        aprobacion = self.esquema.participaciones.get(aprobador_usuario=self.a)
        resultados = {}
        barrera = threading.Barrier(2)

        def _decidir(clave):
            barrera.wait()
            try:
                fila = Aprobacion.objects.get(pk=aprobacion.pk)
                resolver_aprobacion(fila, self.a, decision=Aprobacion.Estado.APROBADA)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_decidida"
            finally:
                connection.close()

        hilo_1 = threading.Thread(target=_decidir, args=("1",))
        hilo_2 = threading.Thread(target=_decidir, args=("2",))
        hilo_1.start()
        hilo_2.start()
        hilo_1.join()
        hilo_2.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_decidida"), 1)


class ConcurrenciaParalelaTodosTests(TransactionTestCase):
    """PARALELA + TODOS con 2 de 3 participantes decidiendo APROBADA en
    paralelo real — ninguno de los dos por sí solo cierra el esquema
    (falta el tercero), así que esto ejercita el camino de "ambas
    decisiones se graban, ninguna cierra todavía" bajo el nuevo orden de
    locks (EsquemaAprobacion → todas sus Aprobacion de una sola vez)."""

    def setUp(self):
        self.a = Usuario.objects.create_user("conc_todos_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("conc_todos_b", password=CLAVE_PRUEBA)
        self.c = Usuario.objects.create_user("conc_todos_c", password=CLAVE_PRUEBA)
        self.esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.TODOS,
            participantes=[
                (Aprobacion.TipoAprobador.USUARIO, self.a),
                (Aprobacion.TipoAprobador.USUARIO, self.b),
                (Aprobacion.TipoAprobador.USUARIO, self.c),
            ],
        )

    def test_dos_aprobaciones_concurrentes_no_cierran_sin_el_tercero(self):
        barrera = threading.Barrier(2)
        errores = []

        def _decidir(usuario):
            barrera.wait()
            try:
                aprobacion = self.esquema.participaciones.get(aprobador_usuario=usuario)
                resolver_aprobacion(aprobacion, usuario, decision=Aprobacion.Estado.APROBADA)
            except Exception as exc:  # noqa: BLE001 — cualquier error aquí es una falla real de la prueba.
                errores.append(exc)
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_decidir, args=(self.a,))
        hilo_b = threading.Thread(target=_decidir, args=(self.b,))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        self.assertEqual(errores, [])
        esquema = EsquemaAprobacion.objects.get(pk=self.esquema.pk)
        self.assertIsNone(esquema.resultado)
        decididas = esquema.participaciones.filter(estado=Aprobacion.Estado.APROBADA).count()
        self.assertEqual(decididas, 2)
        resolver_aprobacion(
            self.esquema.participaciones.get(aprobador_usuario=self.c), self.c, decision=Aprobacion.Estado.APROBADA
        )
        esquema.refresh_from_db()
        self.assertEqual(esquema.resultado, EsquemaAprobacion.Resultado.APROBADA)


class ConcurrenciaResultadosIncompatiblesTests(TransactionTestCase):
    """PARALELA + CUALQUIERA con una APROBADA y una RECHAZADA disparadas en
    paralelo real (no de forma determinista/secuencial como en
    `ParalelaTodosTests.
    test_rechazo_ya_grabado_prevalece_sobre_aprobacion_ya_grabada_aunque_no_haya_cerrado`).

    Límite de alcance HONESTO, reportado explícitamente en el informe
    final: la prioridad RECHAZADA > APROBADA solo se garantiza para
    decisiones que ya están grabadas (o dentro de la misma transacción)
    en el momento en que `_evaluar_cierre_esquema` evalúa. Si la
    transacción de A completa TODO su ciclo (decidir + evaluar + cerrar +
    commit) antes de que la de B siquiera comience, el esquema ya cerró
    con el resultado de A cuando B llega — no hay forma de "deshacer" un
    cierre ya comprometido sin una función de rollback que nadie ha
    pedido. Esta prueba por eso NO asume qué resultado gana: verifica
    únicamente las garantías que SÍ son deterministas — sin deadlock, sin
    decisión perdida, exactamente un cierre, y el resultado final es
    siempre uno de los dos realmente decididos (nunca un tercer valor
    inventado)."""

    def setUp(self):
        self.a = Usuario.objects.create_user("conc_incompat_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("conc_incompat_b", password=CLAVE_PRUEBA)
        self.esquema = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a), (Aprobacion.TipoAprobador.USUARIO, self.b)],
        )

    def test_sin_deadlock_sin_perdida_resultado_es_uno_de_los_dos_decididos(self):
        errores = []
        barrera = threading.Barrier(2)

        def _decidir(usuario, decision, observacion):
            barrera.wait()
            try:
                aprobacion = self.esquema.participaciones.get(aprobador_usuario=usuario)
                resolver_aprobacion(aprobacion, usuario, decision=decision, observacion=observacion)
            except ValidationError:
                pass  # esperado si la otra decisión ya cerró el esquema primero.
            except Exception as exc:  # noqa: BLE001
                errores.append(exc)
            finally:
                connection.close()

        hilo_a = threading.Thread(
            target=_decidir, args=(self.a, Aprobacion.Estado.APROBADA, "")
        )
        hilo_b = threading.Thread(
            target=_decidir, args=(self.b, Aprobacion.Estado.RECHAZADA, "No procede.")
        )
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        self.assertEqual(errores, [])  # nunca deadlock, nunca error inesperado.
        esquema = EsquemaAprobacion.objects.get(pk=self.esquema.pk)
        self.assertIn(esquema.resultado, (EsquemaAprobacion.Resultado.APROBADA, EsquemaAprobacion.Resultado.RECHAZADA))
        estados = list(self.esquema.participaciones.values_list("estado", flat=True))
        self.assertNotIn(Aprobacion.Estado.PENDIENTE, estados)


# ----------------------------------------------------------------------------
# 3.UI.2 — Pruebas HTTP de apps/aprobaciones/views.py que no requieren
# Workflow (bandeja, detalle, decidir, reasignar, autorización). Sin
# importar apps.workflows — mismo criterio que el resto de este archivo.
# ----------------------------------------------------------------------------


class AutenticacionRequeridaTests(TestCase):
    def setUp(self):
        aprobador = Usuario.objects.create_user("autreq_aprobador", password=CLAVE_PRUEBA)
        esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, aprobador)])
        self.aprobacion = esquema.participaciones.get()

    def test_bandeja_requiere_login(self):
        respuesta = self.client.get(reverse("aprobaciones:lista"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.url)

    def test_detalle_requiere_login(self):
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[self.aprobacion.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.url)


class BandejaViewTests(TestCase):
    def setUp(self):
        self.aprobador = Usuario.objects.create_user("bandeja_aprobador", password=CLAVE_PRUEBA)
        self.otro = Usuario.objects.create_user("bandeja_otro", password=CLAVE_PRUEBA)
        self.esquema_propio = _crear_esquema_simple(
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)]
        )
        self.aprobacion_propia = self.esquema_propio.participaciones.get()
        self.esquema_ajeno = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.otro)])

    def test_bandeja_renderiza_y_filtra_solo_pendientes_propias(self):
        self.client.login(username="bandeja_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:lista"))
        self.assertEqual(respuesta.status_code, 200)
        pks = [fila["aprobacion"].pk for fila in respuesta.context["filas"]]
        self.assertIn(self.aprobacion_propia.pk, pks)
        self.assertEqual(len(pks), 1)

    def test_tab_todas_sin_permiso_sigue_acotado_a_la_relacion_directa(self):
        """Sin `aprobaciones.consultar`/`gestionar`, "Todas" no debe
        ampliar la visibilidad más allá de lo que ya resuelve
        `aprobaciones_visibles_para` — no se reescribe su QuerySet."""
        self.client.login(username="bandeja_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:lista"), {"tab": "todas"})
        pks = [fila["aprobacion"].pk for fila in respuesta.context["filas"]]
        self.assertIn(self.aprobacion_propia.pk, pks)
        self.assertNotIn(self.esquema_ajeno.participaciones.get().pk, pks)

    def test_tab_todas_con_permiso_consultar_ve_todo(self):
        _otorgar_permiso(self.aprobador, PERMISO_CONSULTAR)
        self.client.login(username="bandeja_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:lista"), {"tab": "todas"})
        pks = [fila["aprobacion"].pk for fila in respuesta.context["filas"]]
        self.assertIn(self.esquema_ajeno.participaciones.get().pk, pks)

    def test_bandeja_empty_state(self):
        sin_trabajo = Usuario.objects.create_user("bandeja_sin_trabajo", password=CLAVE_PRUEBA)
        self.client.login(username="bandeja_sin_trabajo", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:lista"))
        self.assertContains(respuesta, "No tienes aprobaciones pendientes.")


class DetalleViewTests(TestCase):
    def setUp(self):
        self.aprobador = Usuario.objects.create_user("detalle_aprobador", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user("detalle_ajeno", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_detalle_autorizado_para_aprobador_directo(self):
        self.client.login(username="detalle_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[self.aprobacion.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(respuesta.context["contexto_workflow"])

    def test_detalle_no_autorizado_devuelve_403(self):
        self.client.login(username="detalle_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[self.aprobacion.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_acciones_ocultas_cuando_no_corresponden(self):
        """Un usuario con solo `aprobaciones.consultar` (visibilidad, no
        relación directa) ve el detalle pero ningún control de decisión."""
        _otorgar_permiso(self.ajeno, PERMISO_CONSULTAR)
        self.client.login(username="detalle_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[self.aprobacion.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(respuesta.context["puede_decidir"])
        self.assertFalse(respuesta.context["puede_reasignar"])
        self.assertIsNone(respuesta.context["decision_form"])
        self.assertNotContains(respuesta, reverse("aprobaciones:decidir", args=[self.aprobacion.pk]))


class DecidirViewTests(TestCase):
    def setUp(self):
        self.aprobador = Usuario.objects.create_user("decidir_aprobador", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user("decidir_ajeno", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_aprobar_exitoso(self):
        self.client.login(username="decidir_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self.aprobacion.pk]), {"decision": Aprobacion.Estado.APROBADA}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.estado, Aprobacion.Estado.APROBADA)

    def test_rechazar_sin_observacion_no_modifica_dominio(self):
        self.client.login(username="decidir_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self.aprobacion.pk]), {"decision": Aprobacion.Estado.RECHAZADA}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.estado, Aprobacion.Estado.PENDIENTE)

    def test_rechazar_con_observacion_exitoso(self):
        self.client.login(username="decidir_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self.aprobacion.pk]),
            {"decision": Aprobacion.Estado.RECHAZADA, "observacion": "No cumple."},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.estado, Aprobacion.Estado.RECHAZADA)

    def test_devolver_con_observacion_exitoso(self):
        self.client.login(username="decidir_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self.aprobacion.pk]),
            {"decision": Aprobacion.Estado.DEVUELTA, "observacion": "Ajustar."},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.estado, Aprobacion.Estado.DEVUELTA)

    def test_decidir_no_autorizado_devuelve_403(self):
        self.client.login(username="decidir_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self.aprobacion.pk]), {"decision": Aprobacion.Estado.APROBADA}
        )
        self.assertEqual(respuesta.status_code, 403)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.estado, Aprobacion.Estado.PENDIENTE)

    def test_post_directo_sobre_aprobacion_ajena_tambien_es_403(self):
        otro_esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.ajeno)])
        ajena = otro_esquema.participaciones.get()
        self.client.login(username="decidir_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[ajena.pk]), {"decision": Aprobacion.Estado.APROBADA}
        )
        self.assertEqual(respuesta.status_code, 403)

    def test_prg_tras_decision_exitosa(self):
        self.client.login(username="decidir_aprobador", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[self.aprobacion.pk]),
            {"decision": Aprobacion.Estado.APROBADA},
            follow=True,
        )
        self.assertRedirects(respuesta, reverse("aprobaciones:detalle", args=[self.aprobacion.pk]))
        mensajes = [str(m) for m in respuesta.context["messages"]]
        self.assertIn("Decisión registrada.", mensajes)


class ReasignarViewTests(TestCase):
    def setUp(self):
        self.gestor = Usuario.objects.create_user("reasigview_gestor", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.gestor, PERMISO_GESTIONAR)
        self.original = Usuario.objects.create_user("reasigview_original", password=CLAVE_PRUEBA)
        self.nuevo = Usuario.objects.create_user("reasigview_nuevo", password=CLAVE_PRUEBA)
        self.esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.original)])
        self.aprobacion = self.esquema.participaciones.get()

    def test_reasignar_exitoso(self):
        self.client.login(username="reasigview_gestor", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:reasignar", args=[self.aprobacion.pk]), {"usuario": self.nuevo.pk}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.aprobador_usuario_id, self.nuevo.id)

    def test_reasignar_no_autorizado_devuelve_403(self):
        self.client.login(username="reasigview_original", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:reasignar", args=[self.aprobacion.pk]), {"usuario": self.nuevo.pk}
        )
        self.assertEqual(respuesta.status_code, 403)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.aprobador_usuario_id, self.original.id)

    def test_formulario_invalido_no_altera_dominio(self):
        self.client.login(username="reasigview_gestor", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("aprobaciones:reasignar", args=[self.aprobacion.pk]), {})
        self.assertEqual(respuesta.status_code, 302)
        self.aprobacion.refresh_from_db()
        self.assertEqual(self.aprobacion.aprobador_usuario_id, self.original.id)


# ----------------------------------------------------------------------------
# Excepción documentada (ver docstring de este módulo y de
# `apps/aprobaciones/views.py::decidir_view`): única clase de este archivo
# que importa `apps.workflows`, para probar la integración HTTP real de
# "decidir una Aprobación vinculada a un Workflow" — sin esto, la
# instrucción explícita de 3.UI.2 ("la vista no puede reproducir el
# motor... no puede llamar simplemente a resolver_aprobacion() si eso deja
# Aprobación RESUELTA / Workflow EN_ESPERA") quedaría sin verificación
# end-to-end.
# ----------------------------------------------------------------------------

from apps.workflows.models import (  # noqa: E402  (import tardío intencional, ver nota arriba)
    ConfiguracionEtapaAprobacion as _ConfiguracionEtapaAprobacion,
    EsquemaAprobacionWorkflow as _EsquemaAprobacionWorkflow,
    Etapa as _Etapa,
    InstanciaWorkflow as _InstanciaWorkflow,
    ParticipanteEtapaAprobacion as _ParticipanteEtapaAprobacion,
    TransicionEtapa as _TransicionEtapa,
    Workflow as _Workflow,
    WorkflowVersion as _WorkflowVersion,
)
from apps.workflows.motor import iniciar_workflow as _iniciar_workflow  # noqa: E402
from apps.workflows.versionamiento import activar_version as _activar_version  # noqa: E402


def _crear_workflow_con_aprobacion_vinculada(actor, aprobadores, *, modo, politica=None):
    """Fixture mínima — duplicado deliberado y reducido de
    `apps.workflows.tests._crear_workflow_activo_con_aprobacion` (no se
    importa ese módulo de pruebas como si fuera una librería). INICIO →
    APROBACION → 3 FIN (uno por resultado_aprobacion), ya ACTIVA.
    `aprobadores`: lista ordenada de Usuario (participantes tipo USUARIO,
    suficiente para estas pruebas de integración HTTP)."""
    workflow = _Workflow.objects.create(nombre="3.UI.2 — integración HTTP")
    version = _WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = _Etapa.objects.create(version=version, tipo=_Etapa.Tipo.INICIO, nombre="Inicio")
    aprobacion_etapa = _Etapa.objects.create(version=version, tipo=_Etapa.Tipo.APROBACION, nombre="Aprobar")
    _TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion_etapa)
    configuracion = _ConfiguracionEtapaAprobacion.objects.create(
        etapa=aprobacion_etapa, modo=modo, politica=politica or ""
    )
    for orden, aprobador in enumerate(aprobadores, start=1):
        _ParticipanteEtapaAprobacion.objects.create(
            configuracion=configuracion,
            orden=orden,
            tipo_aprobador=_ParticipanteEtapaAprobacion.TipoAprobador.USUARIO,
            usuario=aprobador,
        )
    for resultado in ("APROBADA", "RECHAZADA", "DEVUELTA"):
        fin = _Etapa.objects.create(version=version, tipo=_Etapa.Tipo.FIN, nombre=f"Fin {resultado}")
        _TransicionEtapa.objects.create(
            etapa_origen=aprobacion_etapa, etapa_destino=fin, resultado_aprobacion=resultado
        )
    _activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    instancia = _iniciar_workflow(workflow, actor=actor)
    esquema = _EsquemaAprobacionWorkflow.objects.get(instancia_etapa__instancia_workflow=instancia).esquema
    return instancia, esquema


class DecidirViewIntegracionWorkflowTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user("aprobint_actor", password=CLAVE_PRUEBA)
        self.aprobador_a = Usuario.objects.create_user("aprobint_a", password=CLAVE_PRUEBA)
        self.aprobador_b = Usuario.objects.create_user("aprobint_b", password=CLAVE_PRUEBA)

    def test_decision_definitiva_continua_workflow(self):
        instancia, esquema = _crear_workflow_con_aprobacion_vinculada(
            self.actor,
            [self.aprobador_a],
            modo=_ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=_ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
        )
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, _InstanciaWorkflow.Estado.EN_ESPERA)
        aprobacion = esquema.participaciones.get()

        self.client.login(username="aprobint_a", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[aprobacion.pk]), {"decision": Aprobacion.Estado.APROBADA}
        )
        self.assertEqual(respuesta.status_code, 302)

        # Único participante, PARALELA+CUALQUIERA: esta decisión cierra el
        # esquema de inmediato y debe reanudar el motor en la misma
        # transacción — la instancia NUNCA debe quedar "Aprobación
        # RESUELTA / Workflow EN_ESPERA" (instrucción explícita).
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, _InstanciaWorkflow.Estado.COMPLETADA)

    def test_decision_intermedia_no_avanza_workflow_prematuramente(self):
        instancia, esquema = _crear_workflow_con_aprobacion_vinculada(
            self.actor,
            [self.aprobador_a, self.aprobador_b],
            modo=_ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=_ConfiguracionEtapaAprobacion.Politica.TODOS,
        )
        instancia.refresh_from_db()
        aprobacion_a = esquema.participaciones.get(aprobador_usuario=self.aprobador_a)

        self.client.login(username="aprobint_a", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[aprobacion_a.pk]), {"decision": Aprobacion.Estado.APROBADA}
        )
        self.assertEqual(respuesta.status_code, 302)

        # Política TODOS con 2 participantes: la primera decisión no cierra
        # el esquema todavía — resolver una participación individual no
        # significa que la Etapa APROBACION ya pueda continuar (decisión
        # aprobada, ver docstring de resolver_aprobacion_workflow).
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, _InstanciaWorkflow.Estado.EN_ESPERA)

    def test_aprobacion_independiente_funciona_sin_workflow(self):
        esquema = _crear_esquema_simple(participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador_a)])
        aprobacion = esquema.participaciones.get()
        self.client.login(username="aprobint_a", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("aprobaciones:decidir", args=[aprobacion.pk]), {"decision": Aprobacion.Estado.APROBADA}
        )
        self.assertEqual(respuesta.status_code, 302)
        aprobacion.refresh_from_db()
        self.assertEqual(aprobacion.estado, Aprobacion.Estado.APROBADA)

    def test_detalle_muestra_contexto_workflow_cuando_existe(self):
        instancia, esquema = _crear_workflow_con_aprobacion_vinculada(
            self.actor,
            [self.aprobador_a],
            modo=_ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=_ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
        )
        aprobacion = esquema.participaciones.get()
        self.client.login(username="aprobint_a", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("aprobaciones:detalle", args=[aprobacion.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNotNone(respuesta.context["contexto_workflow"])
        self.assertEqual(respuesta.context["contexto_workflow"]["etapa"].nombre, "Aprobar")
