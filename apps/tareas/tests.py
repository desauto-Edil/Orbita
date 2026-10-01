"""Pruebas de `apps.tareas` — Sprint 3.3 (CU-021/022/023, RQF-071 a 077,
RN-024).

Único archivo de pruebas de esta app (mismo criterio que `apps.core`/
`apps.catalogo`/`apps.tickets`/`apps.workflows`: sin paquete `tests/`). No
se ejecutan como parte de la implementación — se entregan junto con los
comandos exactos para correrlas vía Docker; las corre el usuario.

Deliberadamente **no importa nada de `apps.workflows`** en su dominio
(`ModeloTareaTests` en adelante): es la prueba viva de que `apps.tareas`
sigue siendo independiente (corrección arquitectónica aprobada). Las
pruebas de integración Tarea↔Workflow a nivel de MOTOR (Strategy, motivo
de espera) siguen viviendo en `apps/workflows/tests.py`.

**3.UI.1 (HTTP)** agrega al final de este archivo una única excepción
documentada: `CompletarTareaWorkflowIntegracionTests` sí importa
`apps.workflows` — porque `apps/tareas/views.py::completar_view` también
lo hace, a propósito, para invocar `apps.workflows.integracion.
completar_tarea_workflow` cuando la Tarea es la principal de una
`InstanciaEtapa` (ver su docstring). Probar esa vista sin poder construir
un Workflow real sería probar solo la mitad del comportamiento que la
propia instrucción de 3.UI.1 exige verificar ("Atención especial a Tareas
originadas por Workflow... no llames a una operación base que deje Tarea
COMPLETADA / Workflow EN_ESPERA"). El resto del archivo permanece sin esa
dependencia."""

import shutil
import tempfile
import threading

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AsignacionRol, Equipo, MiembroEquipo, Permiso, RegistroAuditoria, RolFuncional, RolPermiso
from apps.tareas.autorizacion import (
    PERMISO_CONSULTAR,
    PERMISO_GESTIONAR,
    delegado_actual,
    es_responsable_actual,
    puede_asignar_tarea,
    puede_comentar_tarea,
    puede_completar_tarea,
    puede_consultar_tarea,
    puede_crear_subtarea,
    puede_delegar_tarea,
    puede_iniciar_tarea,
    puede_reasignar_tarea,
    puede_tomar_tarea,
)
from apps.tareas.consultas import (
    tareas_asignadas_a,
    tareas_del_equipo,
    tareas_disponibles_para_tomar,
    tareas_vencidas,
    tareas_visibles_para,
)
from apps.tareas.models import AdjuntoTarea, ComentarioTarea, DelegacionTarea, HistorialTarea, Tarea
from apps.tareas.operaciones import (
    adjuntar_evidencia_tarea,
    asignar_tarea,
    comentar_tarea,
    completar_tarea,
    crear_subtarea,
    crear_tarea,
    delegar_tarea,
    iniciar_tarea,
    reasignar_tarea,
    tomar_tarea,
)

Usuario = get_user_model()
CLAVE_PRUEBA = "Clave-Segura-123"


def _otorgar_permiso(usuario, codigo):
    permiso, _ = Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": codigo})
    rol = RolFuncional.objects.create(nombre=f"Rol {codigo} {usuario.username}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL)


def _crear_tarea_simple(**kwargs):
    defaults = {"titulo": "Tarea de prueba"}
    defaults.update(kwargs)
    return crear_tarea(**defaults)


class ModeloTareaTests(TestCase):
    def test_origen_sistema_sin_creador_es_invalido(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Tarea.objects.create(titulo="x", origen=Tarea.Origen.SISTEMA, creada_por=Usuario.objects.create_user("u1", password=CLAVE_PRUEBA))

    def test_subtarea_no_puede_tener_otra_subtarea(self):
        padre = _crear_tarea_simple(titulo="Padre", permite_subtareas=True)
        hija = Tarea.objects.create(titulo="Hija", tarea_padre=padre)
        nieta = Tarea(titulo="Nieta", tarea_padre=hija)
        with self.assertRaises(ValidationError):
            nieta.save()

    def test_str(self):
        tarea = _crear_tarea_simple(titulo="Revisar algo")
        self.assertEqual(str(tarea), "Revisar algo")


class CrearTareaTests(TestCase):
    def test_crear_tarea_independiente_manual(self):
        actor = Usuario.objects.create_user("creador1", password=CLAVE_PRUEBA)
        tarea = crear_tarea(titulo="Tarea manual", creada_por=actor, origen=Tarea.Origen.MANUAL)
        self.assertEqual(tarea.estado, Tarea.Estado.PENDIENTE)
        self.assertEqual(tarea.creada_por_id, actor.id)

    def test_crear_tarea_sistema_sin_creador(self):
        tarea = crear_tarea(titulo="Tarea de sistema", origen=Tarea.Origen.SISTEMA)
        self.assertIsNone(tarea.creada_por_id)

    def test_crear_tarea_sistema_con_creador_falla(self):
        actor = Usuario.objects.create_user("creador2", password=CLAVE_PRUEBA)
        with self.assertRaises(ValueError):
            crear_tarea(titulo="x", origen=Tarea.Origen.SISTEMA, creada_por=actor)

    def test_crear_tarea_audita_creacion(self):
        actor = Usuario.objects.create_user("creador3", password=CLAVE_PRUEBA)
        tarea = crear_tarea(titulo="Tarea auditada", creada_por=actor)
        from django.contrib.contenttypes.models import ContentType

        existe = RegistroAuditoria.objects.filter(
            content_type=ContentType.objects.get_for_model(Tarea), object_id=tarea.pk,
            accion=RegistroAuditoria.Accion.CREAR,
        ).exists()
        self.assertTrue(existe)


class AsignacionTests(TestCase):
    def setUp(self):
        self.gestor = Usuario.objects.create_user("gestor1", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.gestor, PERMISO_GESTIONAR)
        self.ejecutor = Usuario.objects.create_user("ejecutor1", password=CLAVE_PRUEBA)
        self.equipo = Equipo.objects.create(nombre="Equipo A")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.ejecutor)

    def test_tomar_tarea_por_gestor_global(self):
        tarea = _crear_tarea_simple()
        tarea = tomar_tarea(tarea, self.gestor)
        self.assertEqual(tarea.usuario_responsable_id, self.gestor.id)

    def test_tomar_tarea_por_miembro_del_equipo_responsable(self):
        tarea = _crear_tarea_simple(equipo_responsable=self.equipo)
        tarea = tomar_tarea(tarea, self.ejecutor)
        self.assertEqual(tarea.usuario_responsable_id, self.ejecutor.id)

    def test_tomar_tarea_sin_relacion_ni_permiso_falla(self):
        tarea = _crear_tarea_simple()
        extrano = Usuario.objects.create_user("extrano1", password=CLAVE_PRUEBA)
        with self.assertRaises(PermissionDenied):
            tomar_tarea(tarea, extrano)

    def test_tomar_tarea_ya_asignada_falla(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.ejecutor)
        with self.assertRaises(ValidationError):
            tomar_tarea(tarea, self.gestor)

    def test_asignar_tarea_por_gestor(self):
        tarea = _crear_tarea_simple()
        tarea = asignar_tarea(tarea, self.gestor, usuario=self.ejecutor)
        self.assertEqual(tarea.usuario_responsable_id, self.ejecutor.id)

    def test_asignar_tarea_sin_permiso_falla(self):
        tarea = _crear_tarea_simple()
        with self.assertRaises(PermissionDenied):
            asignar_tarea(tarea, self.ejecutor, usuario=self.ejecutor)

    def test_reasignar_tarea_por_gestor(self):
        otro = Usuario.objects.create_user("otro1", password=CLAVE_PRUEBA)
        tarea = _crear_tarea_simple(usuario_responsable=self.ejecutor)
        tarea = reasignar_tarea(tarea, self.gestor, usuario=otro)
        self.assertEqual(tarea.usuario_responsable_id, otro.id)

    def test_reasignar_tarea_por_responsable_actual(self):
        otro = Usuario.objects.create_user("otro2", password=CLAVE_PRUEBA)
        tarea = _crear_tarea_simple(usuario_responsable=self.ejecutor)
        tarea = reasignar_tarea(tarea, self.ejecutor, usuario=otro)
        self.assertEqual(tarea.usuario_responsable_id, otro.id)

    def test_reasignar_tarea_completada_falla(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.ejecutor)
        tarea = completar_tarea(tarea, self.ejecutor)
        with self.assertRaises(ValidationError):
            reasignar_tarea(tarea, self.gestor, usuario=self.ejecutor)

    def test_historial_registra_tomada_asignada_reasignada(self):
        tarea = _crear_tarea_simple()
        tomar_tarea(tarea, self.gestor)
        self.assertTrue(tarea.historial.filter(tipo_evento=HistorialTarea.TipoEvento.TOMADA).exists())

        tarea2 = _crear_tarea_simple()
        asignar_tarea(tarea2, self.gestor, usuario=self.ejecutor)
        self.assertTrue(tarea2.historial.filter(tipo_evento=HistorialTarea.TipoEvento.ASIGNADA).exists())

        otro = Usuario.objects.create_user("otro3", password=CLAVE_PRUEBA)
        reasignar_tarea(tarea2, self.gestor, usuario=otro)
        self.assertTrue(tarea2.historial.filter(tipo_evento=HistorialTarea.TipoEvento.REASIGNADA).exists())

    def test_asignacion_audita_cambio_de_responsable(self):
        from django.contrib.contenttypes.models import ContentType

        tarea = _crear_tarea_simple()
        tomar_tarea(tarea, self.gestor)
        existe = RegistroAuditoria.objects.filter(
            content_type=ContentType.objects.get_for_model(Tarea), object_id=tarea.pk,
            accion=RegistroAuditoria.Accion.ACTUALIZAR,
        ).exists()
        self.assertTrue(existe)


class DelegacionTests(TestCase):
    def setUp(self):
        self.responsable = Usuario.objects.create_user("resp1", password=CLAVE_PRUEBA)
        self.delegado = Usuario.objects.create_user("deleg1", password=CLAVE_PRUEBA)
        self.tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        self.ahora = timezone.now()

    def test_delegar_no_modifica_responsable_formal(self):
        delegar_tarea(
            self.tarea, self.responsable, delegado_a=self.delegado,
            desde=self.ahora, hasta=self.ahora + timedelta(hours=1),
        )
        self.tarea.refresh_from_db()
        self.assertEqual(self.tarea.usuario_responsable_id, self.responsable.id)

    def test_desde_debe_ser_menor_que_hasta(self):
        with self.assertRaises(ValidationError):
            delegar_tarea(
                self.tarea, self.responsable, delegado_a=self.delegado,
                desde=self.ahora, hasta=self.ahora - timedelta(hours=1),
            )

    def test_no_se_puede_delegar_al_propio_responsable(self):
        with self.assertRaises(ValidationError):
            delegar_tarea(
                self.tarea, self.responsable, delegado_a=self.responsable,
                desde=self.ahora, hasta=self.ahora + timedelta(hours=1),
            )

    def test_solapamiento_de_delegaciones_activas_es_invalido(self):
        delegar_tarea(
            self.tarea, self.responsable, delegado_a=self.delegado,
            desde=self.ahora, hasta=self.ahora + timedelta(hours=2),
        )
        otro_delegado = Usuario.objects.create_user("deleg2", password=CLAVE_PRUEBA)
        with self.assertRaises(ValidationError):
            delegar_tarea(
                self.tarea, self.responsable, delegado_a=otro_delegado,
                desde=self.ahora + timedelta(hours=1), hasta=self.ahora + timedelta(hours=3),
            )

    def test_delegacion_no_solapada_es_valida(self):
        delegar_tarea(
            self.tarea, self.responsable, delegado_a=self.delegado,
            desde=self.ahora, hasta=self.ahora + timedelta(hours=1),
        )
        otro_delegado = Usuario.objects.create_user("deleg3", password=CLAVE_PRUEBA)
        delegacion2 = delegar_tarea(
            self.tarea, self.responsable, delegado_a=otro_delegado,
            desde=self.ahora + timedelta(hours=1), hasta=self.ahora + timedelta(hours=2),
        )
        self.assertEqual(delegacion2.delegado_a_id, otro_delegado.id)

    def test_delegado_puede_actuar_durante_vigencia(self):
        delegar_tarea(
            self.tarea, self.responsable, delegado_a=self.delegado,
            desde=self.ahora, hasta=self.ahora + timedelta(hours=1),
        )
        self.assertTrue(es_responsable_actual(self.delegado, self.tarea))
        self.assertTrue(puede_completar_tarea(self.delegado, self.tarea))
        self.assertEqual(delegado_actual(self.tarea).id, self.delegado.id)

    def test_delegado_no_puede_actuar_fuera_de_vigencia(self):
        delegar_tarea(
            self.tarea, self.responsable, delegado_a=self.delegado,
            desde=self.ahora - timedelta(hours=2), hasta=self.ahora - timedelta(hours=1),
        )
        self.assertFalse(es_responsable_actual(self.delegado, self.tarea))
        self.assertIsNone(delegado_actual(self.tarea))

    def test_historial_registra_delegada(self):
        delegar_tarea(
            self.tarea, self.responsable, delegado_a=self.delegado,
            desde=self.ahora, hasta=self.ahora + timedelta(hours=1),
        )
        self.assertTrue(self.tarea.historial.filter(tipo_evento=HistorialTarea.TipoEvento.DELEGADA).exists())


class IniciarCompletarTests(TestCase):
    def setUp(self):
        self.responsable = Usuario.objects.create_user("resp2", password=CLAVE_PRUEBA)

    def test_flujo_pendiente_en_progreso_completada(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        tarea = iniciar_tarea(tarea, self.responsable)
        self.assertEqual(tarea.estado, Tarea.Estado.EN_PROGRESO)
        self.assertIsNotNone(tarea.iniciada_en)

        tarea = completar_tarea(tarea, self.responsable)
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)
        self.assertEqual(tarea.completada_por_id, self.responsable.id)
        self.assertIsNotNone(tarea.completada_en)

    def test_completar_sin_iniciar_esta_permitido(self):
        """RQF-073 no exige pasar por EN_PROGRESO antes de completar —
        ninguna RN lo impone (determinación explícita, ver
        `apps.tareas.autorizacion.puede_completar_tarea`)."""
        tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        tarea = completar_tarea(tarea, self.responsable)
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)

    def test_iniciar_por_quien_no_es_responsable_falla(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        extrano = Usuario.objects.create_user("extrano2", password=CLAVE_PRUEBA)
        with self.assertRaises(PermissionDenied):
            iniciar_tarea(tarea, extrano)

    def test_completar_por_quien_solo_tiene_gestionar_falla(self):
        """`tareas.gestionar` no permite completar trabajo ajeno
        (instrucción explícita)."""
        gestor = Usuario.objects.create_user("gestor2", password=CLAVE_PRUEBA)
        _otorgar_permiso(gestor, PERMISO_GESTIONAR)
        tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        with self.assertRaises(PermissionDenied):
            completar_tarea(tarea, gestor)

    def test_doble_completar_falla(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        completar_tarea(tarea, self.responsable)
        with self.assertRaises(ValidationError):
            completar_tarea(tarea, self.responsable)

    def test_historial_registra_iniciada_y_completada(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        iniciar_tarea(tarea, self.responsable)
        completar_tarea(tarea, self.responsable)
        eventos = set(tarea.historial.values_list("tipo_evento", flat=True))
        self.assertIn(HistorialTarea.TipoEvento.INICIADA, eventos)
        self.assertIn(HistorialTarea.TipoEvento.COMPLETADA, eventos)
        self.assertNotIn("CREADA", eventos)


class SubtareaTests(TestCase):
    def setUp(self):
        self.responsable = Usuario.objects.create_user("resp3", password=CLAVE_PRUEBA)

    def test_crear_subtarea_permitida(self):
        padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=True)
        hija = crear_subtarea(padre, self.responsable, titulo="Hija", usuario_responsable=self.responsable)
        self.assertEqual(hija.tarea_padre_id, padre.id)
        self.assertFalse(hija.permite_subtareas)

    def test_crear_subtarea_no_permitida_por_configuracion(self):
        padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=False)
        with self.assertRaises(PermissionDenied):
            crear_subtarea(padre, self.responsable, titulo="Hija")

    def test_sub_subtarea_rechazada(self):
        padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=True)
        hija = crear_subtarea(padre, self.responsable, titulo="Hija", usuario_responsable=self.responsable)
        # Aunque se fuerce permite_subtareas en la hija, la profundidad la bloquea igual (defensa V1).
        hija.permite_subtareas = True
        hija.save()
        with self.assertRaises(PermissionDenied):
            crear_subtarea(hija, self.responsable, titulo="Nieta")

    def test_padre_con_subtarea_pendiente_no_completa(self):
        padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=True)
        crear_subtarea(padre, self.responsable, titulo="Hija", usuario_responsable=self.responsable)
        with self.assertRaises(ValidationError):
            completar_tarea(padre, self.responsable)

    def test_padre_completa_tras_completar_subtareas(self):
        padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=True)
        hija = crear_subtarea(padre, self.responsable, titulo="Hija", usuario_responsable=self.responsable)
        completar_tarea(hija, self.responsable)
        padre = completar_tarea(padre, self.responsable)
        self.assertEqual(padre.estado, Tarea.Estado.COMPLETADA)

    def test_subtarea_tiene_responsable_y_estado_propios(self):
        padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=True)
        otro = Usuario.objects.create_user("otro4", password=CLAVE_PRUEBA)
        hija = crear_subtarea(padre, self.responsable, titulo="Hija", usuario_responsable=otro)
        self.assertNotEqual(hija.usuario_responsable_id, padre.usuario_responsable_id)
        hija = iniciar_tarea(hija, otro)
        padre.refresh_from_db()
        self.assertEqual(padre.estado, Tarea.Estado.PENDIENTE)
        self.assertEqual(hija.estado, Tarea.Estado.EN_PROGRESO)


class ComentarioEvidenciaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_dir = tempfile.mkdtemp(prefix="orbita_tareas_tests_")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_dir)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.responsable = Usuario.objects.create_user("resp4", password=CLAVE_PRUEBA)
        self.tarea = _crear_tarea_simple(usuario_responsable=self.responsable)

    def test_comentar_tarea_por_responsable(self):
        comentario = comentar_tarea(self.tarea, self.responsable, "Avance parcial")
        self.assertEqual(comentario.autor_id, self.responsable.id)

    def test_comentar_tarea_sin_relacion_falla(self):
        extrano = Usuario.objects.create_user("extrano3", password=CLAVE_PRUEBA)
        with self.assertRaises(PermissionDenied):
            comentar_tarea(self.tarea, extrano, "Intento no autorizado")

    def test_comentar_tarea_completada_falla(self):
        """Una Tarea COMPLETADA ya no está "abierta a interacción" — mismo
        criterio que `puede_comentar_ticket`: `puede_comentar_tarea`
        devuelve `False` para cualquiera, incluido el propio responsable,
        así que la operación falla con `PermissionDenied` (autorización),
        no `ValidationError` (dato inválido)."""
        self.tarea = completar_tarea(self.tarea, self.responsable)
        with self.assertRaises(PermissionDenied):
            comentar_tarea(self.tarea, self.responsable, "Tarde")

    def test_adjuntar_evidencia_a_tarea(self):
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        adjunto = adjuntar_evidencia_tarea(self.responsable, archivo, tarea=self.tarea)
        self.assertEqual(adjunto.tipo_relacion, AdjuntoTarea.TipoRelacion.TAREA)

    def test_adjuntar_evidencia_vacia_falla(self):
        archivo = SimpleUploadedFile("vacio.txt", b"", content_type="text/plain")
        with self.assertRaises(ValidationError):
            adjuntar_evidencia_tarea(self.responsable, archivo, tarea=self.tarea)

    def test_adjuntar_evidencia_a_comentario(self):
        comentario = comentar_tarea(self.tarea, self.responsable, "Con evidencia")
        archivo = SimpleUploadedFile("evidencia2.txt", b"contenido", content_type="text/plain")
        adjunto = adjuntar_evidencia_tarea(self.responsable, archivo, comentario=comentario)
        self.assertEqual(adjunto.tipo_relacion, AdjuntoTarea.TipoRelacion.COMENTARIO)

    def test_adjuntar_evidencia_exige_exactamente_un_padre(self):
        archivo = SimpleUploadedFile("x.txt", b"contenido", content_type="text/plain")
        with self.assertRaises(ValueError):
            adjuntar_evidencia_tarea(self.responsable, archivo)


class ConsultasTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user("consulta1", password=CLAVE_PRUEBA)
        self.equipo = Equipo.objects.create(nombre="Equipo Consultas")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.usuario)

    def test_tareas_asignadas_a_incluye_directas_y_de_equipo(self):
        directa = _crear_tarea_simple(titulo="Directa", usuario_responsable=self.usuario)
        de_equipo = _crear_tarea_simple(titulo="De equipo", equipo_responsable=self.equipo)
        ajena = _crear_tarea_simple(titulo="Ajena")

        resultado = set(tareas_asignadas_a(self.usuario).values_list("id", flat=True))
        self.assertEqual(resultado, {directa.id, de_equipo.id})
        self.assertNotIn(ajena.id, resultado)

    def test_tareas_disponibles_para_tomar_respeta_equipo(self):
        disponible = _crear_tarea_simple(titulo="Disponible", equipo_responsable=self.equipo)
        ya_tomada = _crear_tarea_simple(titulo="Tomada", equipo_responsable=self.equipo, usuario_responsable=self.usuario)
        otro_equipo = Equipo.objects.create(nombre="Otro equipo")
        ajena = _crear_tarea_simple(titulo="De otro equipo", equipo_responsable=otro_equipo)

        resultado = set(tareas_disponibles_para_tomar(self.usuario).values_list("id", flat=True))
        self.assertEqual(resultado, {disponible.id})
        self.assertNotIn(ya_tomada.id, resultado)
        self.assertNotIn(ajena.id, resultado)

    def test_tareas_del_equipo(self):
        tarea = _crear_tarea_simple(equipo_responsable=self.equipo)
        self.assertIn(tarea, tareas_del_equipo(self.equipo))

    def test_tareas_vencidas_excluye_completadas(self):
        vencida = _crear_tarea_simple(titulo="Vencida", fecha_limite=timezone.now() - timedelta(days=1))
        vencida_pero_completada = _crear_tarea_simple(
            titulo="Vencida completada", fecha_limite=timezone.now() - timedelta(days=1),
            usuario_responsable=self.usuario,
        )
        completar_tarea(vencida_pero_completada, self.usuario)
        vigente = _crear_tarea_simple(titulo="Vigente", fecha_limite=timezone.now() + timedelta(days=1))

        resultado = set(tareas_vencidas().values_list("id", flat=True))
        self.assertIn(vencida.id, resultado)
        self.assertNotIn(vencida_pero_completada.id, resultado)
        self.assertNotIn(vigente.id, resultado)

    def test_tareas_visibles_para_con_permiso_consultar_ve_todo(self):
        _otorgar_permiso(self.usuario, PERMISO_CONSULTAR)
        ajena = _crear_tarea_simple(titulo="Cualquiera")
        self.assertIn(ajena, tareas_visibles_para(self.usuario))


class ConcurrenciaCompletarTareaTests(TransactionTestCase):
    """Doble `completar_tarea` concurrente sobre la MISMA tarea — solo uno
    debe ganar, mismo patrón (`TransactionTestCase` + hilos reales contra
    Postgres) que `apps.workflows.tests.ConcurrenciaReanudarInstanciaTests`."""

    def setUp(self):
        self.responsable = Usuario.objects.create_user("concurrencia_tareas", password=CLAVE_PRUEBA)
        self.tarea = _crear_tarea_simple(usuario_responsable=self.responsable)

    def test_doble_completar_concurrente_solo_uno_avanza(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _intentar_completar(clave):
            barrera.wait()
            try:
                tarea = Tarea.objects.get(pk=self.tarea.pk)
                completar_tarea(tarea, self.responsable)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_completada"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_intentar_completar, args=("a",))
        hilo_b = threading.Thread(target=_intentar_completar, args=("b",))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_completada"), 1)

        tarea = Tarea.objects.get(pk=self.tarea.pk)
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)


# ============================================================================
# 3.UI.1 — Pruebas HTTP (vistas/URLs/Forms). Verifican la integración
# vista→dominio, nunca reglas de dominio ya cubiertas arriba (ese es el
# criterio explícito: "no dupliques tests de dominio ya existentes salvo
# que sea necesario verificar la integración HTTP").
# ============================================================================


def _crear_tarea_con_responsable(**kwargs):
    responsable = kwargs.pop("usuario_responsable", None) or Usuario.objects.create_user(
        f"resp_{Usuario.objects.count()}", password=CLAVE_PRUEBA
    )
    tarea = _crear_tarea_simple(usuario_responsable=responsable, **kwargs)
    return tarea, responsable


class AutenticacionRequeridaTests(TestCase):
    def test_bandeja_requiere_login(self):
        respuesta = self.client.get(reverse("tareas:lista"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login/", respuesta.url)

    def test_detalle_requiere_login(self):
        tarea = _crear_tarea_simple()
        respuesta = self.client.get(reverse("tareas:detalle", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login/", respuesta.url)


class BandejaViewTests(TestCase):
    def setUp(self):
        self.usuario = Usuario.objects.create_user("bandeja1", password=CLAVE_PRUEBA)
        self.equipo = Equipo.objects.create(nombre="Equipo bandeja")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.usuario)
        self.client.login(username="bandeja1", password=CLAVE_PRUEBA)

    def test_bandeja_renderiza_y_filtra_solo_asignadas(self):
        mia = _crear_tarea_simple(titulo="Mía", usuario_responsable=self.usuario)
        ajena = _crear_tarea_simple(titulo="Ajena")
        respuesta = self.client.get(reverse("tareas:lista"))
        self.assertEqual(respuesta.status_code, 200)
        ids = {fila["tarea"].id for fila in respuesta.context["filas"]}
        self.assertIn(mia.id, ids)
        self.assertNotIn(ajena.id, ids)

    def test_bandeja_tab_disponibles_usa_consulta_existente(self):
        disponible = _crear_tarea_simple(titulo="Disponible", equipo_responsable=self.equipo)
        respuesta = self.client.get(reverse("tareas:lista") + "?tab=disponibles")
        self.assertEqual(respuesta.context["tab"], "disponibles")
        ids = {fila["tarea"].id for fila in respuesta.context["filas"]}
        self.assertIn(disponible.id, ids)

    def test_bandeja_empty_state(self):
        respuesta = self.client.get(reverse("tareas:lista"))
        self.assertContains(respuesta, "No tienes tareas asignadas.")

    def test_vencimiento_se_presenta_correctamente(self):
        vencida = _crear_tarea_simple(
            titulo="Vencida", usuario_responsable=self.usuario, fecha_limite=timezone.now() - timedelta(days=1)
        )
        vigente = _crear_tarea_simple(
            titulo="Vigente", usuario_responsable=self.usuario, fecha_limite=timezone.now() + timedelta(days=1)
        )
        respuesta = self.client.get(reverse("tareas:lista"))
        por_id = {fila["tarea"].id: fila["vencida"] for fila in respuesta.context["filas"]}
        self.assertTrue(por_id[vencida.id])
        self.assertFalse(por_id[vigente.id])


class DetalleViewTests(TestCase):
    def setUp(self):
        self.responsable = Usuario.objects.create_user("detalle_resp", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user("detalle_ajeno", password=CLAVE_PRUEBA)
        self.tarea = _crear_tarea_simple(usuario_responsable=self.responsable)

    def test_detalle_autorizado_para_responsable(self):
        self.client.login(username="detalle_resp", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:detalle", args=[self.tarea.pk]))
        self.assertEqual(respuesta.status_code, 200)

    def test_detalle_no_autorizado_devuelve_403(self):
        self.client.login(username="detalle_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:detalle", args=[self.tarea.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_acciones_ocultas_cuando_no_corresponden(self):
        """Un usuario con visibilidad amplia (`tareas.consultar`) pero sin
        relación operacional real: ve el detalle, pero ningún `puede_*`
        de acción es verdadero — ni un botón se renderiza sin el respaldo
        real de autorización."""
        observador = Usuario.objects.create_user("detalle_observador", password=CLAVE_PRUEBA)
        _otorgar_permiso(observador, PERMISO_CONSULTAR)
        self.client.login(username="detalle_observador", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:detalle", args=[self.tarea.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(respuesta.context["puede_tomar"])
        self.assertFalse(respuesta.context["puede_iniciar"])
        self.assertFalse(respuesta.context["puede_completar"])
        self.assertFalse(respuesta.context["puede_asignar"])
        self.assertFalse(respuesta.context["puede_reasignar"])
        self.assertFalse(respuesta.context["puede_delegar"])
        self.assertFalse(respuesta.context["puede_comentar"])
        self.assertNotContains(respuesta, 'action="' + reverse("tareas:completar", args=[self.tarea.pk]))


class AccionesViewTests(TestCase):
    def setUp(self):
        self.gestor = Usuario.objects.create_user("accion_gestor", password=CLAVE_PRUEBA)
        _otorgar_permiso(self.gestor, PERMISO_GESTIONAR)
        self.equipo = Equipo.objects.create(nombre="Equipo acciones")
        self.miembro = Usuario.objects.create_user("accion_miembro", password=CLAVE_PRUEBA)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.miembro)
        self.ajeno = Usuario.objects.create_user("accion_ajeno", password=CLAVE_PRUEBA)

    def test_tomar_exitoso_por_miembro_de_equipo(self):
        tarea = _crear_tarea_simple(equipo_responsable=self.equipo)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:tomar", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(respuesta.url, reverse("tareas:detalle", args=[tarea.pk]))
        tarea.refresh_from_db()
        self.assertEqual(tarea.usuario_responsable_id, self.miembro.id)

    def test_tomar_no_autorizado_devuelve_403(self):
        tarea = _crear_tarea_simple(equipo_responsable=self.equipo)
        self.client.login(username="accion_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:tomar", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 403)
        tarea.refresh_from_db()
        self.assertIsNone(tarea.usuario_responsable_id)

    def test_post_directo_sin_pasar_por_detalle_tambien_es_403(self):
        """Confirma explícitamente la regla obligatoria: un POST directo a
        la URL de acción, sin haber visitado nunca el detalle (sin botón
        que "esconder"), es rechazado igual — la autorización no depende
        de la vista GET."""
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        self.client.login(username="accion_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:completar", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_iniciar_exitoso(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:iniciar", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 302)
        tarea.refresh_from_db()
        self.assertEqual(tarea.estado, Tarea.Estado.EN_PROGRESO)

    def test_completar_exitoso_sin_vinculo_workflow(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:completar", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 302)
        tarea.refresh_from_db()
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)
        self.assertEqual(tarea.completada_por_id, self.miembro.id)

    def test_asignar_exitoso(self):
        tarea = _crear_tarea_simple()
        self.client.login(username="accion_gestor", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:asignar", args=[tarea.pk]), {"usuario": self.miembro.pk})
        self.assertEqual(respuesta.status_code, 302)
        tarea.refresh_from_db()
        self.assertEqual(tarea.usuario_responsable_id, self.miembro.id)

    def test_reasignar_exitoso(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        otro = Usuario.objects.create_user("accion_otro", password=CLAVE_PRUEBA)
        self.client.login(username="accion_gestor", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:reasignar", args=[tarea.pk]), {"usuario": otro.pk})
        self.assertEqual(respuesta.status_code, 302)
        tarea.refresh_from_db()
        self.assertEqual(tarea.usuario_responsable_id, otro.id)

    def test_delegar_exitoso(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        delegado = Usuario.objects.create_user("accion_delegado", password=CLAVE_PRUEBA)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        desde = timezone.now() + timedelta(hours=1)
        hasta = desde + timedelta(days=1)
        respuesta = self.client.post(
            reverse("tareas:delegar", args=[tarea.pk]),
            {
                "delegado_a": delegado.pk,
                "desde": desde.strftime("%Y-%m-%dT%H:%M"),
                "hasta": hasta.strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(DelegacionTarea.objects.filter(tarea=tarea, delegado_a=delegado).exists())

    def test_comentario_exitoso(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:comentar", args=[tarea.pk]), {"contenido": "Avance registrado."})
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(ComentarioTarea.objects.filter(tarea=tarea, contenido="Avance registrado.").exists())

    def test_evidencia_exitosa(self):
        tarea = _crear_tarea_simple(usuario_responsable=self.miembro)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido", content_type="text/plain")
        respuesta = self.client.post(reverse("tareas:adjuntar_evidencia", args=[tarea.pk]), {"archivo": archivo})
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(AdjuntoTarea.objects.filter(tarea=tarea, nombre_original="evidencia.txt").exists())

    def test_formulario_invalido_no_altera_dominio(self):
        """Ni usuario ni equipo en el POST de asignación: el Form rechaza
        la entrada antes de tocar el dominio — la tarea permanece exactamente
        igual, sin excepción no controlada."""
        tarea = _crear_tarea_simple()
        self.client.login(username="accion_gestor", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:asignar", args=[tarea.pk]), {})
        self.assertEqual(respuesta.status_code, 302)
        tarea.refresh_from_db()
        self.assertIsNone(tarea.usuario_responsable_id)

    def test_prg_tras_operacion_exitosa(self):
        tarea = _crear_tarea_simple(equipo_responsable=self.equipo)
        self.client.login(username="accion_miembro", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:tomar", args=[tarea.pk]), follow=True)
        self.assertRedirects(respuesta, reverse("tareas:detalle", args=[tarea.pk]))
        mensajes = [str(m) for m in respuesta.context["messages"]]
        self.assertIn("Tarea tomada.", mensajes)


class SubtareaViewTests(TestCase):
    def setUp(self):
        self.responsable = Usuario.objects.create_user("subtarea_resp", password=CLAVE_PRUEBA)
        self.padre = _crear_tarea_simple(usuario_responsable=self.responsable, permite_subtareas=True)
        self.client.login(username="subtarea_resp", password=CLAVE_PRUEBA)

    def test_crear_subtarea_exitosa(self):
        respuesta = self.client.post(
            reverse("tareas:crear_subtarea", args=[self.padre.pk]), {"titulo": "Subtarea 1"}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(Tarea.objects.filter(tarea_padre=self.padre, titulo="Subtarea 1").exists())

    def test_completar_subtarea_via_vista(self):
        subtarea = crear_subtarea(
            self.padre, self.responsable, titulo="Sub a completar", usuario_responsable=self.responsable
        )
        respuesta = self.client.post(reverse("tareas:completar", args=[subtarea.pk]))
        self.assertEqual(respuesta.status_code, 302)
        subtarea.refresh_from_db()
        self.assertEqual(subtarea.estado, Tarea.Estado.COMPLETADA)


class EvidenciaDescargaProtegidaTests(TestCase):
    def setUp(self):
        self.responsable = Usuario.objects.create_user("descarga_resp", password=CLAVE_PRUEBA)
        self.ajeno = Usuario.objects.create_user("descarga_ajeno", password=CLAVE_PRUEBA)
        self.tarea = _crear_tarea_simple(usuario_responsable=self.responsable)
        archivo = SimpleUploadedFile("evidencia.txt", b"contenido-protegido", content_type="text/plain")
        self.adjunto = adjuntar_evidencia_tarea(self.responsable, archivo, tarea=self.tarea)

    def test_descarga_autorizada(self):
        self.client.login(username="descarga_resp", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:descargar_evidencia", args=[self.adjunto.pk]))
        self.assertEqual(respuesta.status_code, 200)

    def test_descarga_no_autorizada_devuelve_403(self):
        self.client.login(username="descarga_ajeno", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("tareas:descargar_evidencia", args=[self.adjunto.pk]))
        self.assertEqual(respuesta.status_code, 403)


# ----------------------------------------------------------------------------
# Excepción documentada (ver docstring de este módulo y de
# `apps/tareas/views.py::completar_view`): única clase de este archivo que
# importa `apps.workflows`, para probar la integración HTTP real de
# "completar una Tarea principal de Workflow" — sin esto, la instrucción
# explícita de 3.UI.1 ("revisa la integración final del Sprint 3... no
# llames a una operación base que deje Tarea COMPLETADA / Workflow
# EN_ESPERA") quedaría sin verificación end-to-end.
# ----------------------------------------------------------------------------

from apps.workflows.models import (  # noqa: E402  (import tardío intencional, ver nota arriba)
    ConfiguracionEtapaTarea as _ConfiguracionEtapaTarea,
    Etapa as _Etapa,
    InstanciaWorkflow as _InstanciaWorkflow,
    TareaWorkflow as _TareaWorkflow,
    TransicionEtapa as _TransicionEtapa,
    Workflow as _Workflow,
    WorkflowVersion as _WorkflowVersion,
)
from apps.workflows.motor import iniciar_workflow as _iniciar_workflow  # noqa: E402
from apps.workflows.versionamiento import activar_version as _activar_version  # noqa: E402


def _crear_workflow_con_tarea_vinculada(actor, usuario_responsable, *, permite_subtareas=False):
    """Fixture mínima — duplicado deliberado y reducido de
    `apps.workflows.tests._crear_workflow_activo_con_tarea` (no se importa
    ese módulo de pruebas como si fuera una librería)."""
    workflow = _Workflow.objects.create(nombre="3.UI.1 — integración HTTP")
    version = _WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = _Etapa.objects.create(version=version, tipo=_Etapa.Tipo.INICIO, nombre="Inicio")
    tarea_etapa = _Etapa.objects.create(version=version, tipo=_Etapa.Tipo.TAREA, nombre="Revisar")
    fin = _Etapa.objects.create(version=version, tipo=_Etapa.Tipo.FIN, nombre="Fin")
    _TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea_etapa)
    _TransicionEtapa.objects.create(etapa_origen=tarea_etapa, etapa_destino=fin)
    _ConfiguracionEtapaTarea.objects.create(
        etapa=tarea_etapa,
        tipo_responsable=_ConfiguracionEtapaTarea.TipoResponsable.USUARIO,
        usuario_responsable=usuario_responsable,
        permite_subtareas=permite_subtareas,
    )
    _activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    instancia = _iniciar_workflow(workflow, actor=actor)
    tarea_vinculada = _TareaWorkflow.objects.get(instancia_etapa__instancia_workflow=instancia).tarea
    return instancia, tarea_vinculada


class CompletarTareaWorkflowIntegracionTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user("integracion_actor", password=CLAVE_PRUEBA)
        self.responsable = Usuario.objects.create_user("integracion_resp", password=CLAVE_PRUEBA)

    def test_completar_tarea_principal_de_workflow_continua_la_instancia(self):
        instancia, tarea = _crear_workflow_con_tarea_vinculada(self.actor, self.responsable)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, _InstanciaWorkflow.Estado.EN_ESPERA)

        self.client.login(username="integracion_resp", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:completar", args=[tarea.pk]))
        self.assertEqual(respuesta.status_code, 302)

        tarea.refresh_from_db()
        instancia.refresh_from_db()
        self.assertEqual(tarea.estado, Tarea.Estado.COMPLETADA)
        # INICIO → TAREA → FIN: al completar la única TAREA, el motor debe
        # avanzar automáticamente hasta FIN — la instancia NUNCA debe quedar
        # "Tarea COMPLETADA / Workflow EN_ESPERA" (instrucción explícita).
        self.assertEqual(instancia.estado, _InstanciaWorkflow.Estado.COMPLETADA)

    def test_completar_subtarea_no_reanuda_workflow_de_la_tarea_principal(self):
        instancia, tarea_principal = _crear_workflow_con_tarea_vinculada(
            self.actor, self.responsable, permite_subtareas=True
        )
        subtarea = crear_subtarea(
            tarea_principal,
            self.responsable,
            titulo="Sub de tarea de workflow",
            usuario_responsable=self.responsable,
        )

        self.client.login(username="integracion_resp", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("tareas:completar", args=[subtarea.pk]))
        self.assertEqual(respuesta.status_code, 302)

        subtarea.refresh_from_db()
        instancia.refresh_from_db()
        self.assertEqual(subtarea.estado, Tarea.Estado.COMPLETADA)
        # La subtarea nunca tiene `TareaWorkflow` (W.6) — completarla no debe
        # tocar el Workflow de la tarea principal en absoluto.
        self.assertEqual(instancia.estado, _InstanciaWorkflow.Estado.EN_ESPERA)
        tarea_principal.refresh_from_db()
        self.assertEqual(tarea_principal.estado, Tarea.Estado.PENDIENTE)
