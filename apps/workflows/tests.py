"""Pruebas de Workflows.

3.1 — Definición, configuración y versionamiento (CU-020 "Diseñar y
versionar workflow", RQF-063/064/069/070, RN-020/021): versionamiento
(crear/clonar/activar/inmutabilidad), validación estructural
(`apps.workflows.validacion.validar_estructura`), integridad de
`TransicionEtapa` (condición/fallback, misma versión), ciclos permitidos,
clonación preservando topología, auditoría de activación y autorización.

3.2 — Motor de ejecución (CU-020 "Ejecutar workflow" — mismo código,
distinto CU en la fuente Excel, W.10, ver `apps/workflows/motor.py`),
RQF-065/066/067/068, RN-020/021): `iniciar_workflow`/`avanzar_instancia`/
`reanudar_instancia`, INICIO/HITO/FIN automáticos, CONDICION delega en su
Strategy (RN-021), ESPERA/reanudación con revalidación bajo lock, ciclos
ejecutando la misma `Etapa` varias veces sin perder contexto, protección
técnica de avance automático, errores funcionales/técnicos, autorización
(`workflows.ejecutar`, W.8) y concurrencia.

No hay entidad `Tarea` operacional (3.3) ni Strategies de `APROBACION`
(Sprint 4)/`GACETA` (Sprint 6) — fuera de alcance en ambos incrementos.

3.2.x — Reanudación automática de `ESPERA` vía Celery Beat (RQF-067):
`apps.workflows.tasks.reanudar_esperas_vencidas`. No implementa lógica de
Workflow — delega en `reanudar_instancia` (motor de 3.2, sin cambios
conceptuales); las pruebas de este incremento verifican selección de
candidatos, delegación, idempotencia y manejo de errores/concurrencia de la
tarea, no el motor en sí (ya cubierto por `MotorEsperaTests`/
`ConcurrenciaReanudarInstanciaTests`).

3.3 — Integración Tarea↔Workflow (CU-021/022/023, RQF-071 a 077): TAREA
pasa a `ejecutable=True` (`EstrategiaTarea`), `motivo_espera` distingue
esperas TEMPORAL/TAREA (W.3), y `apps.workflows.integracion.
completar_tarea_workflow` reanuda el motor vía el núcleo genérico
`continuar_espera_externa` (W.7). El dominio `Tarea` en sí (asignación,
delegación, subtareas, autorización, historial) se prueba íntegramente en
`apps/tareas/tests.py`, sin depender de Workflow — aquí solo se prueba la
INTEGRACIÓN entre ambos dominios."""

import threading
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion
from apps.aprobaciones.operaciones import crear_esquema_aprobacion
from apps.core.models import AsignacionRol, Equipo, MiembroEquipo, Permiso, RegistroAuditoria, RolFuncional, RolPermiso
from apps.tareas.models import Tarea
from apps.tareas.operaciones import completar_tarea, crear_subtarea
from apps.workflows.autorizacion import (
    puede_administrar_workflows,
    puede_consultar_workflows,
    puede_ejecutar_workflows,
)
from apps.workflows import editor
from apps.workflows import fases as fases_ops
from apps.workflows.estrategias import (
    ESTRATEGIAS_POR_TIPO,
    EstrategiaCondicion,
    EstrategiaSinConfiguracion,
    ResultadoEjecucion,
    ResultadoEjecucionEtapa,
)
from apps.workflows.integracion import completar_tarea_workflow, resolver_aprobacion_workflow
from apps.workflows.models import (
    ConfiguracionEtapaAprobacion,
    ConfiguracionEtapaTarea,
    Etapa,
    EsquemaAprobacionWorkflow,
    FaseWorkflow,
    InstanciaEtapa,
    InstanciaWorkflow,
    ParticipanteEtapaAprobacion,
    TareaWorkflow,
    TransicionFaseWorkflow,
    TransicionEtapa,
    Workflow,
    WorkflowVersion,
)
from apps.workflows.motor import (
    avanzar_instancia,
    continuar_espera_externa,
    iniciar_workflow,
    reanudar_instancia,
)
from apps.workflows.tasks import _candidatos_esperas_vencidas, reanudar_esperas_vencidas
from apps.workflows.validacion import validar_estructura
from apps.workflows.versionamiento import activar_version, crear_nueva_version, crear_workflow, editar_workflow

Usuario = get_user_model()
CLAVE_PRUEBA = "Clave-Segura-123"


def _otorgar_workflows_administrar(usuario):
    """Helper de pruebas: otorga `workflows.administrar` en alcance GLOBAL."""
    permiso, _ = Permiso.objects.get_or_create(
        codigo="workflows.administrar", defaults={"nombre": "Administrar workflows"}
    )
    rol = RolFuncional.objects.create(nombre=f"Rol workflows {usuario.username}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(
        usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
    )


def _otorgar_workflows_consultar(usuario):
    """Helper de pruebas (3.UI.3): otorga únicamente `workflows.consultar`
    en alcance GLOBAL — distinto de `_otorgar_workflows_administrar`, para
    probar que "administrar no se deduce de consultar" en la capa HTTP."""
    permiso, _ = Permiso.objects.get_or_create(
        codigo="workflows.consultar", defaults={"nombre": "Consultar workflows"}
    )
    rol = RolFuncional.objects.create(nombre=f"Rol lector workflows {usuario.username}")
    RolPermiso.objects.create(rol=rol, permiso=permiso)
    return AsignacionRol.objects.create(
        usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
    )


def _auditorias_de(instancia):
    return RegistroAuditoria.objects.filter(
        content_type=ContentType.objects.get_for_model(type(instancia)), object_id=instancia.pk
    )


def _crear_workflow_lineal(nombre="Workflow válido"):
    """INICIO → HITO → FIN — la topología mínima válida para activar,
    reutilizada por la mayoría de las pruebas. Usa HITO (no TAREA) para la
    etapa intermedia desde 3.2: TAREA sigue reconocida por el modelo pero
    ya no es activable (W.5) — la clave del dict y el `nombre` se conservan
    como "tarea"/"Tarea" solo como etiquetas heredadas de 3.1, sin relación
    con el tipo real de la etapa."""
    workflow = Workflow.objects.create(nombre=nombre)
    version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
    tarea = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Tarea")
    fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
    TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
    TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
    return workflow, version, {"inicio": inicio, "tarea": tarea, "fin": fin}


class PlantillaFasesR1Tests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="gestor_fases", password=CLAVE_PRUEBA)

    def _plantilla_lineal(self):
        workflow = fases_ops.crear_plantilla_fases(self.actor, nombre="Gestion estandar")
        version = workflow.versiones.get(numero=1)
        recepcion = fases_ops.agregar_fase(version, self.actor, nombre="Recepcion")
        ejecucion = fases_ops.agregar_fase(version, self.actor, nombre="Ejecucion")
        revision = fases_ops.agregar_fase(version, self.actor, nombre="Revision")
        cierre = fases_ops.agregar_fase(version, self.actor, nombre="Cierre")
        fases_ops.conectar_fases(recepcion, ejecucion, self.actor)
        fases_ops.conectar_fases(ejecucion, revision, self.actor)
        fases_ops.conectar_fases(revision, cierre, self.actor)
        return workflow, version, {
            "recepcion": recepcion,
            "ejecucion": ejecucion,
            "revision": revision,
            "cierre": cierre,
        }

    def test_crear_plantilla_nueva_y_fases(self):
        workflow = fases_ops.crear_plantilla_fases(self.actor, nombre="Gestion estandar")
        version = workflow.versiones.get(numero=1)

        fase = fases_ops.agregar_fase(version, self.actor, nombre="Recepcion", descripcion="Entrada")

        self.assertEqual(workflow.modo, Workflow.Modo.PLANTILLA_FASES)
        self.assertEqual(version.estado, WorkflowVersion.Estado.BORRADOR)
        self.assertEqual(fase.version_id, version.pk)
        self.assertEqual(fase.orden, 1)

    def test_transiciones_y_activacion_de_plantilla(self):
        workflow, version, fases = self._plantilla_lineal()

        activar_version(workflow, version, actor=self.actor)
        workflow.refresh_from_db()

        self.assertEqual(workflow.version_activa_id, version.pk)
        self.assertEqual(version.fases.count(), 4)
        self.assertEqual(
            fases["recepcion"].transiciones_salientes.get().fase_destino_id,
            fases["ejecucion"].pk,
        )

    def test_clona_fases_y_transiciones_al_crear_nueva_version(self):
        workflow, version, _fases = self._plantilla_lineal()
        activar_version(workflow, version, actor=self.actor)

        nueva = crear_nueva_version(workflow, actor=self.actor)

        self.assertEqual(nueva.fases.count(), 4)
        self.assertEqual(TransicionFaseWorkflow.objects.filter(fase_origen__version=nueva).count(), 3)
        self.assertFalse(nueva.etapas.exists())

    def test_version_activa_de_fases_es_inmutable(self):
        workflow, version, _fases = self._plantilla_lineal()
        activar_version(workflow, version, actor=self.actor)

        with self.assertRaises(ValidationError):
            fases_ops.agregar_fase(version, self.actor, nombre="Post cierre")

    def test_rechaza_transicion_con_fase_de_otra_version(self):
        _workflow1, _version1, fases1 = self._plantilla_lineal()
        _workflow2, _version2, fases2 = self._plantilla_lineal()

        with self.assertRaises(ValidationError):
            fases_ops.conectar_fases(fases1["cierre"], fases2["recepcion"], self.actor)

    def test_legacy_sigue_ejecutable_sin_usar_fases(self):
        workflow, version, _etapas = _crear_workflow_lineal("Legacy operativo")
        activar_version(workflow, version, actor=self.actor)

        instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertEqual(workflow.modo, Workflow.Modo.LEGACY_EJECUTABLE)
        self.assertEqual(instancia.workflow_version_id, version.pk)
        self.assertFalse(FaseWorkflow.objects.filter(version=version).exists())


class VersionamientoTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="gestor31", password=CLAVE_PRUEBA)

    def test_crear_workflow(self):
        workflow = Workflow.objects.create(nombre="Aprobación de compras")
        self.assertIsNone(workflow.version_activa)
        self.assertEqual(str(workflow), "Aprobación de compras")

    def test_crear_version_vacia(self):
        workflow = Workflow.objects.create(nombre="Workflow nuevo")
        version = crear_nueva_version(workflow, actor=self.actor)
        self.assertEqual(version.numero, 1)
        self.assertEqual(version.estado, WorkflowVersion.Estado.BORRADOR)
        self.assertEqual(version.etapas.count(), 0)

    def test_crear_version_clona_desde_activa_por_defecto(self):
        workflow, version1, _ = _crear_workflow_lineal()
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()

        version2 = crear_nueva_version(workflow, actor=self.actor)
        self.assertEqual(version2.numero, 2)
        self.assertEqual(version2.etapas.count(), 3)
        self.assertEqual(
            TransicionEtapa.objects.filter(etapa_origen__version=version2).count(), 2
        )

    def test_editar_etapa_en_borrador_permitido(self):
        _, version, etapas = _crear_workflow_lineal()
        etapas["tarea"].nombre = "Tarea renombrada"
        etapas["tarea"].save()
        etapas["tarea"].refresh_from_db()
        self.assertEqual(etapas["tarea"].nombre, "Tarea renombrada")

    def test_activar_version_valida(self):
        workflow, version, _ = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)
        version.refresh_from_db()
        workflow.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.ACTIVA)
        self.assertEqual(workflow.version_activa_id, version.pk)

    def test_activar_congela_version_anterior_como_historica(self):
        workflow, version1, _ = _crear_workflow_lineal()
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()

        version2 = crear_nueva_version(workflow, actor=self.actor)
        activar_version(workflow, version2, actor=self.actor)

        version1.refresh_from_db()
        version2.refresh_from_db()
        workflow.refresh_from_db()
        self.assertEqual(version1.estado, WorkflowVersion.Estado.HISTORICA)
        self.assertEqual(version2.estado, WorkflowVersion.Estado.ACTIVA)
        self.assertEqual(workflow.version_activa_id, version2.pk)

    def test_version_anterior_permanece_intacta_tras_activar_otra(self):
        workflow, version1, etapas1 = _crear_workflow_lineal()
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()

        version2 = crear_nueva_version(workflow, actor=self.actor)
        tarea_v2 = version2.etapas.get(tipo=Etapa.Tipo.HITO)
        tarea_v2.nombre = "Tarea distinta en v2"
        tarea_v2.save()
        activar_version(workflow, version2, actor=self.actor)

        etapas1["tarea"].refresh_from_db()
        self.assertEqual(etapas1["tarea"].nombre, "Tarea")

    def test_activar_solo_permitido_desde_borrador(self):
        workflow, version, _ = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)
        with self.assertRaises(ValueError):
            activar_version(workflow, version, actor=self.actor)

    def test_activar_version_de_otro_workflow_falla(self):
        workflow1, version1, _ = _crear_workflow_lineal("Workflow 1")
        workflow2, _, _ = _crear_workflow_lineal("Workflow 2")
        with self.assertRaises(ValueError):
            activar_version(workflow2, version1, actor=self.actor)

    def test_eliminar_etapa_en_borrador_permitido(self):
        _, version, etapas = _crear_workflow_lineal()
        etapas["tarea"].delete()
        self.assertEqual(version.etapas.count(), 2)


class InmutabilidadTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="gestor31b", password=CLAVE_PRUEBA)

    def test_editar_etapa_en_version_activa_falla(self):
        workflow, version, etapas = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)
        etapas["tarea"].nombre = "Intento de cambio"
        with self.assertRaises(ValidationError):
            etapas["tarea"].save()

    def test_crear_etapa_en_version_activa_falla(self):
        workflow, version, _ = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)
        with self.assertRaises(ValidationError):
            Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Nueva")

    def test_eliminar_transicion_en_version_activa_falla(self):
        workflow, version, etapas = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)
        transicion = TransicionEtapa.objects.get(etapa_origen=etapas["inicio"])
        with self.assertRaises(ValidationError):
            transicion.delete()

    def test_editar_version_historica_falla(self):
        workflow, version1, _ = _crear_workflow_lineal()
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()
        crear_nueva_version(workflow, actor=self.actor)
        version1.refresh_from_db()  # ahora HISTORICA
        with self.assertRaises(ValidationError):
            Etapa.objects.create(version=version1, tipo=Etapa.Tipo.TAREA, nombre="No debería crear")


class EstructuraValidacionTests(TestCase):
    def test_workflow_sin_etapas_es_invalido(self):
        workflow = Workflow.objects.create(nombre="Vacío")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        errores = validar_estructura(version)
        self.assertTrue(errores)

    def test_exactamente_un_inicio_requerido_cero(self):
        workflow = Workflow.objects.create(nombre="Sin inicio")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        errores = validar_estructura(version)
        self.assertTrue(any("INICIO" in e for e in errores))

    def test_exactamente_un_inicio_requerido_dos(self):
        _, version, _ = _crear_workflow_lineal()
        Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Segundo inicio")
        errores = validar_estructura(version)
        self.assertTrue(any("exactamente una etapa INICIO" in e for e in errores))

    def test_al_menos_un_fin_requerido(self):
        workflow = Workflow.objects.create(nombre="Sin fin")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        errores = validar_estructura(version)
        self.assertTrue(any("al menos una etapa FIN" in e for e in errores))

    def test_etapa_ordinaria_sin_salida_no_es_fin_valida(self):
        """Una TAREA sin transición saliente (callejón sin salida) no debe
        confundirse con una terminación formal: sigue faltando un FIN."""
        workflow = Workflow.objects.create(nombre="Callejón sin salida")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Sin salida")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        errores = validar_estructura(version)
        self.assertTrue(any("al menos una etapa FIN" in e for e in errores))
        with self.assertRaises(ValidationError):
            activar_version(
                version.workflow, version, actor=Usuario.objects.create_user("x1", password=CLAVE_PRUEBA)
            )

    def test_inicio_sin_entradas_bloqueado_al_guardar(self):
        _, version, etapas = _crear_workflow_lineal()
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(etapa_origen=etapas["tarea"], etapa_destino=etapas["inicio"])

    def test_fin_sin_salidas_bloqueado_al_guardar(self):
        _, version, etapas = _crear_workflow_lineal()
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(etapa_origen=etapas["fin"], etapa_destino=etapas["tarea"])

    def test_inicio_sin_entradas_detectado_por_validar_estructura_si_se_bypasea_save(self):
        """`clean()`/`save()` protegen la vía ordinaria (RQF-046, mismo
        criterio ya documentado para Formulario) — `bulk_create` la
        bypasea; `validar_estructura` es la segunda capa que igual lo
        detecta."""
        _, version, etapas = _crear_workflow_lineal()
        TransicionEtapa.objects.bulk_create(
            [TransicionEtapa(etapa_origen=etapas["tarea"], etapa_destino=etapas["inicio"])]
        )
        errores = validar_estructura(version)
        self.assertTrue(any("INICIO" in e and "entrantes" in e for e in errores))

    def test_etapa_inalcanzable_detectada(self):
        _, version, etapas = _crear_workflow_lineal()
        Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Huérfana")
        errores = validar_estructura(version)
        self.assertTrue(any("Huérfana" in e and "alcanzables" in e for e in errores))

    def test_aprobacion_disponible_para_activar(self):
        """3.4 (diseño aprobado): APROBACION deja de ser un tipo bloqueado
        — `EstrategiaAprobacion.ejecutable=True` (a diferencia de 3.1-3.3,
        donde este mismo test comprobaba lo contrario). Requiere sus 3
        transiciones nombradas por `resultado_aprobacion` — nunca
        `variable`/`operador`/`valor`/`es_fallback` (eso sigue siendo
        exclusivo de CONDICION)."""
        actor = Usuario.objects.create_user("x2", password=CLAVE_PRUEBA)
        workflow, _ = _crear_workflow_activo_con_aprobacion(
            actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, actor)],
        )
        workflow.version_activa.refresh_from_db()
        self.assertEqual(workflow.version_activa.estado, WorkflowVersion.Estado.ACTIVA)

    def test_aprobacion_sin_configuracion_no_disponible(self):
        workflow = Workflow.objects.create(nombre="Aprobación sin config")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        for resultado in ("APROBADA", "RECHAZADA", "DEVUELTA"):
            TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion=resultado)
        errores = validar_estructura(version)
        self.assertTrue(any("no tiene ConfiguracionEtapaAprobacion" in e for e in errores))

    def test_aprobacion_sin_participantes_no_disponible(self):
        workflow = Workflow.objects.create(nombre="Aprobación sin participantes")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        for resultado in ("APROBADA", "RECHAZADA", "DEVUELTA"):
            TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion=resultado)
        ConfiguracionEtapaAprobacion.objects.create(
            etapa=aprobacion, modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
        )
        errores = validar_estructura(version)
        self.assertTrue(any("no tiene ningún participante configurado" in e for e in errores))

    def test_aprobacion_sin_las_tres_transiciones_no_disponible(self):
        workflow = Workflow.objects.create(nombre="Aprobación incompleta")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        # Solo APROBADA — faltan RECHAZADA y DEVUELTA.
        TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion="APROBADA")
        errores = validar_estructura(version)
        self.assertTrue(any("resultado_aprobacion=RECHAZADA" in e for e in errores))
        self.assertTrue(any("resultado_aprobacion=DEVUELTA" in e for e in errores))

    def test_aprobacion_con_transicion_duplicada_no_disponible(self):
        workflow = Workflow.objects.create(nombre="Aprobación con duplicado")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        fin1 = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin 1")
        fin2 = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin 2")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin1, resultado_aprobacion="APROBADA")
        TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin2, resultado_aprobacion="APROBADA")
        TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin1, resultado_aprobacion="RECHAZADA")
        TransicionEtapa.objects.create(etapa_origen=aprobacion, etapa_destino=fin1, resultado_aprobacion="DEVUELTA")
        errores = validar_estructura(version)
        self.assertTrue(any("resultado_aprobacion=APROBADA" in e and "tiene 2" in e for e in errores))

    def test_aprobacion_no_admite_campos_de_condicion(self):
        """Rama `else` de `validar_integridad_transicion` (3.4): una
        transición que sale de APROBACION nunca acepta
        variable/operador/valor/es_fallback — esos son exclusivos de
        CONDICION."""
        workflow = Workflow.objects.create(nombre="Aprobación con campos de condición")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(
                etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion="APROBADA", variable="x"
            )

    def test_condicion_no_admite_resultado_aprobacion(self):
        """Simétrico: CONDICION permanece completamente independiente,
        sin regresión — `resultado_aprobacion` es exclusivo de
        APROBACION."""
        workflow = Workflow.objects.create(nombre="Condición con resultado_aprobacion")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        condicion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.CONDICION, nombre="Cond")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=condicion)
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(
                etapa_origen=condicion,
                etapa_destino=fin,
                resultado_aprobacion="APROBADA",
                es_fallback=True,
            )

    def test_gaceta_no_disponible_para_activar(self):
        workflow = Workflow.objects.create(nombre="Con gaceta")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        gaceta = Etapa.objects.create(version=version, tipo=Etapa.Tipo.GACETA, nombre="Gaceta")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=gaceta)
        TransicionEtapa.objects.create(etapa_origen=gaceta, etapa_destino=fin)
        errores = validar_estructura(version)
        self.assertTrue(any("GACETA" in e and "no está disponible" in e for e in errores))

    def test_tarea_disponible_para_activar(self):
        """3.3 (W.19, corrección aprobada): TAREA deja de ser un tipo
        bloqueado — `EstrategiaTarea.ejecutable=True` (a diferencia de
        3.2, donde este mismo test comprobaba lo contrario). Sigue sin
        aceptar ninguna clave en `Etapa.configuracion` (config real vive en
        `ConfiguracionEtapaTarea`, no en JSON)."""
        workflow = Workflow.objects.create(nombre="Con tarea")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        tarea = Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        errores = validar_estructura(version)
        self.assertEqual(errores, [])
        actor = Usuario.objects.create_user("x3", password=CLAVE_PRUEBA)
        activar_version(workflow, version, actor=actor)
        version.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.ACTIVA)

    def test_ticket_no_disponible_para_activar(self):
        workflow = Workflow.objects.create(nombre="Con ticket")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        ticket = Etapa.objects.create(version=version, tipo=Etapa.Tipo.TICKET, nombre="Ticket")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=ticket)
        TransicionEtapa.objects.create(etapa_origen=ticket, etapa_destino=fin)
        errores = validar_estructura(version)
        self.assertTrue(any("TICKET" in e and "no está disponible" in e for e in errores))
        actor = Usuario.objects.create_user("x4", password=CLAVE_PRUEBA)
        with self.assertRaises(ValidationError):
            activar_version(workflow, version, actor=actor)

    def test_etapa_ordinaria_con_dos_salidas_es_invalida(self):
        """W.4 (3.2, corrección aprobada): solo CONDICION puede tener más
        de una transición saliente. Un HITO con dos salidas (sin pasar por
        una CONDICION) es estructuralmente inválido."""
        workflow = Workflow.objects.create(nombre="Hito con dos salidas")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        hito = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Hito")
        fin1 = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin 1")
        fin2 = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin 2")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=hito)
        TransicionEtapa.objects.create(etapa_origen=hito, etapa_destino=fin1)
        TransicionEtapa.objects.create(etapa_origen=hito, etapa_destino=fin2)
        errores = validar_estructura(version)
        self.assertTrue(
            any("Hito" in e and "exactamente una transición saliente" in e and "tiene 2" in e for e in errores)
        )

    def test_etapa_ordinaria_con_cero_salidas_es_invalida(self):
        workflow = Workflow.objects.create(nombre="Hito sin salida")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        hito = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Hito")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=hito)
        errores = validar_estructura(version)
        self.assertTrue(any("Hito" in e and "tiene 0" in e for e in errores))

    def test_configuracion_invalida_de_etapa_detectada(self):
        _, version, etapas = _crear_workflow_lineal()
        # Escribe directo a la BD, bypaseando Etapa.clean() (igual criterio
        # que el test de bulk_create de arriba), para poder ejercitar la
        # detección de `validar_estructura` de forma aislada.
        Etapa.objects.filter(pk=etapas["tarea"].pk).update(configuracion={"clave_no_admitida": 1})
        errores = validar_estructura(version)
        self.assertTrue(any("Tarea" in e for e in errores))


class CondicionYTransicionesTests(TestCase):
    def _crear_workflow_con_condicion(self):
        workflow = Workflow.objects.create(nombre="Con condición")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        condicion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.CONDICION, nombre="¿Monto alto?")
        # HITO, no TAREA (W.5, 3.2) — ver nota en `_crear_workflow_lineal`.
        tarea = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Revisión especial")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=condicion)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        return workflow, version, {
            "inicio": inicio, "condicion": condicion, "tarea": tarea, "fin": fin,
        }

    def test_condicion_valida_con_fallback_y_una_condicional_activa(self):
        workflow, version, etapas = self._crear_workflow_con_condicion()
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"],
            etapa_destino=etapas["tarea"],
            prioridad=1,
            variable="monto",
            operador=TransicionEtapa.Operador.MAYOR_QUE,
            valor="1000000",
        )
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"], etapa_destino=etapas["fin"], es_fallback=True
        )
        errores = validar_estructura(version)
        self.assertEqual(errores, [])
        actor = Usuario.objects.create_user("cond1", password=CLAVE_PRUEBA)
        activar_version(workflow, version, actor=actor)
        version.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.ACTIVA)

    def test_condicion_sin_fallback_es_invalida(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"],
            etapa_destino=etapas["tarea"],
            variable="monto",
            operador=TransicionEtapa.Operador.MAYOR_QUE,
            valor="1000000",
        )
        errores = validar_estructura(version)
        self.assertTrue(any("exactamente una transición de" in e for e in errores))

    def test_condicion_con_dos_fallbacks_es_invalida(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"], etapa_destino=etapas["tarea"], es_fallback=True
        )
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"], etapa_destino=etapas["fin"], es_fallback=True
        )
        errores = validar_estructura(version)
        self.assertTrue(any("exactamente una transición de" in e for e in errores))

    def test_condicion_solo_con_fallback_sin_ninguna_condicional_es_invalida(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"], etapa_destino=etapas["fin"], es_fallback=True
        )
        errores = validar_estructura(version)
        self.assertTrue(any("al menos una transición condicional" in e for e in errores))

    def test_fallback_no_debe_llevar_expresion_condicional(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(
                etapa_origen=etapas["condicion"],
                etapa_destino=etapas["fin"],
                es_fallback=True,
                variable="monto",
            )

    def test_transicion_condicional_incompleta_es_rechazada(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(
                etapa_origen=etapas["condicion"], etapa_destino=etapas["tarea"], variable="monto"
            )

    def test_campos_condicionales_vacios_para_etapa_no_condicion(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(
                etapa_origen=etapas["tarea"],
                etapa_destino=etapas["fin"],
                variable="monto",
                operador=TransicionEtapa.Operador.IGUAL_A,
                valor="1",
            )

    def test_es_fallback_no_aplica_fuera_de_condicion(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(
                etapa_origen=etapas["tarea"], etapa_destino=etapas["fin"], es_fallback=True
            )

    def test_prioridad_ordena_las_transiciones_condicionales(self):
        _, version, etapas = self._crear_workflow_con_condicion()
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"],
            etapa_destino=etapas["fin"],
            es_fallback=True,
        )
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"],
            etapa_destino=etapas["tarea"],
            prioridad=2,
            variable="monto",
            operador=TransicionEtapa.Operador.MAYOR_QUE,
            valor="1000000",
        )
        TransicionEtapa.objects.create(
            etapa_origen=etapas["condicion"],
            etapa_destino=etapas["tarea"],
            prioridad=1,
            variable="prioridad",
            operador=TransicionEtapa.Operador.IGUAL_A,
            valor="ALTA",
        )
        prioridades = list(
            etapas["condicion"].transiciones_salientes.filter(es_fallback=False).values_list(
                "prioridad", flat=True
            )
        )
        self.assertEqual(prioridades, sorted(prioridades))

    def test_transicion_entre_versiones_distintas_rechazada(self):
        workflow, version1, etapas1 = _crear_workflow_lineal("Workflow con dos versiones")
        version2 = WorkflowVersion.objects.create(workflow=workflow, numero=2)
        etapa_v2 = Etapa.objects.create(version=version2, tipo=Etapa.Tipo.TAREA, nombre="En v2")
        with self.assertRaises(ValidationError):
            TransicionEtapa.objects.create(etapa_origen=etapas1["tarea"], etapa_destino=etapa_v2)


def _crear_workflow_con_ciclo(nombre="Con ciclo"):
    """INICIO → HITO → CONDICION → (repetir HITO) / (fallback → FIN).

    Bajo la regla de cardinalidad de W.4 (3.2) solo CONDICION puede tener
    más de una transición saliente, así que cualquier punto de retorno de
    un ciclo debe pasar por una CONDICION — reutilizada tanto por la
    prueba estructural de 3.1 (`CicloYAlcanzabilidadTests`) como por las
    pruebas de ejecución de ciclos del motor (3.2)."""
    workflow = Workflow.objects.create(nombre=nombre)
    version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
    hito = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Hito")
    condicion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.CONDICION, nombre="¿Repetir?")
    fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")

    TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=hito)
    TransicionEtapa.objects.create(etapa_origen=hito, etapa_destino=condicion)
    repetir = TransicionEtapa.objects.create(
        etapa_origen=condicion,
        etapa_destino=hito,
        prioridad=1,
        variable="repetir",
        operador=TransicionEtapa.Operador.IGUAL_A,
        valor="si",
    )
    salir = TransicionEtapa.objects.create(etapa_origen=condicion, etapa_destino=fin, es_fallback=True)
    return workflow, version, {
        "inicio": inicio, "hito": hito, "condicion": condicion, "fin": fin,
        "repetir": repetir, "salir": salir,
    }


# --- Helpers de 3.2 (motor de ejecución) -----------------------------------


def _crear_workflow_lineal_activo(actor):
    """INICIO → HITO → FIN, ya ACTIVA — base de la mayoría de las pruebas
    del motor que no necesitan bifurcación ni espera."""
    workflow, version, etapas = _crear_workflow_lineal()
    activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    return workflow, etapas


def _crear_workflow_activo_con_condicion(actor):
    """INICIO → CONDICION → (condicional: monto > 1000000 → HITO "Especial"
    / fallback → HITO "Normal") → FIN, ya ACTIVA."""
    workflow = Workflow.objects.create(nombre="Motor con condición")
    version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
    condicion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.CONDICION, nombre="¿Monto alto?")
    especial = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Especial")
    normal = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Normal")
    fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
    TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=condicion)
    condicional = TransicionEtapa.objects.create(
        etapa_origen=condicion,
        etapa_destino=especial,
        prioridad=1,
        variable="monto",
        operador=TransicionEtapa.Operador.MAYOR_QUE,
        valor="1000000",
    )
    fallback = TransicionEtapa.objects.create(etapa_origen=condicion, etapa_destino=normal, es_fallback=True)
    TransicionEtapa.objects.create(etapa_origen=especial, etapa_destino=fin)
    TransicionEtapa.objects.create(etapa_origen=normal, etapa_destino=fin)
    activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    return workflow, {
        "inicio": inicio, "condicion": condicion, "especial": especial, "normal": normal,
        "fin": fin, "condicional": condicional, "fallback": fallback,
    }


def _crear_workflow_activo_con_espera(actor, configuracion):
    """INICIO → ESPERA → FIN, ya ACTIVA."""
    workflow = Workflow.objects.create(nombre="Motor con espera")
    version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
    espera = Etapa.objects.create(
        version=version, tipo=Etapa.Tipo.ESPERA, nombre="Esperar", configuracion=configuracion
    )
    fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
    TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=espera)
    TransicionEtapa.objects.create(etapa_origen=espera, etapa_destino=fin)
    activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    return workflow, {"inicio": inicio, "espera": espera, "fin": fin}


def _crear_workflow_activo_con_tarea(actor, *, usuario_responsable=None, equipo_responsable=None, permite_subtareas=False):
    """INICIO → TAREA → FIN, ya ACTIVA — 3.3."""
    workflow = Workflow.objects.create(nombre="Motor con tarea")
    version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
    tarea_etapa = Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Revisar")
    fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
    TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea_etapa)
    TransicionEtapa.objects.create(etapa_origen=tarea_etapa, etapa_destino=fin)
    if usuario_responsable is not None:
        ConfiguracionEtapaTarea.objects.create(
            etapa=tarea_etapa,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO,
            usuario_responsable=usuario_responsable,
            permite_subtareas=permite_subtareas,
        )
    elif equipo_responsable is not None:
        ConfiguracionEtapaTarea.objects.create(
            etapa=tarea_etapa,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.EQUIPO,
            equipo_responsable=equipo_responsable,
            permite_subtareas=permite_subtareas,
        )
    activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    return workflow, {"inicio": inicio, "tarea": tarea_etapa, "fin": fin}


def _crear_workflow_activo_con_aprobacion(actor, *, modo, politica=None, participantes, destinos=None):
    """INICIO → APROBACION → (según resultado_aprobacion) → FIN*, ya
    ACTIVA — 3.4. `participantes`: lista de `(tipo_aprobador,
    usuario_o_equipo)` en el mismo formato que
    `apps.aprobaciones.operaciones.crear_esquema_aprobacion`. `destinos`,
    si se pasa, es un dict `{"APROBADA": etapa, "RECHAZADA": etapa,
    "DEVUELTA": etapa}` con etapas ya creadas en la misma versión (para
    construir flujos más elaborados, p.ej. el de Definition of Done); si
    se omite, las 3 ramas van cada una a su propio FIN."""
    workflow = Workflow.objects.create(nombre="Motor con aprobación")
    version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
    inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
    aprobacion_etapa = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
    TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion_etapa)

    configuracion = ConfiguracionEtapaAprobacion.objects.create(
        etapa=aprobacion_etapa, modo=modo, politica=politica or ""
    )
    for orden, (tipo_aprobador, aprobador) in enumerate(participantes, start=1):
        if tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.USUARIO:
            ParticipanteEtapaAprobacion.objects.create(
                configuracion=configuracion, orden=orden, tipo_aprobador=tipo_aprobador, usuario=aprobador
            )
        else:
            ParticipanteEtapaAprobacion.objects.create(
                configuracion=configuracion, orden=orden, tipo_aprobador=tipo_aprobador, equipo=aprobador
            )

    etapas = {"inicio": inicio, "aprobacion": aprobacion_etapa}
    if destinos is None:
        destinos = {}
        for resultado in ("APROBADA", "RECHAZADA", "DEVUELTA"):
            fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre=f"Fin {resultado}")
            destinos[resultado] = fin
            etapas[f"fin_{resultado.lower()}"] = fin
    for resultado, destino in destinos.items():
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion_etapa, etapa_destino=destino, resultado_aprobacion=resultado
        )

    activar_version(workflow, version, actor=actor)
    workflow.refresh_from_db()
    return workflow, etapas


class CicloYAlcanzabilidadTests(TestCase):
    def test_ciclos_permitidos_estructuralmente(self):
        """RN nada prohíbe los ciclos (RQF-079, "devolver para ajustes",
        anticipa que Sprint 4 los necesitará) — mientras exista igualmente
        un camino que alcance un FIN, la versión sigue siendo activable."""
        workflow, version, _ = _crear_workflow_con_ciclo()
        errores = validar_estructura(version)
        self.assertEqual(errores, [])
        actor = Usuario.objects.create_user("ciclo1", password=CLAVE_PRUEBA)
        activar_version(workflow, version, actor=actor)
        version.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.ACTIVA)


class ClonacionTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="clon1", password=CLAVE_PRUEBA)

    def test_clonacion_preserva_topologia(self):
        workflow, version1, etapas = _crear_workflow_lineal()
        TransicionEtapa.objects.filter(
            etapa_origen=etapas["tarea"], etapa_destino=etapas["fin"]
        ).update(nombre="Único camino")
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()

        version2 = crear_nueva_version(workflow, actor=self.actor)

        self.assertEqual(version2.etapas.count(), version1.etapas.count())
        transiciones_v2 = TransicionEtapa.objects.filter(etapa_origen__version=version2)
        self.assertEqual(transiciones_v2.count(), 2)
        for transicion in transiciones_v2:
            self.assertEqual(transicion.etapa_origen.version_id, version2.pk)
            self.assertEqual(transicion.etapa_destino.version_id, version2.pk)

        tipos_v1 = sorted(version1.etapas.values_list("tipo", "nombre"))
        tipos_v2 = sorted(version2.etapas.values_list("tipo", "nombre"))
        self.assertEqual(tipos_v1, tipos_v2)

    def test_clonacion_de_workflow_con_condicion_preserva_prioridad_y_fallback(self):
        workflow = Workflow.objects.create(nombre="Con condición a clonar")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        condicion = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.CONDICION, nombre="Cond")
        tarea = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=condicion)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        TransicionEtapa.objects.create(
            etapa_origen=condicion,
            etapa_destino=tarea,
            prioridad=1,
            variable="monto",
            operador=TransicionEtapa.Operador.MAYOR_QUE,
            valor="1000",
        )
        TransicionEtapa.objects.create(etapa_origen=condicion, etapa_destino=fin, es_fallback=True)

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        condicion_v2 = version2.etapas.get(tipo=Etapa.Tipo.CONDICION)
        salientes_v2 = list(condicion_v2.transiciones_salientes.all())
        self.assertEqual(len(salientes_v2), 2)
        fallback_v2 = [t for t in salientes_v2 if t.es_fallback]
        condicionales_v2 = [t for t in salientes_v2 if not t.es_fallback]
        self.assertEqual(len(fallback_v2), 1)
        self.assertEqual(len(condicionales_v2), 1)
        self.assertEqual(condicionales_v2[0].variable, "monto")
        self.assertEqual(condicionales_v2[0].valor, "1000")

    def test_clonar_desde_version_historica_para_recuperar_contenido(self):
        workflow, version1, _ = _crear_workflow_lineal()
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()
        version2 = crear_nueva_version(workflow, actor=self.actor)
        activar_version(workflow, version2, actor=self.actor)
        version1.refresh_from_db()
        self.assertEqual(version1.estado, WorkflowVersion.Estado.HISTORICA)

        version3 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        self.assertEqual(version3.estado, WorkflowVersion.Estado.BORRADOR)
        self.assertEqual(version3.etapas.count(), version1.etapas.count())

    # -- FASE 3.C, cierre correctivo aprobado: clonación de configuración
    # relacional (TAREA/APROBACION) que `crear_nueva_version` no clonaba. --

    def test_clonacion_de_configuracion_tarea_conserva_responsable_y_subtareas(self):
        workflow = Workflow.objects.create(nombre="Con tarea configurada")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        tarea = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        equipo = Equipo.objects.create(nombre="Equipo responsable clon")
        ConfiguracionEtapaTarea.objects.create(
            etapa=tarea,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.EQUIPO,
            equipo_responsable=equipo,
            permite_subtareas=True,
        )

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        tarea_v2 = version2.etapas.get(tipo=Etapa.Tipo.TAREA)
        config_v2 = tarea_v2.configuracion_tarea

        self.assertEqual(config_v2.etapa_id, tarea_v2.pk)
        self.assertNotEqual(config_v2.etapa_id, tarea.pk)
        self.assertEqual(config_v2.tipo_responsable, ConfiguracionEtapaTarea.TipoResponsable.EQUIPO)
        self.assertEqual(config_v2.equipo_responsable_id, equipo.pk)
        self.assertIsNone(config_v2.usuario_responsable_id)
        self.assertTrue(config_v2.permite_subtareas)

    def test_clonacion_de_tarea_sin_configuracion_no_falla_ni_crea_configuracion_artificial(self):
        workflow = Workflow.objects.create(nombre="Con tarea sin configurar")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        tarea = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        self.assertFalse(hasattr(tarea, "configuracion_tarea"))

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        tarea_v2 = version2.etapas.get(tipo=Etapa.Tipo.TAREA)
        self.assertFalse(ConfiguracionEtapaTarea.objects.filter(etapa=tarea_v2).exists())

    def test_clonacion_de_configuracion_aprobacion_conserva_modo_politica_y_participantes(self):
        workflow = Workflow.objects.create(nombre="Con aprobación configurada")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.APROBACION, nombre="Aprobación")
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion="APROBADA"
        )
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion="RECHAZADA"
        )
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion="DEVUELTA"
        )
        config = ConfiguracionEtapaAprobacion.objects.create(
            etapa=aprobacion,
            modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.TODOS,
        )
        equipo = Equipo.objects.create(nombre="Equipo aprobador clon")
        ParticipanteEtapaAprobacion.objects.create(
            configuracion=config,
            orden=1,
            tipo_aprobador=ParticipanteEtapaAprobacion.TipoAprobador.USUARIO,
            usuario=self.actor,
        )
        ParticipanteEtapaAprobacion.objects.create(
            configuracion=config,
            orden=2,
            tipo_aprobador=ParticipanteEtapaAprobacion.TipoAprobador.EQUIPO,
            equipo=equipo,
        )

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        aprobacion_v2 = version2.etapas.get(tipo=Etapa.Tipo.APROBACION)
        config_v2 = aprobacion_v2.configuracion_aprobacion

        self.assertEqual(config_v2.etapa_id, aprobacion_v2.pk)
        self.assertNotEqual(config_v2.pk, config.pk)
        self.assertEqual(config_v2.modo, ConfiguracionEtapaAprobacion.Modo.PARALELA)
        self.assertEqual(config_v2.politica, ConfiguracionEtapaAprobacion.Politica.TODOS)

        participantes_v2 = list(config_v2.participantes.order_by("orden"))
        self.assertEqual(len(participantes_v2), 2)
        self.assertTrue(all(p.configuracion_id == config_v2.pk for p in participantes_v2))
        self.assertEqual(participantes_v2[0].orden, 1)
        self.assertEqual(participantes_v2[0].tipo_aprobador, ParticipanteEtapaAprobacion.TipoAprobador.USUARIO)
        self.assertEqual(participantes_v2[0].usuario_id, self.actor.pk)
        self.assertEqual(participantes_v2[1].orden, 2)
        self.assertEqual(participantes_v2[1].tipo_aprobador, ParticipanteEtapaAprobacion.TipoAprobador.EQUIPO)
        self.assertEqual(participantes_v2[1].equipo_id, equipo.pk)

        # Regresión (F): resultado_aprobacion se conserva en las 3 transiciones clonadas.
        resultados_v2 = sorted(
            aprobacion_v2.transiciones_salientes.values_list("resultado_aprobacion", flat=True)
        )
        self.assertEqual(resultados_v2, ["APROBADA", "DEVUELTA", "RECHAZADA"])

    def test_independencia_entre_versiones_aprobacion(self):
        workflow = Workflow.objects.create(nombre="Independencia aprobación")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        aprobacion = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.APROBACION, nombre="Aprobación")
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=aprobacion)
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=fin, resultado_aprobacion="APROBADA"
        )
        config_v1 = ConfiguracionEtapaAprobacion.objects.create(
            etapa=aprobacion,
            modo=ConfiguracionEtapaAprobacion.Modo.SECUENCIAL,
        )
        participante_v1 = ParticipanteEtapaAprobacion.objects.create(
            configuracion=config_v1,
            orden=1,
            tipo_aprobador=ParticipanteEtapaAprobacion.TipoAprobador.USUARIO,
            usuario=self.actor,
        )

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        aprobacion_v2 = version2.etapas.get(tipo=Etapa.Tipo.APROBACION)
        config_v2 = aprobacion_v2.configuracion_aprobacion
        participante_v2 = config_v2.participantes.get()

        # 3. Se modifica la configuración/participante de V2...
        config_v2.modo = ConfiguracionEtapaAprobacion.Modo.PARALELA
        config_v2.politica = ConfiguracionEtapaAprobacion.Politica.CUALQUIERA
        config_v2.save()
        otro_usuario = Usuario.objects.create_user(username="clon2", password=CLAVE_PRUEBA)
        participante_v2.usuario = otro_usuario
        participante_v2.orden = 99
        participante_v2.save()

        # 4. ...y V1 conserva exactamente su configuración original.
        config_v1.refresh_from_db()
        participante_v1.refresh_from_db()
        self.assertEqual(config_v1.modo, ConfiguracionEtapaAprobacion.Modo.SECUENCIAL)
        self.assertEqual(config_v1.politica, "")
        self.assertEqual(participante_v1.orden, 1)
        self.assertEqual(participante_v1.usuario_id, self.actor.pk)

    def test_independencia_entre_versiones_tarea(self):
        workflow = Workflow.objects.create(nombre="Independencia tarea")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        tarea = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=tarea)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=fin)
        config_v1 = ConfiguracionEtapaTarea.objects.create(
            etapa=tarea,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO,
            usuario_responsable=self.actor,
            permite_subtareas=False,
        )

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        tarea_v2 = version2.etapas.get(tipo=Etapa.Tipo.TAREA)
        config_v2 = tarea_v2.configuracion_tarea

        config_v2.permite_subtareas = True
        config_v2.save()

        config_v1.refresh_from_db()
        self.assertFalse(config_v1.permite_subtareas)

    def test_clonacion_no_copia_informacion_de_ejecucion(self):
        """(E) `crear_nueva_version` nunca crea/copia filas de EJECUCIÓN —
        `InstanciaWorkflow`/`InstanciaEtapa`/`TareaWorkflow`/
        `EsquemaAprobacionWorkflow`/`Aprobacion` viven exclusivamente atadas
        a la versión que efectivamente se ejecutó, nunca a un clon."""
        workflow, version1, _ = _crear_workflow_lineal()
        activar_version(workflow, version1, actor=self.actor)
        workflow.refresh_from_db()
        instancia = iniciar_workflow(workflow, actor=self.actor)
        self.assertTrue(InstanciaWorkflow.objects.filter(pk=instancia.pk).exists())
        self.assertTrue(InstanciaEtapa.objects.filter(instancia_workflow=instancia).exists())

        conteos_previos = {
            "InstanciaWorkflow": InstanciaWorkflow.objects.count(),
            "InstanciaEtapa": InstanciaEtapa.objects.count(),
            "TareaWorkflow": TareaWorkflow.objects.count(),
            "EsquemaAprobacionWorkflow": EsquemaAprobacionWorkflow.objects.count(),
            "Aprobacion": Aprobacion.objects.count(),
        }

        crear_nueva_version(workflow, actor=self.actor)

        self.assertEqual(InstanciaWorkflow.objects.count(), conteos_previos["InstanciaWorkflow"])
        self.assertEqual(InstanciaEtapa.objects.count(), conteos_previos["InstanciaEtapa"])
        self.assertEqual(TareaWorkflow.objects.count(), conteos_previos["TareaWorkflow"])
        self.assertEqual(
            EsquemaAprobacionWorkflow.objects.count(), conteos_previos["EsquemaAprobacionWorkflow"]
        )
        self.assertEqual(Aprobacion.objects.count(), conteos_previos["Aprobacion"])

    def test_clonacion_regresion_configuracion_json_y_condicion_siguen_intactas(self):
        """(F) Regresión: `Etapa.configuracion` JSON (ESPERA) y la
        integridad de CONDICION (prioridad/fallback, ya cubierta arriba)
        siguen funcionando exactamente igual tras agregar los clonadores de
        TAREA/APROBACION — ningún tipo sin clonador propio pierde nada de
        lo que ya se clonaba antes de FASE 3.C."""
        workflow = Workflow.objects.create(nombre="Con espera a clonar")
        version1 = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        espera = Etapa.objects.create(
            version=version1,
            tipo=Etapa.Tipo.ESPERA,
            nombre="Espera",
            configuracion={"modo": "DURACION", "duracion_valor": 3, "duracion_unidad": "DIAS"},
        )
        fin = Etapa.objects.create(version=version1, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=espera)
        TransicionEtapa.objects.create(etapa_origen=espera, etapa_destino=fin)

        version2 = crear_nueva_version(workflow, actor=self.actor, clonar_desde=version1)
        espera_v2 = version2.etapas.get(tipo=Etapa.Tipo.ESPERA)
        self.assertEqual(
            espera_v2.configuracion, {"modo": "DURACION", "duracion_valor": 3, "duracion_unidad": "DIAS"}
        )
        self.assertEqual(espera_v2.transiciones_salientes.count(), 1)


class AuditoriaTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="auditor31", password=CLAVE_PRUEBA)

    def test_crear_version_queda_auditada(self):
        workflow = Workflow.objects.create(nombre="Auditado")
        version = crear_nueva_version(workflow, actor=self.actor)
        auditorias = _auditorias_de(version)
        self.assertEqual(auditorias.count(), 1)
        self.assertEqual(auditorias.first().accion, RegistroAuditoria.Accion.CREAR)

    def test_activacion_de_version_queda_auditada_sobre_el_workflow(self):
        workflow, version, _ = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)

        auditorias = _auditorias_de(workflow).filter(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, datos_nuevos={"version_activa_id": version.pk}
        )
        self.assertEqual(auditorias.count(), 1)
        registro = auditorias.first()
        self.assertEqual(registro.usuario_id, self.actor.pk)
        self.assertEqual(registro.datos_anteriores, {"version_activa_id": None})

    def test_no_existe_una_tercera_tabla_de_historial(self):
        """No existe un historial propio de Workflow (corrección aprobada)
        — la auditoría vive exclusivamente en `RegistroAuditoria`. Aquí
        `version` se crea directo (`_crear_workflow_lineal`, no vía
        `crear_nueva_version`), así que no genera auditoría de creación;
        solo la activación, sobre `Workflow`, queda registrada — ninguna
        auditoría "fantasma" aparece en ningún otro lugar."""
        workflow, version, _ = _crear_workflow_lineal()
        activar_version(workflow, version, actor=self.actor)
        self.assertEqual(_auditorias_de(version).count(), 0)
        self.assertEqual(_auditorias_de(workflow).count(), 1)


class AuditoriaInlinesWorkflowTests(TestCase):
    """Formsets reales de los inlines y hooks de sus ModelAdmin propietarios."""

    def setUp(self):
        from django.contrib import admin
        from django.test import RequestFactory

        self.site = admin.site
        self.actor = Usuario.objects.create_superuser("admin_inlines", "admin@example.com", CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.actor)
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.actor
        workflow = Workflow.objects.create(nombre="Auditoría inlines")
        self.version = WorkflowVersion.objects.create(workflow=workflow, numero=1)

    def _ciclo_inline(self, padre, modelo, valores, cambio):
        propietario = self.site._registry[type(padre)]
        inline_cls = next(clase for clase in propietario.inlines if clase.model is modelo)
        inline = inline_cls(type(padre), self.site)
        formset_cls = inline.get_formset(self.request, padre)
        prefijo = formset_cls.get_default_prefix()
        pk = None
        for accion in ("CREAR", "ACTUALIZAR", "ELIMINAR"):
            with self.subTest(modelo=modelo.__name__, accion=accion):
                campos = dict(valores)
                if accion != "CREAR":
                    campos.update(cambio)
                datos = {
                    f"{prefijo}-TOTAL_FORMS": "1",
                    f"{prefijo}-INITIAL_FORMS": "0" if pk is None else "1",
                    f"{prefijo}-0-id": "" if pk is None else str(pk),
                }
                datos.update({f"{prefijo}-0-{nombre}": valor for nombre, valor in campos.items()})
                if accion == "ELIMINAR":
                    datos[f"{prefijo}-0-DELETE"] = "on"
                formset = formset_cls(data=datos, instance=padre, prefix=prefijo)
                self.assertTrue(formset.is_valid(), (formset.errors, formset.non_form_errors()))
                eventos = list(RegistroAuditoria.objects.values_list("pk", flat=True))
                # El changeform del Admin ya proporciona la frontera atómica.
                with transaction.atomic():
                    propietario.save_formset(self.request, None, formset, change=True)
                nuevos = RegistroAuditoria.objects.exclude(pk__in=eventos)
                self.assertEqual(nuevos.count(), 1)
                evento = nuevos.get()
                self.assertEqual(evento.accion, accion)
                self.assertEqual(evento.usuario, self.actor)
                self.assertEqual(evento.origen, "USUARIO")
                self.assertEqual(evento.content_type, ContentType.objects.get_for_model(modelo))
                if accion == "CREAR":
                    pk = formset.new_objects[0].pk
                    self.assertIsNone(evento.datos_anteriores)
                    self.assertIsNotNone(evento.datos_nuevos)
                elif accion == "ACTUALIZAR":
                    for campo in cambio:
                        self.assertNotEqual(evento.datos_anteriores[campo], evento.datos_nuevos[campo])
                    self.assertTrue(modelo.objects.filter(pk=pk).exists())
                else:
                    self.assertIsNotNone(evento.datos_anteriores)
                    self.assertIsNone(evento.datos_nuevos)
                    self.assertFalse(modelo.objects.filter(pk=pk).exists())
                self.assertEqual(evento.object_id, pk)

    def test_etapa_en_workflow_version(self):
        self._ciclo_inline(
            self.version, Etapa,
            {"tipo": "HITO", "nombre": "Original", "descripcion": "", "configuracion": "{}"},
            {"nombre": "Editada"},
        )

    def test_configuracion_tarea_en_etapa(self):
        etapa = Etapa.objects.create(version=self.version, tipo="TAREA", nombre="Tarea")
        self._ciclo_inline(
            etapa, ConfiguracionEtapaTarea,
            {"tipo_responsable": "USUARIO", "usuario_responsable": self.actor.pk,
             "equipo_responsable": "", "permite_subtareas": ""},
            {"permite_subtareas": "on"},
        )

    def test_configuracion_aprobacion_en_etapa(self):
        etapa = Etapa.objects.create(version=self.version, tipo="APROBACION", nombre="Aprobar")
        self._ciclo_inline(
            etapa, ConfiguracionEtapaAprobacion,
            {"modo": "PARALELA", "politica": "TODOS"}, {"politica": "CUALQUIERA"},
        )

    def test_participante_en_configuracion_aprobacion(self):
        etapa = Etapa.objects.create(version=self.version, tipo="APROBACION", nombre="Aprobar")
        configuracion = ConfiguracionEtapaAprobacion.objects.create(etapa=etapa, modo="SECUENCIAL")
        self._ciclo_inline(
            configuracion, ParticipanteEtapaAprobacion,
            {"orden": "1", "tipo_aprobador": "USUARIO", "usuario": self.actor.pk, "equipo": ""},
            {"orden": "2"},
        )


class AutorizacionTests(TestCase):
    def test_sin_permiso_no_puede_administrar_ni_consultar(self):
        usuario = Usuario.objects.create_user(username="sinpermiso31", password=CLAVE_PRUEBA)
        self.assertFalse(puede_administrar_workflows(usuario))
        self.assertFalse(puede_consultar_workflows(usuario))

    def test_con_permiso_administrar_tambien_puede_consultar(self):
        usuario = Usuario.objects.create_user(username="admin31", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(usuario)
        self.assertTrue(puede_administrar_workflows(usuario))
        self.assertTrue(puede_consultar_workflows(usuario))

    def test_con_permiso_solo_consultar(self):
        usuario = Usuario.objects.create_user(username="lector31", password=CLAVE_PRUEBA)
        permiso, _ = Permiso.objects.get_or_create(
            codigo="workflows.consultar", defaults={"nombre": "Consultar workflows"}
        )
        rol = RolFuncional.objects.create(nombre="Rol lector workflows")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(
            usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.assertFalse(puede_administrar_workflows(usuario))
        self.assertTrue(puede_consultar_workflows(usuario))


class EstrategiaEsperaTests(TestCase):
    """`ESPERA` es el único tipo con configuración propia en 3.1 (X.3) —
    duración relativa o fecha objetivo, sin espera por evento."""

    def _crear_espera(self, configuracion):
        workflow = Workflow.objects.create(nombre="Con espera")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        return Etapa.objects.create(
            version=version, tipo=Etapa.Tipo.ESPERA, nombre="Esperar", configuracion=configuracion
        )

    def test_espera_por_duracion_valida(self):
        etapa = self._crear_espera({"modo": "DURACION", "duracion_valor": 3, "duracion_unidad": "DIAS"})
        etapa.clean()  # no debe lanzar

    def test_espera_por_fecha_valida(self):
        etapa = self._crear_espera({"modo": "FECHA", "fecha_objetivo": "2026-12-31"})
        etapa.clean()

    def test_espera_sin_modo_es_invalida(self):
        with self.assertRaises(ValidationError):
            self._crear_espera({}).clean()

    def test_espera_duracion_no_admite_fecha_objetivo(self):
        with self.assertRaises(ValidationError):
            self._crear_espera(
                {"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "DIAS", "fecha_objetivo": "2026-01-01"}
            ).clean()

    def test_espera_fecha_formato_invalido(self):
        with self.assertRaises(ValidationError):
            self._crear_espera({"modo": "FECHA", "fecha_objetivo": "no-es-una-fecha"}).clean()

    def test_espera_clave_desconocida_rechazada(self):
        with self.assertRaises(ValidationError):
            self._crear_espera({"modo": "FECHA", "fecha_objetivo": "2026-01-01", "evento": "algo"}).clean()

    def test_tipos_sin_configuracion_rechazan_cualquier_clave(self):
        workflow = Workflow.objects.create(nombre="Config espuria")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        etapa = Etapa.objects.create(
            version=version, tipo=Etapa.Tipo.TAREA, nombre="Tarea", configuracion={"responsable": 1}
        )
        with self.assertRaises(ValidationError):
            etapa.clean()


# ============================================================================
# 3.2 — Motor de ejecución (CU-020 "Ejecutar workflow", RQF-065/066/067/068,
# RN-020/021)
# ============================================================================


class MotorFlujoBasicoTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="motorflujo1", password=CLAVE_PRUEBA)

    def test_flujo_inicio_hito_fin_se_completa_automaticamente(self):
        workflow, etapas = _crear_workflow_lineal_activo(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertIsNotNone(instancia.finalizada_en)

        ejecuciones = list(instancia.ejecuciones_etapa.order_by("orden"))
        self.assertEqual(
            [e.etapa_id for e in ejecuciones],
            [etapas["inicio"].pk, etapas["tarea"].pk, etapas["fin"].pk],
        )
        self.assertEqual([e.orden for e in ejecuciones], [1, 2, 3])
        self.assertTrue(all(e.estado == InstanciaEtapa.Estado.COMPLETADA for e in ejecuciones))
        self.assertTrue(all(e.finalizada_en is not None for e in ejecuciones))

    def test_iniciar_workflow_sin_version_activa_falla(self):
        workflow = Workflow.objects.create(nombre="Sin versión activa")
        with self.assertRaises(ValueError):
            iniciar_workflow(workflow, actor=self.actor)

    def test_instancia_queda_atada_a_la_version_con_la_que_se_creo(self):
        """RN-020: publicar una nueva versión no cambia instancias
        existentes."""
        workflow, etapas = _crear_workflow_lineal_activo(self.actor)
        version1 = workflow.version_activa
        instancia = iniciar_workflow(workflow, actor=self.actor)

        version2 = crear_nueva_version(workflow, actor=self.actor)
        activar_version(workflow, version2, actor=self.actor)

        instancia.refresh_from_db()
        self.assertEqual(instancia.workflow_version_id, version1.pk)

    def test_iniciar_workflow_con_origen_sistema_no_exige_actor(self):
        workflow, _ = _crear_workflow_lineal_activo(self.actor)
        instancia = iniciar_workflow(workflow, origen=RegistroAuditoria.Origen.SISTEMA)
        self.assertIsNone(instancia.iniciado_por_id)
        auditorias = _auditorias_de(instancia)
        self.assertEqual(auditorias.first().origen, RegistroAuditoria.Origen.SISTEMA)


class MotorCondicionTests(TestCase):
    """RQF-066/RN-021: la CONDICION —no el motor— decide la transición."""

    def setUp(self):
        self.actor = Usuario.objects.create_user(username="motorcond1", password=CLAVE_PRUEBA)

    def test_condicion_toma_primera_coincidencia(self):
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"monto": 2000000})

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        etapa_ids = list(instancia.ejecuciones_etapa.values_list("etapa_id", flat=True))
        self.assertIn(etapas["especial"].pk, etapa_ids)
        self.assertNotIn(etapas["normal"].pk, etapa_ids)

        ejecucion_condicion = instancia.ejecuciones_etapa.get(etapa=etapas["condicion"])
        self.assertEqual(ejecucion_condicion.transicion_tomada_id, etapas["condicional"].pk)

    def test_condicion_toma_fallback_cuando_no_hay_coincidencia(self):
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"monto": 100})

        etapa_ids = list(instancia.ejecuciones_etapa.values_list("etapa_id", flat=True))
        self.assertIn(etapas["normal"].pk, etapa_ids)
        self.assertNotIn(etapas["especial"].pk, etapa_ids)

        ejecucion_condicion = instancia.ejecuciones_etapa.get(etapa=etapas["condicion"])
        self.assertEqual(ejecucion_condicion.transicion_tomada_id, etapas["fallback"].pk)

    def test_condicion_con_variable_inexistente_toma_fallback(self):
        """Sin `datos_iniciales`, `variables["monto"]` no existe —
        `MAYOR_QUE(None, 1000000)` no debe reventar, debe simplemente no
        coincidir (mismo criterio que `apps.catalogo.reglas`)."""
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor)

        ejecucion_condicion = instancia.ejecuciones_etapa.get(etapa=etapas["condicion"])
        self.assertEqual(ejecucion_condicion.transicion_tomada_id, etapas["fallback"].pk)

    def test_motor_delega_en_la_strategy_no_decide_por_tipo(self):
        """RN-021 / corrección aprobada: si se reemplaza la Strategy de
        CONDICION por un doble que siempre elige la rama condicional (sin
        tocar `motor.py`), el resultado cambia — la decisión vive en la
        Strategy, no en un `if etapa.tipo == CONDICION` del motor."""
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)

        class _EstrategiaCondicionSiempreEspecial(EstrategiaCondicion):
            def ejecutar(self, instancia_etapa, contexto):
                condicional = instancia_etapa.etapa.transiciones_salientes.get(es_fallback=False)
                return ResultadoEjecucionEtapa(
                    estado=ResultadoEjecucion.CONTINUAR, transicion_seleccionada=condicional
                )

        with patch.dict(
            "apps.workflows.estrategias.ESTRATEGIAS_POR_TIPO",
            {"CONDICION": _EstrategiaCondicionSiempreEspecial()},
        ):
            # Con datos que normalmente irían por el fallback (monto bajo).
            instancia = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"monto": 1})

        etapa_ids = list(instancia.ejecuciones_etapa.values_list("etapa_id", flat=True))
        self.assertIn(etapas["especial"].pk, etapa_ids)
        self.assertNotIn(etapas["normal"].pk, etapa_ids)


class MotorCicloTests(TestCase):
    """3.1 permite ciclos (`A → B → A`); una misma `Etapa` puede ejecutarse
    varias veces dentro de una instancia — `InstanciaEtapa` representa una
    EJECUCIÓN, no "la etapa"."""

    def setUp(self):
        self.actor = Usuario.objects.create_user(username="motorciclo1", password=CLAVE_PRUEBA)

    def _iniciar_con_ciclo_activo_hasta_el_limite(self, limite):
        workflow, version, etapas = _crear_workflow_con_ciclo()
        activar_version(workflow, version, actor=self.actor)
        workflow.refresh_from_db()
        with patch("apps.workflows.motor.LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION", limite):
            instancia = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"repetir": "si"})
        return instancia, etapas

    def test_ciclo_ejecuta_la_misma_etapa_varias_veces(self):
        instancia, etapas = self._iniciar_con_ciclo_activo_hasta_el_limite(5)
        ejecuciones_hito = instancia.ejecuciones_etapa.filter(etapa=etapas["hito"])
        self.assertGreater(ejecuciones_hito.count(), 1)

    def test_orden_es_secuencial_y_unico_por_instancia(self):
        instancia, etapas = self._iniciar_con_ciclo_activo_hasta_el_limite(5)
        ordenes = list(instancia.ejecuciones_etapa.order_by("orden").values_list("orden", flat=True))
        self.assertEqual(ordenes, list(range(1, len(ordenes) + 1)))

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InstanciaEtapa.objects.create(
                    instancia_workflow=instancia, etapa=etapas["hito"], orden=1
                )

    def test_contexto_con_ciclos_no_pierde_resultados_anteriores(self):
        instancia, etapas = self._iniciar_con_ciclo_activo_hasta_el_limite(5)
        historial = instancia.contexto["resultados_etapas"][str(etapas["hito"].pk)]
        self.assertEqual(len(historial), instancia.ejecuciones_etapa.filter(etapa=etapas["hito"]).count())

    def test_limite_tecnico_no_produce_error(self):
        instancia, _etapas = self._iniciar_con_ciclo_activo_hasta_el_limite(5)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_EJECUCION)
        self.assertNotEqual(instancia.estado, InstanciaWorkflow.Estado.ERROR)

    def test_continuar_despues_del_limite_tecnico_completa_el_workflow(self):
        instancia, _etapas = self._iniciar_con_ciclo_activo_hasta_el_limite(3)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_EJECUCION)

        # Simula que, entre invocaciones, algo hizo que la condición deje
        # de cumplirse (ver docstring de la clase: ninguna Strategy de 3.2
        # cambia "repetir" por sí sola dentro del bucle automático).
        instancia.refresh_from_db()
        instancia.contexto["variables"]["repetir"] = "no"
        instancia.save(update_fields=["contexto"])

        with patch("apps.workflows.motor.LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION", 3):
            instancia = avanzar_instancia(instancia)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_avanzar_instancia_que_no_esta_en_ejecucion_falla(self):
        instancia, _etapas = self._iniciar_con_ciclo_activo_hasta_el_limite(3)
        instancia.contexto["variables"]["repetir"] = "no"
        instancia.save(update_fields=["contexto"])
        with patch("apps.workflows.motor.LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION", 3):
            instancia = avanzar_instancia(instancia)  # ahora COMPLETADA
        with self.assertRaises(ValueError):
            avanzar_instancia(instancia)


class MotorEsperaTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="motoresp1", password=CLAVE_PRUEBA)

    def test_espera_deja_la_instancia_en_espera_con_reanudar_en(self):
        workflow, etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": 3, "duracion_unidad": "DIAS"}
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["espera"])
        self.assertEqual(ejecucion.estado, InstanciaEtapa.Estado.EN_ESPERA)
        self.assertIn("reanudar_en", ejecucion.resultado)
        self.assertIsNone(ejecucion.finalizada_en)

    def test_reanudar_antes_de_tiempo_falla(self):
        workflow, _etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": 3, "duracion_unidad": "DIAS"}
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)

        with self.assertRaises(ValueError):
            reanudar_instancia(instancia)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

    def test_reanudar_despues_de_vencido_avanza_y_completa(self):
        workflow, etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "DIAS"}
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["espera"])
        ejecucion.resultado["reanudar_en"] = (timezone.now() - timedelta(minutes=1)).isoformat()
        ejecucion.save(update_fields=["resultado"])

        instancia = reanudar_instancia(instancia)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_reanudar_instancia_que_no_esta_en_espera_falla(self):
        workflow, etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "DIAS"}
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["espera"])
        ejecucion.resultado["reanudar_en"] = (timezone.now() - timedelta(minutes=1)).isoformat()
        ejecucion.save(update_fields=["resultado"])
        instancia = reanudar_instancia(instancia)  # ya COMPLETADA

        with self.assertRaises(ValueError):
            reanudar_instancia(instancia)


class _EstrategiaFalloFuncional(EstrategiaSinConfiguracion):
    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        return ResultadoEjecucionEtapa(
            estado=ResultadoEjecucion.ERROR, mensaje_error="Condición de negocio no cumplida."
        )


class _EstrategiaFalloTecnico(EstrategiaSinConfiguracion):
    ejecutable = True

    def ejecutar(self, instancia_etapa, contexto):
        raise RuntimeError("fallo inesperado de prueba")


class MotorErroresTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="motorerr1", password=CLAVE_PRUEBA)

    def test_error_funcional_no_marca_finalizada_en(self):
        workflow, etapas = _crear_workflow_lineal_activo(self.actor)
        with patch.dict("apps.workflows.estrategias.ESTRATEGIAS_POR_TIPO", {"HITO": _EstrategiaFalloFuncional()}):
            instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.ERROR)
        self.assertIsNone(instancia.finalizada_en)

        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])
        self.assertEqual(ejecucion.estado, InstanciaEtapa.Estado.ERROR)
        self.assertEqual(ejecucion.error["tipo"], "FUNCIONAL")
        self.assertIsNone(ejecucion.finalizada_en)

    def test_error_tecnico_no_persiste_traceback(self):
        workflow, etapas = _crear_workflow_lineal_activo(self.actor)
        with patch.dict("apps.workflows.estrategias.ESTRATEGIAS_POR_TIPO", {"HITO": _EstrategiaFalloTecnico()}):
            instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.ERROR)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])
        self.assertEqual(ejecucion.error["tipo"], "TECNICO")
        self.assertIn("fallo inesperado de prueba", ejecucion.error["mensaje"])
        self.assertNotIn("Traceback", ejecucion.error["mensaje"])
        self.assertLess(len(ejecucion.error["mensaje"]), 500)

    def test_strategy_no_disponible_en_runtime_produce_error_controlado(self):
        """Defensa adicional (mismo criterio que `validacion.py` en 3.1):
        `validar_estructura` ya impide activar una versión con un tipo no
        ejecutable (W.5) — esto solo protege contra el caso de que, por
        error, una Strategy antes ejecutable deje de estarlo."""
        workflow, etapas = _crear_workflow_lineal_activo(self.actor)
        estrategia_hito = ESTRATEGIAS_POR_TIPO["HITO"]
        with patch.object(estrategia_hito, "ejecutable", False):
            instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.ERROR)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])
        self.assertEqual(ejecucion.error["tipo"], "TECNICO")
        self.assertIn("HITO", ejecucion.error["mensaje"])


class MotorAutorizacionTests(TestCase):
    def test_sin_permiso_no_puede_ejecutar(self):
        usuario = Usuario.objects.create_user(username="sinpermisoexec32", password=CLAVE_PRUEBA)
        self.assertFalse(puede_ejecutar_workflows(usuario))

    def test_permiso_ejecutar_es_independiente_de_consultar_y_administrar(self):
        usuario = Usuario.objects.create_user(username="soloejecutar32", password=CLAVE_PRUEBA)
        permiso, _ = Permiso.objects.get_or_create(
            codigo="workflows.ejecutar", defaults={"nombre": "Ejecutar workflows"}
        )
        rol = RolFuncional.objects.create(nombre="Rol ejecutor workflows")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(
            usuario=usuario, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL
        )
        self.assertTrue(puede_ejecutar_workflows(usuario))
        self.assertFalse(puede_administrar_workflows(usuario))

    def test_administrar_no_implica_ejecutar(self):
        """W.8, corrección aprobada: a diferencia de administrar→consultar
        (relación ya aprobada en 3.1), administrar y ejecutar son
        capacidades completamente independientes."""
        usuario = Usuario.objects.create_user(username="soloadmin32", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(usuario)
        self.assertTrue(puede_administrar_workflows(usuario))
        self.assertFalse(puede_ejecutar_workflows(usuario))


class MotorConcurrenciaSecuencialTests(TestCase):
    """Idempotencia sin necesidad de hilos reales: reintentar una operación
    sobre una instancia que ya cambió de estado debe fallar limpio, nunca
    reejecutar. La contención bajo lock con hilos reales concurrentes se
    prueba en `ConcurrenciaReanudarInstanciaTests` (`TransactionTestCase`,
    mismo patrón que `apps/tickets/tests.py`)."""

    def setUp(self):
        self.actor = Usuario.objects.create_user(username="motorconc1", password=CLAVE_PRUEBA)

    def test_doble_avance_tras_completarse_falla_limpio(self):
        workflow, _etapas = _crear_workflow_lineal_activo(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        with self.assertRaises(ValueError):
            avanzar_instancia(instancia)


class ConcurrenciaReanudarInstanciaTests(TransactionTestCase):
    """W.6/N (corrección aprobada): dos hilos reanudando la MISMA instancia
    EN_ESPERA a la vez — solo uno debe ganar (`select_for_update()` +
    revalidación de estado bajo lock), el otro debe recibir un error
    explícito en vez de reanudarla dos veces. `TransactionTestCase` + hilos
    reales contra Postgres (no `TestCase`, que envuelve cada test en una
    única transacción y no serializa hilos de verdad) — mismo patrón que
    `apps/tickets/tests.py::ConcurrenciaTomarTicketTests`."""

    def setUp(self):
        self.actor = Usuario.objects.create_user(username="concurrencia32", password=CLAVE_PRUEBA)
        workflow, etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "DIAS"}
        )
        self.instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = self.instancia.ejecuciones_etapa.get(etapa=etapas["espera"])
        ejecucion.resultado["reanudar_en"] = (timezone.now() - timedelta(minutes=1)).isoformat()
        ejecucion.save(update_fields=["resultado"])

    def test_dos_reanudaciones_concurrentes_solo_una_avanza(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _intentar_reanudar(clave):
            barrera.wait()
            try:
                instancia = InstanciaWorkflow.objects.get(pk=self.instancia.pk)
                reanudar_instancia(instancia)
                resultados[clave] = "ok"
            except ValueError:
                resultados[clave] = "ya_reanudada"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_intentar_reanudar, args=("a",))
        hilo_b = threading.Thread(target=_intentar_reanudar, args=("b",))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_reanudada"), 1)

        instancia = InstanciaWorkflow.objects.get(pk=self.instancia.pk)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)


class TareaReanudarEsperasVencidasTests(TestCase):
    """3.2.x — `apps.workflows.tasks.reanudar_esperas_vencidas`. Prueba la
    tarea y su consulta de candidatos, no el motor (eso ya lo cubren
    `MotorEsperaTests`/`ConcurrenciaReanudarInstanciaTests`): aquí el motor
    real solo se usa para llegar a estados EN_ESPERA/ERROR realistas."""

    def setUp(self):
        self.actor = Usuario.objects.create_user(username="celery32x", password=CLAVE_PRUEBA)

    def _crear_instancia_en_espera(self, *, dias_duracion=1, vencida=False):
        workflow, etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": dias_duracion, "duracion_unidad": "DIAS"}
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        if vencida:
            ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["espera"])
            ejecucion.resultado["reanudar_en"] = (timezone.now() - timedelta(minutes=1)).isoformat()
            ejecucion.save(update_fields=["resultado"])
        return instancia, etapas

    def test_espera_no_vencida_no_se_selecciona(self):
        instancia, _ = self._crear_instancia_en_espera(dias_duracion=3, vencida=False)
        self.assertNotIn(instancia.pk, list(_candidatos_esperas_vencidas()))

    def test_espera_vencida_si_se_selecciona(self):
        instancia, _ = self._crear_instancia_en_espera(vencida=True)
        self.assertIn(instancia.pk, list(_candidatos_esperas_vencidas()))

    def test_scheduler_delega_en_reanudar_instancia_no_duplica_logica(self):
        """La tarea no reimplementa cambio de estado/avance/transición: solo
        llama a `reanudar_instancia`. Si se la reemplaza por un doble que no
        hace nada, la instancia debe quedar exactamente como estaba."""
        instancia, _ = self._crear_instancia_en_espera(vencida=True)

        with patch("apps.workflows.tasks.reanudar_instancia") as mock_reanudar:
            procesadas = reanudar_esperas_vencidas()

        mock_reanudar.assert_called_once()
        (llamada_instancia,), _kwargs = mock_reanudar.call_args
        self.assertEqual(llamada_instancia.pk, instancia.pk)
        self.assertEqual(procesadas, 1)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

    def test_reanudacion_automatica_continua_hasta_fin(self):
        instancia, _ = self._crear_instancia_en_espera(vencida=True)
        reanudar_esperas_vencidas()
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_dos_ejecuciones_del_scheduler_no_duplican_avance(self):
        instancia, _ = self._crear_instancia_en_espera(vencida=True)

        primera = reanudar_esperas_vencidas()
        cantidad_ejecuciones_tras_primera = instancia.ejecuciones_etapa.count()

        segunda = reanudar_esperas_vencidas()
        cantidad_ejecuciones_tras_segunda = instancia.ejecuciones_etapa.count()

        self.assertEqual(primera, 1)
        self.assertEqual(segunda, 0)
        self.assertEqual(cantidad_ejecuciones_tras_primera, cantidad_ejecuciones_tras_segunda)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_candidato_invalidado_concurrentemente_no_rompe_el_lote(self):
        """Simula que, entre seleccionar los candidatos y procesarlos, otra
        ejecución (otro worker, u otra reanudación programática) ya resolvió
        uno de ellos — `reanudar_instancia` lo reporta como `ValueError`;
        eso es concurrencia normal, no debe impedir procesar el resto."""
        instancia_invalidada, _ = self._crear_instancia_en_espera(vencida=True)
        instancia_valida, _ = self._crear_instancia_en_espera(vencida=True)

        def _side_effect(instancia, **kwargs):
            if instancia.pk == instancia_invalidada.pk:
                raise ValueError("La instancia no tiene una ejecución vigente en espera.")
            return None

        with patch("apps.workflows.tasks.reanudar_instancia", side_effect=_side_effect) as mock_reanudar:
            procesadas = reanudar_esperas_vencidas()

        self.assertEqual(mock_reanudar.call_count, 2)
        self.assertEqual(procesadas, 1)

    def test_instancia_con_error_no_impide_procesar_las_siguientes(self):
        """Si el motor deja una instancia en `ERROR` al reanudarla,
        `reanudar_instancia` no lanza excepción (ERROR es un estado, no un
        fallo de la llamada) — la tarea debe seguir procesando el resto del
        lote con normalidad."""
        workflow_error = Workflow.objects.create(nombre="Con error tras espera")
        version = WorkflowVersion.objects.create(workflow=workflow_error, numero=1)
        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        espera = Etapa.objects.create(
            version=version,
            tipo=Etapa.Tipo.ESPERA,
            nombre="Esperar",
            configuracion={"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "DIAS"},
        )
        hito_fallido = Etapa.objects.create(version=version, tipo=Etapa.Tipo.HITO, nombre="Falla")
        fin = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin")
        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=espera)
        TransicionEtapa.objects.create(etapa_origen=espera, etapa_destino=hito_fallido)
        TransicionEtapa.objects.create(etapa_origen=hito_fallido, etapa_destino=fin)
        activar_version(workflow_error, version, actor=self.actor)
        workflow_error.refresh_from_db()

        with patch.dict("apps.workflows.estrategias.ESTRATEGIAS_POR_TIPO", {"HITO": _EstrategiaFalloFuncional()}):
            instancia_con_error = iniciar_workflow(workflow_error, actor=self.actor)
            ejecucion = instancia_con_error.ejecuciones_etapa.get(etapa=espera)
            ejecucion.resultado["reanudar_en"] = (timezone.now() - timedelta(minutes=1)).isoformat()
            ejecucion.save(update_fields=["resultado"])

            instancia_ok, _ = self._crear_instancia_en_espera(vencida=True)

            procesadas = reanudar_esperas_vencidas()

        self.assertEqual(procesadas, 2)
        instancia_con_error.refresh_from_db()
        instancia_ok.refresh_from_db()
        self.assertEqual(instancia_con_error.estado, InstanciaWorkflow.Estado.ERROR)
        self.assertEqual(instancia_ok.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_solo_se_procesan_instancias_en_espera(self):
        """Defensa a nivel de consulta: aunque una `InstanciaEtapa` quede
        con estado `EN_ESPERA` (dato corrupto/heredado), si la instancia ya
        no está `EN_ESPERA` no debe seleccionarse — el filtro conjunto
        (`InstanciaEtapa.estado` + `instancia_workflow__estado`) es el que
        garantiza "vigente", no un solo lado."""
        instancia, _ = self._crear_instancia_en_espera(dias_duracion=1, vencida=False)
        instancia.estado = InstanciaWorkflow.Estado.COMPLETADA
        instancia.save(update_fields=["estado"])
        ejecucion = instancia.ejecuciones_etapa.latest("orden")
        ejecucion.resultado["reanudar_en"] = (timezone.now() - timedelta(minutes=1)).isoformat()
        ejecucion.save(update_fields=["resultado"])

        self.assertNotIn(instancia.pk, list(_candidatos_esperas_vencidas()))

    def test_no_se_intenta_reanudar_antes_de_reanudar_en(self):
        instancia, _ = self._crear_instancia_en_espera(dias_duracion=3, vencida=False)
        procesadas = reanudar_esperas_vencidas()
        self.assertEqual(procesadas, 0)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

    def test_actor_automatico_no_requiere_permiso_workflows_ejecutar(self):
        """W.8: la reanudación automática (origen SISTEMA) nunca debe
        consultar `workflows.ejecutar` — ese permiso gobierna una
        reanudación *solicitada por una persona*. Si la tarea alguna vez
        empezara a delegar en `puede_ejecutar_workflows`, este mock lo
        detectaría (nadie tiene el permiso otorgado en este test)."""
        instancia, _ = self._crear_instancia_en_espera(vencida=True)

        with patch("apps.workflows.autorizacion.puede_ejecutar_workflows") as mock_permiso:
            procesadas = reanudar_esperas_vencidas()

        mock_permiso.assert_not_called()
        self.assertEqual(procesadas, 1)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_tarea_periodica_registrada_en_celery_beat(self):
        """Verifica el dato sembrado por la migración `0005_seed_periodic_
        task_reanudar_esperas` — no una prueba de configuración de Celery en
        sí (frágil), solo que la fila que Beat lee ya quedó creada."""
        tarea = PeriodicTask.objects.get(name="workflows.reanudar_esperas_vencidas")
        self.assertEqual(tarea.task, "apps.workflows.tasks.reanudar_esperas_vencidas")
        self.assertTrue(tarea.enabled)
        self.assertEqual(tarea.interval.every, 1)
        self.assertEqual(tarea.interval.period, "minutes")


class MotorTareaTests(TestCase):
    """3.3 — Integración Tarea↔Workflow. El dominio `Tarea` en sí (estados,
    asignación, delegación, subtareas, autorización, historial) se prueba
    en `apps/tareas/tests.py`; aquí solo se prueba la INTEGRACIÓN: que
    `EstrategiaTarea` cree exactamente una `Tarea` vinculada, que el motor
    quede `EN_ESPERA`/`motivo_espera=TAREA`, y que
    `apps.workflows.integracion.completar_tarea_workflow` sea la única vía
    que reanuda el Workflow correspondiente (W.6/W.7)."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("motortarea1", password=CLAVE_PRUEBA)
        self.ejecutor = Usuario.objects.create_user("motortarea1_ejecutor", password=CLAVE_PRUEBA)

    def test_strategy_crea_exactamente_una_tarea(self):
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])

        self.assertEqual(Tarea.objects.filter(vinculo_workflow__instancia_etapa=ejecucion).count(), 1)
        vinculo = TareaWorkflow.objects.get(instancia_etapa=ejecucion)
        self.assertEqual(vinculo.tarea.usuario_responsable_id, self.ejecutor.id)
        self.assertEqual(vinculo.tarea.origen, Tarea.Origen.SISTEMA)
        self.assertIsNone(vinculo.tarea.creada_por_id)

    def test_instancia_queda_en_espera_motivo_tarea(self):
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        self.assertEqual(ejecucion.estado, InstanciaEtapa.Estado.EN_ESPERA)
        self.assertEqual(ejecucion.motivo_espera, InstanciaEtapa.MotivoEspera.TAREA)

    def test_tarea_workflow_unico_en_ambos_lados(self):
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])
        vinculo = TareaWorkflow.objects.get(instancia_etapa=ejecucion)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TareaWorkflow.objects.create(tarea=vinculo.tarea, instancia_etapa=ejecucion)

        otra_tarea = Tarea.objects.create(titulo="Otra")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TareaWorkflow.objects.create(tarea=otra_tarea, instancia_etapa=ejecucion)

    def test_completar_tarea_principal_continua_el_workflow(self):
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])
        tarea = TareaWorkflow.objects.get(instancia_etapa=ejecucion).tarea

        completar_tarea_workflow(tarea, self.ejecutor)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_flujo_inicio_tarea_fin_llega_a_completada(self):
        """Flujo real completo: iniciar workflow → crea Tarea → Workflow
        EN_ESPERA → completar Tarea → Workflow continúa → FIN → COMPLETADA
        (W.19)."""
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

        tarea = TareaWorkflow.objects.get(instancia_etapa__instancia_workflow=instancia).tarea
        completar_tarea_workflow(tarea, self.ejecutor)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertEqual(instancia.ejecuciones_etapa.get(etapa=etapas["fin"]).estado, InstanciaEtapa.Estado.COMPLETADA)

    def test_completar_subtarea_no_reanuda_workflow(self):
        """W.6: solo la Tarea principal (con `TareaWorkflow`) puede
        producir continuación — una subtarea nunca la tiene."""
        workflow, etapas = _crear_workflow_activo_con_tarea(
            self.actor, usuario_responsable=self.ejecutor, permite_subtareas=True
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        tarea = TareaWorkflow.objects.get(instancia_etapa__instancia_workflow=instancia).tarea
        subtarea = crear_subtarea(tarea, self.ejecutor, titulo="Sub", usuario_responsable=self.ejecutor)

        completar_tarea(subtarea, self.ejecutor)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        with self.assertRaises(ValueError):
            completar_tarea_workflow(subtarea, self.ejecutor)

    def test_completar_tarea_workflow_sin_vinculo_falla(self):
        tarea_independiente = Tarea.objects.create(titulo="Sin workflow", usuario_responsable=self.ejecutor)
        with self.assertRaises(ValueError):
            completar_tarea_workflow(tarea_independiente, self.ejecutor)

    def test_scheduler_temporal_ignora_motivo_tarea(self):
        """El scheduler de 3.2.x (`_candidatos_esperas_vencidas`) nunca
        debe seleccionar una `InstanciaEtapa` bloqueada por `motivo_espera=
        TAREA`, ni `reanudar_esperas_vencidas` debe intentar reanudarla."""
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertNotIn(instancia.pk, list(_candidatos_esperas_vencidas()))
        procesadas = reanudar_esperas_vencidas()
        self.assertEqual(procesadas, 0)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

    def test_reanudar_instancia_rechaza_espera_bloqueada_por_tarea(self):
        """Defensa adicional en `reanudar_instancia` (temporal): nunca debe
        liberar una ejecución cuyo `motivo_espera` es TAREA, aunque alguien
        la invoque directamente."""
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)

        with self.assertRaises(ValueError):
            reanudar_instancia(instancia)

    def test_continuar_espera_externa_motivo_incorrecto_falla(self):
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])

        with self.assertRaises(ValueError):
            continuar_espera_externa(ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.TEMPORAL)


class RegresionTransicionSeleccionadaTests(TestCase):
    """3.4 — `continuar_espera_externa(..., transicion_seleccionada=None)`
    debe preservar EXACTAMENTE el comportamiento anterior de TAREA/ESPERA
    (instrucción explícita: pruebas de regresión obligatorias)."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("regresion_ts", password=CLAVE_PRUEBA)
        self.ejecutor = Usuario.objects.create_user("regresion_ts_ej", password=CLAVE_PRUEBA)

    def test_tarea_continua_con_transicion_seleccionada_none_explicito(self):
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])

        continuar_espera_externa(
            ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.TAREA, transicion_seleccionada=None
        )
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        ejecucion.refresh_from_db()
        self.assertEqual(ejecucion.transicion_tomada_id, etapas["tarea"].transiciones_salientes.get().pk)

    def test_espera_temporal_sigue_usando_first_via_reanudar_instancia(self):
        """`reanudar_instancia` nunca pasa `transicion_seleccionada` — debe
        seguir cayendo en `.first()` exactamente como antes de 3.4."""
        workflow, etapas = _crear_workflow_activo_con_espera(
            self.actor, {"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "HORAS"}
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["espera"])
        InstanciaEtapa.objects.filter(pk=ejecucion.pk).update(
            resultado={"reanudar_en": (timezone.now() - timedelta(hours=1)).isoformat()}
        )
        instancia.refresh_from_db()

        reanudar_instancia(instancia)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)

    def test_transicion_seleccionada_de_otra_etapa_es_rechazada(self):
        """Defensa nueva de 3.4: `continuar_espera_externa` nunca acepta
        una `TransicionEtapa` que no salga de la etapa de la ejecución
        indicada — evita continuar por una rama de OTRA etapa ante un
        error de quien llama."""
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["tarea"])

        otro_workflow, otras_etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.actor)],
        )
        transicion_ajena = otras_etapas["aprobacion"].transiciones_salientes.get(resultado_aprobacion="APROBADA")

        with self.assertRaises(ValueError):
            continuar_espera_externa(
                ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.TAREA, transicion_seleccionada=transicion_ajena
            )


class MotorAprobacionTests(TestCase):
    """3.4 (CU-024/025, RQF-078 a 084) — Strategy/motor/integración de
    APROBACION, mismo nivel de prueba que `MotorTareaTests`. El dominio
    Aprobaciones en sí (política, secuencial, equipo, RN-025/026,
    concurrencia interna) se prueba íntegramente en
    `apps/aprobaciones/tests.py`, sin depender de Workflow — aquí solo se
    prueba la INTEGRACIÓN entre ambos dominios."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("motoraprob1", password=CLAVE_PRUEBA)
        self.aprobador = Usuario.objects.create_user("motoraprob1_aprobador", password=CLAVE_PRUEBA)

    def test_strategy_crea_esquema_con_participantes_y_vinculo(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["aprobacion"])

        vinculo = EsquemaAprobacionWorkflow.objects.get(instancia_etapa=ejecucion)
        self.assertEqual(vinculo.esquema.modo, EsquemaAprobacion.Modo.PARALELA)
        self.assertEqual(vinculo.esquema.participaciones.count(), 1)
        self.assertEqual(vinculo.esquema.participaciones.get().aprobador_usuario_id, self.aprobador.id)

    def test_instancia_queda_en_espera_motivo_aprobacion(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["aprobacion"])

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        self.assertEqual(ejecucion.estado, InstanciaEtapa.Estado.EN_ESPERA)
        self.assertEqual(ejecucion.motivo_espera, InstanciaEtapa.MotivoEspera.APROBACION)

    def test_aprobar_continua_por_la_rama_aprobada(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        aprobacion = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=instancia
        ).esquema.participaciones.get()

        resolver_aprobacion_workflow(aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertEqual(instancia.ejecuciones_etapa.get(etapa=etapas["fin_aprobada"]).estado, InstanciaEtapa.Estado.COMPLETADA)

    def test_rechazar_continua_por_la_rama_rechazada(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        aprobacion = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=instancia
        ).esquema.participaciones.get()

        resolver_aprobacion_workflow(
            aprobacion, self.aprobador, decision=Aprobacion.Estado.RECHAZADA, observacion="No."
        )

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertEqual(instancia.ejecuciones_etapa.get(etapa=etapas["fin_rechazada"]).estado, InstanciaEtapa.Estado.COMPLETADA)

    def test_devolver_continua_por_la_rama_devuelta(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        aprobacion = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=instancia
        ).esquema.participaciones.get()

        resolver_aprobacion_workflow(
            aprobacion, self.aprobador, decision=Aprobacion.Estado.DEVUELTA, observacion="Ajustar."
        )

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertEqual(instancia.ejecuciones_etapa.get(etapa=etapas["fin_devuelta"]).estado, InstanciaEtapa.Estado.COMPLETADA)

    def test_secuencial_no_continua_workflow_hasta_que_cierra(self):
        otro_aprobador = Usuario.objects.create_user("motoraprob1_otro", password=CLAVE_PRUEBA)
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.SECUENCIAL,
            participantes=[
                (Aprobacion.TipoAprobador.USUARIO, self.aprobador),
                (Aprobacion.TipoAprobador.USUARIO, otro_aprobador),
            ],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        esquema = EsquemaAprobacionWorkflow.objects.get(instancia_etapa__instancia_workflow=instancia).esquema
        primera = esquema.participaciones.order_by("orden").first()

        resolver_aprobacion_workflow(primera, self.aprobador, decision=Aprobacion.Estado.APROBADA)

        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

        segunda = esquema.participaciones.order_by("orden").last()
        resolver_aprobacion_workflow(segunda, otro_aprobador, decision=Aprobacion.Estado.APROBADA)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertEqual(instancia.ejecuciones_etapa.get(etapa=etapas["fin_aprobada"]).estado, InstanciaEtapa.Estado.COMPLETADA)

    def test_resolver_aprobacion_workflow_sin_vinculo_falla(self):
        esquema_independiente = crear_esquema_aprobacion(
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        aprobacion = esquema_independiente.participaciones.get()
        with self.assertRaises(ValueError):
            resolver_aprobacion_workflow(aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)

    def test_scheduler_temporal_ignora_motivo_aprobacion(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)

        self.assertNotIn(instancia.pk, list(_candidatos_esperas_vencidas()))
        procesadas = reanudar_esperas_vencidas()
        self.assertEqual(procesadas, 0)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)

    def test_reanudar_instancia_rechaza_espera_bloqueada_por_aprobacion(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        with self.assertRaises(ValueError):
            reanudar_instancia(instancia)

    def test_continuar_espera_externa_motivo_incorrecto_falla(self):
        workflow, etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.aprobador)],
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        ejecucion = instancia.ejecuciones_etapa.get(etapa=etapas["aprobacion"])
        with self.assertRaises(ValueError):
            continuar_espera_externa(ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.TAREA)


class ConcurrenciaResolverAprobacionWorkflowTests(TransactionTestCase):
    """Dos aprobadores paralelos (CUALQUIERA) resolviendo concurrentemente
    la MISMA aprobación vinculada a Workflow — solo uno debe avanzar el
    Workflow, mismo patrón que `ConcurrenciaCompletarTareaWorkflowTests`."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("concurrencia_aprob_wf", password=CLAVE_PRUEBA)
        self.a = Usuario.objects.create_user("concurrencia_aprob_wf_a", password=CLAVE_PRUEBA)
        self.b = Usuario.objects.create_user("concurrencia_aprob_wf_b", password=CLAVE_PRUEBA)
        workflow, self.etapas = _crear_workflow_activo_con_aprobacion(
            self.actor,
            modo=EsquemaAprobacion.Modo.PARALELA,
            politica=EsquemaAprobacion.Politica.CUALQUIERA,
            participantes=[(Aprobacion.TipoAprobador.USUARIO, self.a), (Aprobacion.TipoAprobador.USUARIO, self.b)],
        )
        self.instancia = iniciar_workflow(workflow, actor=self.actor)
        self.esquema = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=self.instancia
        ).esquema

    def test_doble_resolucion_concurrente_no_duplica_avance(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _resolver(usuario, clave):
            barrera.wait()
            try:
                aprobacion = self.esquema.participaciones.get(aprobador_usuario=usuario)
                resolver_aprobacion_workflow(aprobacion, usuario, decision=Aprobacion.Estado.APROBADA)
                resultados[clave] = "ok"
            except ValidationError:
                resultados[clave] = "ya_cerrada"
            finally:
                connection.close()

        hilo_a = threading.Thread(target=_resolver, args=(self.a, "a"))
        hilo_b = threading.Thread(target=_resolver, args=(self.b, "b"))
        hilo_a.start()
        hilo_b.start()
        hilo_a.join()
        hilo_b.join()

        valores = list(resultados.values())
        self.assertEqual(valores.count("ok"), 1)
        self.assertEqual(valores.count("ya_cerrada"), 1)

        instancia = InstanciaWorkflow.objects.get(pk=self.instancia.pk)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertEqual(instancia.ejecuciones_etapa.filter(etapa=self.etapas["fin_aprobada"]).count(), 1)


class ConcurrenciaCompletarTareaWorkflowTests(TransactionTestCase):
    """Doble `completar_tarea_workflow` concurrente sobre la MISMA Tarea —
    solo uno debe avanzar el Workflow, mismo patrón
    (`TransactionTestCase` + hilos reales) que
    `ConcurrenciaReanudarInstanciaTests`."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("concurrencia_tarea_wf", password=CLAVE_PRUEBA)
        self.ejecutor = Usuario.objects.create_user("concurrencia_tarea_wf_ej", password=CLAVE_PRUEBA)
        workflow, etapas = _crear_workflow_activo_con_tarea(self.actor, usuario_responsable=self.ejecutor)
        self.instancia = iniciar_workflow(workflow, actor=self.actor)
        self.tarea = TareaWorkflow.objects.get(instancia_etapa__instancia_workflow=self.instancia).tarea

    def test_doble_completar_concurrente_no_duplica_avance(self):
        resultados = {}
        barrera = threading.Barrier(2)

        def _intentar_completar(clave):
            barrera.wait()
            try:
                tarea = Tarea.objects.get(pk=self.tarea.pk)
                completar_tarea_workflow(tarea, self.ejecutor)
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

        instancia = InstanciaWorkflow.objects.get(pk=self.instancia.pk)
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        # Un solo avance real: la ejecución FIN existe una única vez.
        self.assertEqual(instancia.ejecuciones_etapa.filter(etapa__tipo=Etapa.Tipo.FIN).count(), 1)


class DefinitionOfDoneTests(TestCase):
    """Prueba integral obligatoria (cierre de Sprint 3): demuestra que un
    workflow enteramente configurado por datos —
    INICIO → CONDICION → TAREA → APROBACION → ESPERA → FIN — genera
    trabajo real y llega a COMPLETADA sin programar ese flujo específico
    en Python. Construida SOLO con `Workflow`/`WorkflowVersion`/`Etapa`/
    `TransicionEtapa`/`ConfiguracionEtapaTarea`/`ConfiguracionEtapaAprobacion`
    + `ParticipanteEtapaAprobacion` (configuración) y avanzada
    exclusivamente con las 4 funciones públicas de dominio ya existentes
    (`iniciar_workflow`, `completar_tarea_workflow`,
    `resolver_aprobacion_workflow`, `reanudar_instancia`) — ninguna de las
    cuales conoce este flujo en particular.

    **Nota de alcance explícita** (instrucción del usuario: no inventar la
    integración SERVICIO→Workflow si no existe todavía): esta prueba
    arranca desde `iniciar_workflow(workflow, ...)` directamente, no desde
    un `Servicio`/`Ticket`. La conexión `apps.catalogo.Servicio` →
    `iniciar_workflow` **no existe en el código actual** — no hay ningún
    botón, señal ni operación que, al radicar un Ticket de tipo SERVICIO,
    dispare `iniciar_workflow` — esa integración es RQF-085/Sprint 5
    (Procesos), fuera del alcance de 3.1-3.4. Ver el informe final (gap
    de cierre del Definition of Done)."""

    def setUp(self):
        self.actor = Usuario.objects.create_user("dod_actor", password=CLAVE_PRUEBA)
        self.ejecutor = Usuario.objects.create_user("dod_ejecutor", password=CLAVE_PRUEBA)
        self.aprobador = Usuario.objects.create_user("dod_aprobador", password=CLAVE_PRUEBA)

        self.workflow = Workflow.objects.create(nombre="DoD: Condición-Tarea-Aprobación-Espera")
        version = WorkflowVersion.objects.create(workflow=self.workflow, numero=1)

        inicio = Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        condicion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.CONDICION, nombre="¿Monto alto?")
        fin_temprano = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin temprano")
        tarea = Etapa.objects.create(version=version, tipo=Etapa.Tipo.TAREA, nombre="Revisar solicitud")
        aprobacion = Etapa.objects.create(version=version, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar solicitud")
        fin_rechazada = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin rechazada")
        fin_devuelta = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin devuelta")
        espera = Etapa.objects.create(
            version=version,
            tipo=Etapa.Tipo.ESPERA,
            nombre="Esperar publicación",
            configuracion={"modo": "DURACION", "duracion_valor": 1, "duracion_unidad": "HORAS"},
        )
        fin_completo = Etapa.objects.create(version=version, tipo=Etapa.Tipo.FIN, nombre="Fin completo")

        TransicionEtapa.objects.create(etapa_origen=inicio, etapa_destino=condicion)
        # Condicional real (monto alto → termina temprano, rama NO
        # ejercitada por esta prueba) + fallback (monto ausente/bajo →
        # continúa a TAREA, rama SÍ ejercitada) — CONDICION exige ambas.
        TransicionEtapa.objects.create(
            etapa_origen=condicion,
            etapa_destino=fin_temprano,
            prioridad=1,
            variable="monto",
            operador=TransicionEtapa.Operador.MAYOR_QUE,
            valor="1000000",
        )
        TransicionEtapa.objects.create(etapa_origen=condicion, etapa_destino=tarea, es_fallback=True)
        TransicionEtapa.objects.create(etapa_origen=tarea, etapa_destino=aprobacion)
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=espera, resultado_aprobacion="APROBADA"
        )
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=fin_rechazada, resultado_aprobacion="RECHAZADA"
        )
        TransicionEtapa.objects.create(
            etapa_origen=aprobacion, etapa_destino=fin_devuelta, resultado_aprobacion="DEVUELTA"
        )
        TransicionEtapa.objects.create(etapa_origen=espera, etapa_destino=fin_completo)

        ConfiguracionEtapaTarea.objects.create(
            etapa=tarea,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO,
            usuario_responsable=self.ejecutor,
        )
        configuracion_aprobacion = ConfiguracionEtapaAprobacion.objects.create(
            etapa=aprobacion,
            modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
        )
        ParticipanteEtapaAprobacion.objects.create(
            configuracion=configuracion_aprobacion,
            orden=1,
            tipo_aprobador=ParticipanteEtapaAprobacion.TipoAprobador.USUARIO,
            usuario=self.aprobador,
        )

        errores = validar_estructura(version)
        assert errores == [], errores
        activar_version(self.workflow, version, actor=self.actor)
        self.workflow.refresh_from_db()
        self.etapas = {
            "inicio": inicio,
            "condicion": condicion,
            "tarea": tarea,
            "aprobacion": aprobacion,
            "espera": espera,
            "fin_completo": fin_completo,
            "fin_temprano": fin_temprano,
            "fin_rechazada": fin_rechazada,
            "fin_devuelta": fin_devuelta,
        }

    def test_flujo_configurable_completo_sin_codigo_especifico(self):
        # 1) INICIO → CONDICION → (fallback) → TAREA: automático, sin
        #    intervención humana, la instancia queda EN_ESPERA por TAREA.
        instancia = iniciar_workflow(self.workflow, actor=self.actor)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        ejecucion_tarea = instancia.ejecuciones_etapa.get(etapa=self.etapas["tarea"])
        self.assertEqual(ejecucion_tarea.motivo_espera, InstanciaEtapa.MotivoEspera.TAREA)
        ejecucion_condicion = instancia.ejecuciones_etapa.get(etapa=self.etapas["condicion"])
        self.assertEqual(
            ejecucion_condicion.transicion_tomada.etapa_destino_id, self.etapas["tarea"].pk
        )

        # 2) Trabajo humano real: completar la Tarea → avanza a APROBACION,
        #    la instancia queda EN_ESPERA por APROBACION.
        tarea = TareaWorkflow.objects.get(instancia_etapa=ejecucion_tarea).tarea
        completar_tarea_workflow(tarea, self.ejecutor)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        ejecucion_aprobacion = instancia.ejecuciones_etapa.get(etapa=self.etapas["aprobacion"])
        self.assertEqual(ejecucion_aprobacion.motivo_espera, InstanciaEtapa.MotivoEspera.APROBACION)
        esquema = EsquemaAprobacionWorkflow.objects.get(instancia_etapa=ejecucion_aprobacion).esquema
        self.assertEqual(esquema.participaciones.count(), 1)

        # 3) Trabajo humano real: aprobar → avanza a ESPERA (temporal), la
        #    instancia queda EN_ESPERA por TEMPORAL.
        aprobacion = esquema.participaciones.get()
        resolver_aprobacion_workflow(aprobacion, self.aprobador, decision=Aprobacion.Estado.APROBADA)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.EN_ESPERA)
        ejecucion_espera = instancia.ejecuciones_etapa.get(etapa=self.etapas["espera"])
        self.assertEqual(ejecucion_espera.motivo_espera, InstanciaEtapa.MotivoEspera.TEMPORAL)

        # 4) Vencimiento de la espera temporal (mismo mecanismo de 3.2.x) →
        #    reanudar_instancia → FIN → COMPLETADA.
        InstanciaEtapa.objects.filter(pk=ejecucion_espera.pk).update(
            resultado={"reanudar_en": (timezone.now() - timedelta(hours=1)).isoformat()}
        )
        instancia.refresh_from_db()
        reanudar_instancia(instancia)
        instancia.refresh_from_db()

        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)
        self.assertIsNotNone(instancia.finalizada_en)
        self.assertEqual(
            instancia.ejecuciones_etapa.get(etapa=self.etapas["fin_completo"]).estado,
            InstanciaEtapa.Estado.COMPLETADA,
        )
        # Ninguna rama alternativa (temprana/rechazada/devuelta) se tocó.
        for clave in ("fin_temprano", "fin_rechazada", "fin_devuelta"):
            self.assertFalse(instancia.ejecuciones_etapa.filter(etapa=self.etapas[clave]).exists())

    def test_reanudacion_automatica_via_scheduler_tras_aprobacion(self):
        """El mismo flujo, pero la última espera (TEMPORAL) se libera vía
        el scheduler de 3.2.x (`reanudar_esperas_vencidas`), no llamando a
        `reanudar_instancia` a mano — demuestra que la integración con
        Celery Beat sigue intacta sin cambios de 3.4."""
        instancia = iniciar_workflow(self.workflow, actor=self.actor)
        ejecucion_tarea = instancia.ejecuciones_etapa.get(etapa=self.etapas["tarea"])
        tarea = TareaWorkflow.objects.get(instancia_etapa=ejecucion_tarea).tarea
        completar_tarea_workflow(tarea, self.ejecutor)

        instancia.refresh_from_db()
        esquema = EsquemaAprobacionWorkflow.objects.get(
            instancia_etapa__instancia_workflow=instancia
        ).esquema
        resolver_aprobacion_workflow(
            esquema.participaciones.get(), self.aprobador, decision=Aprobacion.Estado.APROBADA
        )

        ejecucion_espera = instancia.ejecuciones_etapa.get(etapa=self.etapas["espera"])
        InstanciaEtapa.objects.filter(pk=ejecucion_espera.pk).update(
            resultado={"reanudar_en": (timezone.now() - timedelta(hours=1)).isoformat()}
        )

        procesadas = reanudar_esperas_vencidas()
        self.assertEqual(procesadas, 1)
        instancia.refresh_from_db()
        self.assertEqual(instancia.estado, InstanciaWorkflow.Estado.COMPLETADA)


# ----------------------------------------------------------------------------
# 3.UI.4 — Editor funcional de Workflow. Dos bloques: (1) las 10 nuevas
# operaciones de dominio de `apps/workflows/editor.py` (Etapa/configuración/
# Transición) — sin HTTP; (2) pruebas HTTP de `apps/workflows/views.py` que
# exponen ese editor. No se duplica la suite estructural ya existente
# (`ValidacionEstructuraTests`/`VersionamientoTests`/etc.) — aquí se
# verifica la nueva frontera de mutación (crear/editar/eliminar/cambiar
# tipo, configuración especializada, transiciones normalizadas) y su
# integración HTTP.
# ----------------------------------------------------------------------------


class EditorEtapaTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="editor_etapa_actor", password=CLAVE_PRUEBA)
        self.workflow = crear_workflow(self.actor, nombre="Editor etapas")
        self.version = self.workflow.versiones.get()

    def test_crear_etapa_hito(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Revisar")
        self.assertEqual(etapa.version_id, self.version.pk)
        self.assertEqual(etapa.tipo, Etapa.Tipo.HITO)

    def test_crear_etapa_rechaza_ticket(self):
        with self.assertRaises(ValidationError):
            editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.TICKET, nombre="No debería crearse")
        self.assertEqual(self.version.etapas.count(), 0)

    def test_crear_etapa_rechaza_gaceta(self):
        with self.assertRaises(ValidationError):
            editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.GACETA, nombre="No debería crearse")
        self.assertEqual(self.version.etapas.count(), 0)

    def test_crear_etapa_registra_auditoria(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Auditado")
        auditorias = _auditorias_de(etapa)
        self.assertEqual(auditorias.filter(accion=RegistroAuditoria.Accion.CREAR).count(), 1)

    def test_editar_etapa_nombre_y_descripcion(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Original")
        editor.editar_etapa(etapa, self.actor, nombre="Editado", descripcion="Nueva descripción")
        etapa.refresh_from_db()
        self.assertEqual(etapa.nombre, "Editado")
        self.assertEqual(etapa.descripcion, "Nueva descripción")

    def test_editar_etapa_registra_auditoria(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Original")
        editor.editar_etapa(etapa, self.actor, nombre="Editado")
        auditorias = _auditorias_de(etapa)
        self.assertEqual(auditorias.filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).count(), 1)

    def test_eliminar_etapa_sin_conexiones_exitoso(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Suelta")
        editor.eliminar_etapa(etapa, self.actor)
        self.assertFalse(Etapa.objects.filter(pk=etapa.pk).exists())

    def test_eliminar_etapa_con_transicion_entrante_bloqueada(self):
        inicio = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        hito = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        editor.crear_transicion(inicio, hito, self.actor)
        with self.assertRaises(ValidationError):
            editor.eliminar_etapa(hito, self.actor)
        self.assertTrue(Etapa.objects.filter(pk=hito.pk).exists())

    def test_eliminar_etapa_con_transicion_saliente_bloqueada(self):
        hito = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        fin = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.FIN, nombre="Fin")
        editor.crear_transicion(hito, fin, self.actor)
        with self.assertRaises(ValidationError):
            editor.eliminar_etapa(hito, self.actor)
        self.assertTrue(Etapa.objects.filter(pk=hito.pk).exists())

    def test_eliminar_etapa_registra_auditoria_antes_de_borrar(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Para eliminar")
        etapa_pk = etapa.pk
        editor.eliminar_etapa(etapa, self.actor)
        registro = RegistroAuditoria.objects.filter(
            content_type=ContentType.objects.get_for_model(Etapa),
            object_id=etapa_pk,
            accion=RegistroAuditoria.Accion.ELIMINAR,
        )
        self.assertEqual(registro.count(), 1)
        self.assertIsNotNone(registro.get().datos_anteriores)


class CambiarTipoEtapaTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="cambiartipo_actor", password=CLAVE_PRUEBA)
        self.workflow = crear_workflow(self.actor, nombre="Cambio de tipo")
        self.version = self.workflow.versiones.get()

    def test_cambio_con_transicion_saliente_rechazado(self):
        hito = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        fin = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.FIN, nombre="Fin")
        editor.crear_transicion(hito, fin, self.actor)
        with self.assertRaises(ValidationError):
            editor.cambiar_tipo_etapa(hito, self.actor, nuevo_tipo=Etapa.Tipo.CONDICION)
        hito.refresh_from_db()
        self.assertEqual(hito.tipo, Etapa.Tipo.HITO)

    def test_cambio_con_solo_transicion_entrante_permitido(self):
        inicio = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        hito = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        editor.crear_transicion(inicio, hito, self.actor)
        editor.cambiar_tipo_etapa(hito, self.actor, nuevo_tipo=Etapa.Tipo.CONDICION)
        hito.refresh_from_db()
        self.assertEqual(hito.tipo, Etapa.Tipo.CONDICION)

    def test_tarea_a_hito_limpia_configuracion_tarea(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.TAREA, nombre="Tarea")
        usuario = Usuario.objects.create_user(username="cambiartipo_resp", password=CLAVE_PRUEBA)
        editor.configurar_etapa_tarea(
            etapa,
            self.actor,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO,
            usuario_responsable=usuario,
        )
        editor.cambiar_tipo_etapa(etapa, self.actor, nuevo_tipo=Etapa.Tipo.HITO)
        self.assertFalse(ConfiguracionEtapaTarea.objects.filter(etapa=etapa).exists())

    def test_aprobacion_a_hito_limpia_configuracion_y_participantes(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        aprobador = Usuario.objects.create_user(username="cambiartipo_aprobador", password=CLAVE_PRUEBA)
        editor.configurar_etapa_aprobacion(
            etapa,
            self.actor,
            modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
            participantes=[(ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, aprobador)],
        )
        configuracion_pk = etapa.configuracion_aprobacion.pk
        editor.cambiar_tipo_etapa(etapa, self.actor, nuevo_tipo=Etapa.Tipo.HITO)
        self.assertFalse(ConfiguracionEtapaAprobacion.objects.filter(pk=configuracion_pk).exists())
        self.assertFalse(ParticipanteEtapaAprobacion.objects.filter(configuracion_id=configuracion_pk).exists())

    def test_espera_a_hito_limpia_configuracion_json(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.ESPERA, nombre="Esperar")
        editor.configurar_etapa_espera(etapa, self.actor, modo="DURACION", duracion_valor=3, duracion_unidad="DIAS")
        editor.cambiar_tipo_etapa(etapa, self.actor, nuevo_tipo=Etapa.Tipo.HITO)
        etapa.refresh_from_db()
        self.assertEqual(etapa.configuracion, {})

    def test_cambiar_a_ticket_rechazado(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        with self.assertRaises(ValidationError):
            editor.cambiar_tipo_etapa(etapa, self.actor, nuevo_tipo=Etapa.Tipo.TICKET)

    def test_mismo_tipo_es_no_op(self):
        etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        resultado = editor.cambiar_tipo_etapa(etapa, self.actor, nuevo_tipo=Etapa.Tipo.HITO)
        self.assertEqual(resultado.pk, etapa.pk)


class ConfigurarEtapaTareaTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="conftarea_actor", password=CLAVE_PRUEBA)
        self.workflow = crear_workflow(self.actor, nombre="Config tarea")
        self.version = self.workflow.versiones.get()
        self.etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.TAREA, nombre="Tarea")

    def test_version_activa_conserva_configuracion_sin_efectos_parciales(self):
        editor.configurar_etapa_tarea(
            self.etapa, self.actor, tipo_responsable="USUARIO",
            usuario_responsable=self.actor, permite_subtareas=True,
        )
        anteriores = list(ConfiguracionEtapaTarea.objects.values())
        self.version.estado = WorkflowVersion.Estado.ACTIVA
        self.version.save()
        etapa = Etapa.objects.get(pk=self.etapa.pk)
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_tarea(etapa, self.actor, permite_subtareas=False)
        self.assertEqual(list(ConfiguracionEtapaTarea.objects.values()), anteriores)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_configurar_con_usuario_responsable(self):
        usuario = Usuario.objects.create_user(username="conftarea_resp", password=CLAVE_PRUEBA)
        editor.configurar_etapa_tarea(
            self.etapa,
            self.actor,
            tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO,
            usuario_responsable=usuario,
            permite_subtareas=True,
        )
        configuracion = ConfiguracionEtapaTarea.objects.get(etapa=self.etapa)
        self.assertEqual(configuracion.usuario_responsable_id, usuario.id)
        self.assertTrue(configuracion.permite_subtareas)

    def test_configurar_sobre_etapa_no_tarea_falla(self):
        hito = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_tarea(hito, self.actor)

    def test_reconfigurar_reemplaza_valores_anteriores(self):
        usuario1 = Usuario.objects.create_user(username="conftarea_u1", password=CLAVE_PRUEBA)
        usuario2 = Usuario.objects.create_user(username="conftarea_u2", password=CLAVE_PRUEBA)
        editor.configurar_etapa_tarea(
            self.etapa, self.actor, tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO, usuario_responsable=usuario1
        )
        editor.configurar_etapa_tarea(
            self.etapa, self.actor, tipo_responsable=ConfiguracionEtapaTarea.TipoResponsable.USUARIO, usuario_responsable=usuario2
        )
        self.assertEqual(ConfiguracionEtapaTarea.objects.filter(etapa=self.etapa).count(), 1)
        configuracion = ConfiguracionEtapaTarea.objects.get(etapa=self.etapa)
        self.assertEqual(configuracion.usuario_responsable_id, usuario2.id)


class ConfigurarEtapaAprobacionTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="confaprob_actor", password=CLAVE_PRUEBA)
        self.workflow = crear_workflow(self.actor, nombre="Config aprobacion")
        self.version = self.workflow.versiones.get()
        self.etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        self.aprobador_a = Usuario.objects.create_user(username="confaprob_a", password=CLAVE_PRUEBA)
        self.aprobador_b = Usuario.objects.create_user(username="confaprob_b", password=CLAVE_PRUEBA)

    def test_version_activa_conserva_configuracion_y_participantes(self):
        editor.configurar_etapa_aprobacion(
            self.etapa, self.actor, modo="PARALELA", politica="TODOS",
            participantes=[("USUARIO", self.aprobador_a), ("USUARIO", self.actor)],
        )
        configuraciones = list(ConfiguracionEtapaAprobacion.objects.values())
        participantes = list(ParticipanteEtapaAprobacion.objects.order_by("pk").values())
        self.version.estado = WorkflowVersion.Estado.ACTIVA
        self.version.save()
        etapa = Etapa.objects.get(pk=self.etapa.pk)
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_aprobacion(
                etapa, self.actor, modo="SECUENCIAL",
                participantes=[("USUARIO", self.aprobador_b)],
            )
        self.assertEqual(list(ConfiguracionEtapaAprobacion.objects.values()), configuraciones)
        self.assertEqual(list(ParticipanteEtapaAprobacion.objects.order_by("pk").values()), participantes)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_configurar_paralela_cualquiera_con_dos_participantes(self):
        editor.configurar_etapa_aprobacion(
            self.etapa,
            self.actor,
            modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
            participantes=[
                (ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, self.aprobador_a),
                (ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, self.aprobador_b),
            ],
        )
        configuracion = ConfiguracionEtapaAprobacion.objects.get(etapa=self.etapa)
        self.assertEqual(configuracion.participantes.count(), 2)
        ordenes = list(configuracion.participantes.order_by("orden").values_list("orden", flat=True))
        self.assertEqual(ordenes, [1, 2])

    def test_configuracion_invalida_hace_rollback_completo(self):
        """modo=SECUENCIAL con política -> `ValidationError`; nada debe
        quedar escrito, ni siquiera una `ConfiguracionEtapaAprobacion` sin
        participantes."""
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_aprobacion(
                self.etapa,
                self.actor,
                modo=ConfiguracionEtapaAprobacion.Modo.SECUENCIAL,
                politica=ConfiguracionEtapaAprobacion.Politica.TODOS,
                participantes=[(ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, self.aprobador_a)],
            )
        self.assertFalse(ConfiguracionEtapaAprobacion.objects.filter(etapa=self.etapa).exists())

    def test_participante_invalido_no_destruye_configuracion_anterior(self):
        editor.configurar_etapa_aprobacion(
            self.etapa,
            self.actor,
            modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
            participantes=[(ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, self.aprobador_a)],
        )
        configuracion_original_pk = ConfiguracionEtapaAprobacion.objects.get(etapa=self.etapa).pk

        with self.assertRaises(ValidationError):
            editor.configurar_etapa_aprobacion(
                self.etapa,
                self.actor,
                modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
                politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
                participantes=[("TIPO_INVALIDO", self.aprobador_b)],
            )

        configuracion = ConfiguracionEtapaAprobacion.objects.get(etapa=self.etapa)
        # La configuración anterior permanece intacta: mismo PK, mismo
        # participante original — el rollback deshizo también el borrado
        # inicial (R4).
        self.assertEqual(configuracion.pk, configuracion_original_pk)
        self.assertEqual(configuracion.participantes.count(), 1)
        self.assertEqual(configuracion.participantes.get().usuario_id, self.aprobador_a.id)

    def test_sin_participantes_falla(self):
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_aprobacion(
                self.etapa,
                self.actor,
                modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
                politica=ConfiguracionEtapaAprobacion.Politica.CUALQUIERA,
                participantes=[],
            )

    def test_configurar_registra_un_solo_evento_de_auditoria(self):
        eventos = RegistroAuditoria.objects.count()
        editor.configurar_etapa_aprobacion(
            self.etapa,
            self.actor,
            modo=ConfiguracionEtapaAprobacion.Modo.PARALELA,
            politica=ConfiguracionEtapaAprobacion.Politica.TODOS,
            participantes=[
                (ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, self.aprobador_a),
                (ParticipanteEtapaAprobacion.TipoAprobador.USUARIO, self.aprobador_b),
            ],
        )
        auditorias = _auditorias_de(self.etapa).filter(accion=RegistroAuditoria.Accion.ACTUALIZAR)
        self.assertEqual(auditorias.count(), 1)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos + 1)


class ConfigurarEtapaEsperaTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="confespera_actor", password=CLAVE_PRUEBA)
        self.workflow = crear_workflow(self.actor, nombre="Config espera")
        self.version = self.workflow.versiones.get()
        self.etapa = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.ESPERA, nombre="Esperar")

    def test_version_activa_conserva_json_anterior(self):
        editor.configurar_etapa_espera(
            self.etapa, self.actor, modo="DURACION", duracion_valor=5, duracion_unidad="DIAS",
        )
        anterior = Etapa.objects.filter(pk=self.etapa.pk).values().get()
        self.version.estado = WorkflowVersion.Estado.ACTIVA
        self.version.save()
        etapa = Etapa.objects.get(pk=self.etapa.pk)
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_espera(etapa, self.actor, modo="FECHA", fecha_objetivo="2026-12-31")
        self.assertEqual(Etapa.objects.filter(pk=etapa.pk).values().get(), anterior)
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_configurar_duracion(self):
        editor.configurar_etapa_espera(self.etapa, self.actor, modo="DURACION", duracion_valor=5, duracion_unidad="DIAS")
        self.etapa.refresh_from_db()
        self.assertEqual(
            self.etapa.configuracion, {"modo": "DURACION", "duracion_valor": 5, "duracion_unidad": "DIAS"}
        )

    def test_duracion_a_fecha_no_conserva_claves_de_duracion(self):
        editor.configurar_etapa_espera(self.etapa, self.actor, modo="DURACION", duracion_valor=5, duracion_unidad="DIAS")
        editor.configurar_etapa_espera(self.etapa, self.actor, modo="FECHA", fecha_objetivo="2026-12-31")
        self.etapa.refresh_from_db()
        self.assertNotIn("duracion_valor", self.etapa.configuracion)
        self.assertNotIn("duracion_unidad", self.etapa.configuracion)
        self.assertEqual(self.etapa.configuracion, {"modo": "FECHA", "fecha_objetivo": "2026-12-31"})

    def test_fecha_a_duracion_no_conserva_fecha_objetivo(self):
        editor.configurar_etapa_espera(self.etapa, self.actor, modo="FECHA", fecha_objetivo="2026-12-31")
        editor.configurar_etapa_espera(self.etapa, self.actor, modo="DURACION", duracion_valor=2, duracion_unidad="HORAS")
        self.etapa.refresh_from_db()
        self.assertNotIn("fecha_objetivo", self.etapa.configuracion)
        self.assertEqual(self.etapa.configuracion, {"modo": "DURACION", "duracion_valor": 2, "duracion_unidad": "HORAS"})

    def test_configuracion_invalida_rechazada_por_la_strategy(self):
        with self.assertRaises(ValidationError):
            editor.configurar_etapa_espera(self.etapa, self.actor, modo="DURACION", duracion_valor=-1, duracion_unidad="DIAS")


class EditorTransicionTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="edittrans_actor", password=CLAVE_PRUEBA)
        self.workflow = crear_workflow(self.actor, nombre="Transiciones editor")
        self.version = self.workflow.versiones.get()
        self.inicio = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        self.hito = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Hito")
        self.fin = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.FIN, nombre="Fin")

    def test_crear_transicion_normal(self):
        transicion = editor.crear_transicion(self.inicio, self.hito, self.actor)
        self.assertEqual(transicion.etapa_destino_id, self.hito.pk)

    def test_transicion_normal_no_conserva_campos_condicion_ni_aprobacion(self):
        transicion = editor.crear_transicion(
            self.inicio,
            self.hito,
            self.actor,
            variable="x",
            operador="IGUAL_A",
            valor="1",
            es_fallback=True,
            resultado_aprobacion="APROBADA",
        )
        self.assertEqual(transicion.variable, "")
        self.assertEqual(transicion.operador, "")
        self.assertEqual(transicion.valor, "")
        self.assertFalse(transicion.es_fallback)
        self.assertEqual(transicion.resultado_aprobacion, "")

    def test_transicion_condicion_no_conserva_resultado_aprobacion(self):
        condicion = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.CONDICION, nombre="¿Condición?")
        transicion = editor.crear_transicion(
            condicion,
            self.hito,
            self.actor,
            variable="monto",
            operador="MAYOR_QUE",
            valor="1000",
            resultado_aprobacion="APROBADA",
        )
        self.assertEqual(transicion.resultado_aprobacion, "")
        self.assertEqual(transicion.variable, "monto")

    def test_transicion_aprobacion_no_conserva_campos_condicion(self):
        aprobacion = editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.APROBACION, nombre="Aprobar")
        transicion = editor.crear_transicion(
            aprobacion,
            self.hito,
            self.actor,
            resultado_aprobacion="RECHAZADA",
            variable="x",
            operador="IGUAL_A",
            valor="1",
            es_fallback=True,
        )
        self.assertEqual(transicion.variable, "")
        self.assertEqual(transicion.operador, "")
        self.assertEqual(transicion.valor, "")
        self.assertFalse(transicion.es_fallback)
        self.assertEqual(transicion.resultado_aprobacion, "RECHAZADA")

    def test_editar_transicion_reemplaza_destino(self):
        transicion = editor.crear_transicion(self.inicio, self.hito, self.actor)
        editor.editar_transicion(transicion, self.actor, etapa_destino=self.fin)
        transicion.refresh_from_db()
        self.assertEqual(transicion.etapa_destino_id, self.fin.pk)

    def test_eliminar_transicion(self):
        transicion = editor.crear_transicion(self.inicio, self.hito, self.actor)
        pk = transicion.pk
        editor.eliminar_transicion(transicion, self.actor)
        self.assertFalse(TransicionEtapa.objects.filter(pk=pk).exists())

    def test_crear_transicion_entre_versiones_distintas_falla(self):
        """No reimplementamos la regla — delegamos en
        `validar_integridad_transicion` vía `full_clean()`/`save()`."""
        otro_workflow = crear_workflow(self.actor, nombre="Otro workflow")
        otra_version = otro_workflow.versiones.get()
        etapa_ajena = editor.crear_etapa(otra_version, self.actor, tipo=Etapa.Tipo.HITO, nombre="Ajena")
        with self.assertRaises(ValidationError):
            editor.crear_transicion(self.inicio, etapa_ajena, self.actor)


class InmutabilidadEditorTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="inmuteditor_actor", password=CLAVE_PRUEBA)
        self.workflow, self.version, self.etapas = _crear_workflow_lineal()
        activar_version(self.workflow, self.version, actor=self.actor)
        self.version.refresh_from_db()

    def test_crear_etapa_sobre_version_activa_falla(self):
        with self.assertRaises(ValidationError):
            editor.crear_etapa(self.version, self.actor, tipo=Etapa.Tipo.HITO, nombre="No debería crearse")

    def test_editar_etapa_sobre_version_activa_falla(self):
        with self.assertRaises(ValidationError):
            editor.editar_etapa(self.etapas["tarea"], self.actor, nombre="No debería cambiar")

    def test_eliminar_etapa_sobre_version_activa_falla(self):
        with self.assertRaises(ValidationError):
            editor.eliminar_etapa(self.etapas["fin"], self.actor)

    def test_crear_transicion_sobre_version_activa_falla(self):
        with self.assertRaises(ValidationError):
            editor.crear_transicion(self.etapas["inicio"], self.etapas["fin"], self.actor)


# --- 3.UI.4 — Pruebas HTTP de apps/workflows/views.py (editor) -------------


class EditorViewsAutorizacionTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="edview_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.lector = Usuario.objects.create_user(username="edview_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)
        self.workflow = crear_workflow(self.admin, nombre="Editor HTTP")
        self.version = self.workflow.versiones.get()
        self.etapa = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.HITO, nombre="Hito")

    def test_crear_etapa_no_autorizado_devuelve_403(self):
        self.client.login(username="edview_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:crear_etapa", args=[self.version.pk]),
            {"tipo": Etapa.Tipo.HITO, "nombre": "No debería crearse"},
        )
        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(Etapa.objects.filter(nombre="No debería crearse").exists())

    def test_editar_etapa_no_autorizado_devuelve_403(self):
        self.client.login(username="edview_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:editar_etapa", args=[self.etapa.pk]),
            {f"etapa-{self.etapa.pk}-editar-nombre": "Hackeado"},
        )
        self.assertEqual(respuesta.status_code, 403)
        self.etapa.refresh_from_db()
        self.assertEqual(self.etapa.nombre, "Hito")

    def test_eliminar_etapa_no_autorizado_devuelve_403(self):
        self.client.login(username="edview_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:eliminar_etapa", args=[self.etapa.pk]))
        self.assertEqual(respuesta.status_code, 403)
        self.assertTrue(Etapa.objects.filter(pk=self.etapa.pk).exists())

    def test_configurar_etapa_no_autorizado_devuelve_403(self):
        self.client.login(username="edview_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:configurar_etapa", args=[self.etapa.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_crear_transicion_no_autorizado_devuelve_403(self):
        self.client.login(username="edview_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:crear_transicion", args=[self.etapa.pk]))
        self.assertEqual(respuesta.status_code, 403)


class EditorViewsExitosoTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="edexito_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.workflow = crear_workflow(self.admin, nombre="Editor HTTP exitoso")
        self.version = self.workflow.versiones.get()

    def test_crear_etapa_exitoso(self):
        self.client.login(username="edexito_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:crear_etapa", args=[self.version.pk]),
            {"tipo": Etapa.Tipo.HITO, "nombre": "Nueva etapa"},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(Etapa.objects.filter(version=self.version, nombre="Nueva etapa").exists())

    def test_editar_etapa_exitoso(self):
        etapa = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.HITO, nombre="Original")
        self.client.login(username="edexito_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:editar_etapa", args=[etapa.pk]),
            {f"etapa-{etapa.pk}-editar-nombre": "Editado desde HTTP"},
        )
        self.assertEqual(respuesta.status_code, 302)
        etapa.refresh_from_db()
        self.assertEqual(etapa.nombre, "Editado desde HTTP")

    def test_eliminar_etapa_exitoso(self):
        etapa = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.HITO, nombre="Para eliminar")
        self.client.login(username="edexito_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:eliminar_etapa", args=[etapa.pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(Etapa.objects.filter(pk=etapa.pk).exists())

    def test_crear_transicion_exitoso(self):
        inicio = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        fin = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.FIN, nombre="Fin")
        self.client.login(username="edexito_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:crear_transicion", args=[inicio.pk]),
            {f"etapa-{inicio.pk}-transicion-etapa_destino": fin.pk, f"etapa-{inicio.pk}-transicion-prioridad": 0},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(TransicionEtapa.objects.filter(etapa_origen=inicio, etapa_destino=fin).exists())


class VersionDetalleEditorContextoTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="edcontexto_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.workflow = crear_workflow(self.admin, nombre="Contexto editor")
        self.version = self.workflow.versiones.get()

    def test_borrador_editable_true_para_administrador(self):
        self.client.login(username="edcontexto_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[self.version.pk]))
        self.assertTrue(respuesta.context["editable"])
        self.assertIsNotNone(respuesta.context["form_crear_etapa"])

    def test_activa_editable_false_incluso_para_administrador(self):
        etapa_inicio = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        etapa_fin = editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.FIN, nombre="Fin")
        editor.crear_transicion(etapa_inicio, etapa_fin, self.admin)
        activar_version(self.workflow, self.version, actor=self.admin)
        self.version.refresh_from_db()
        self.client.login(username="edcontexto_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[self.version.pk]))
        self.assertFalse(respuesta.context["editable"])
        self.assertIsNone(respuesta.context["form_crear_etapa"])


class EditorInmutabilidadHTTPTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="edinmut_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.workflow, self.version, self.etapas = _crear_workflow_lineal()
        activar_version(self.workflow, self.version, actor=self.admin)
        self.version.refresh_from_db()

    def test_post_directo_crear_etapa_sobre_version_activa_no_persiste(self):
        self.client.login(username="edinmut_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:crear_etapa", args=[self.version.pk]),
            {"tipo": Etapa.Tipo.HITO, "nombre": "No debería crearse"},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(Etapa.objects.filter(nombre="No debería crearse").exists())

    def test_post_directo_eliminar_etapa_sobre_version_activa_no_borra(self):
        self.client.login(username="edinmut_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:eliminar_etapa", args=[self.etapas["fin"].pk]))
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(Etapa.objects.filter(pk=self.etapas["fin"].pk).exists())


class EditorTiposReservadosHTTPTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="edreservado_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.workflow = crear_workflow(self.admin, nombre="Tipos reservados")
        self.version = self.workflow.versiones.get()

    def test_ticket_no_puede_crearse_desde_formulario(self):
        self.client.login(username="edreservado_admin", password=CLAVE_PRUEBA)
        self.client.post(
            reverse("workflows:crear_etapa", args=[self.version.pk]),
            {"tipo": Etapa.Tipo.TICKET, "nombre": "No debería crearse"},
        )
        self.assertFalse(Etapa.objects.filter(nombre="No debería crearse").exists())

    def test_gaceta_no_puede_crearse_desde_formulario(self):
        self.client.login(username="edreservado_admin", password=CLAVE_PRUEBA)
        self.client.post(
            reverse("workflows:crear_etapa", args=[self.version.pk]),
            {"tipo": Etapa.Tipo.GACETA, "nombre": "No debería crearse"},
        )
        self.assertFalse(Etapa.objects.filter(nombre="No debería crearse").exists())

    def test_post_manual_saltando_el_form_tambien_es_rechazado(self):
        """Aunque alguien invoque la operación de dominio directamente con
        un `tipo` restringido (saltándose el Form/la vista por completo),
        `editor.crear_etapa` sigue siendo quien realmente lo bloquea — la
        protección no depende únicamente del Form/HTML."""
        with self.assertRaises(ValidationError):
            editor.crear_etapa(self.version, self.admin, tipo=Etapa.Tipo.TICKET, nombre="Nunca debería persistir")
        self.assertEqual(self.version.etapas.count(), 0)


# ----------------------------------------------------------------------------
# 3.UI.3 — Workflows gestión. Dos bloques: (1) las nuevas operaciones de
# dominio `crear_workflow`/`editar_workflow` (GAP cerrado, ver docstring de
# `crear_workflow` en `versionamiento.py`); (2) pruebas HTTP de
# `apps/workflows/views.py`, mismo criterio de no duplicar exhaustivamente
# lo ya probado en `VersionamientoTests`/`AutorizacionTests` — aquí se
# verifica la integración HTTP (autorización server-side, PRG, que la
# vista invoque la API pública sin reimplementarla).
# ----------------------------------------------------------------------------


class CrearEditarWorkflowTests(TestCase):
    def setUp(self):
        self.actor = Usuario.objects.create_user(username="crearworkflow_actor", password=CLAVE_PRUEBA)

    def test_crear_workflow_crea_v1_borrador_vacia(self):
        workflow = crear_workflow(self.actor, nombre="Nuevo", descripcion="Descripción")
        self.assertEqual(workflow.nombre, "Nuevo")
        self.assertEqual(workflow.descripcion, "Descripción")
        self.assertEqual(workflow.versiones.count(), 1)
        version = workflow.versiones.get()
        self.assertEqual(version.numero, 1)
        self.assertEqual(version.estado, WorkflowVersion.Estado.BORRADOR)
        self.assertEqual(version.etapas.count(), 0)
        self.assertIsNone(workflow.version_activa)

    def test_crear_workflow_es_atomico_si_falla_la_creacion_de_v1(self):
        """Simula un fallo dentro de `crear_nueva_version` (llamada interna
        de `crear_workflow`) para confirmar que el `Workflow` tampoco
        persiste — ambas altas ocurren en una sola transacción."""
        with patch("apps.workflows.versionamiento.crear_nueva_version", side_effect=RuntimeError("fallo simulado")):
            with self.assertRaises(RuntimeError):
                crear_workflow(self.actor, nombre="Debe revertirse")
        self.assertFalse(Workflow.objects.filter(nombre="Debe revertirse").exists())

    def test_crear_workflow_registra_auditoria(self):
        workflow = crear_workflow(self.actor, nombre="Auditado")
        auditorias = _auditorias_de(workflow)
        self.assertEqual(auditorias.filter(accion=RegistroAuditoria.Accion.CREAR).count(), 1)
        registro = auditorias.get(accion=RegistroAuditoria.Accion.CREAR)
        self.assertEqual(registro.usuario_id, self.actor.id)
        self.assertIsNone(registro.datos_anteriores)

    def test_editar_workflow_nombre_y_descripcion(self):
        workflow = crear_workflow(self.actor, nombre="Original", descripcion="Antes")
        editar_workflow(workflow, self.actor, nombre="Editado", descripcion="Después")
        workflow.refresh_from_db()
        self.assertEqual(workflow.nombre, "Editado")
        self.assertEqual(workflow.descripcion, "Después")

    def test_editar_workflow_registra_auditoria(self):
        workflow = crear_workflow(self.actor, nombre="Original")
        editar_workflow(workflow, self.actor, nombre="Editado")
        auditorias = _auditorias_de(workflow)
        self.assertEqual(auditorias.filter(accion=RegistroAuditoria.Accion.ACTUALIZAR).count(), 1)

    def test_editar_workflow_no_toca_version_ni_etapas(self):
        """Editar metadatos del contenedor no constituye una nueva versión
        — mismo criterio ya verificado para clonación en `ClonacionTests`."""
        workflow, version, etapas = _crear_workflow_lineal()
        version_pk = version.pk
        cantidad_etapas = version.etapas.count()
        editar_workflow(workflow, self.actor, nombre="Renombrado")
        workflow.refresh_from_db()
        self.assertEqual(workflow.versiones.count(), 1)
        self.assertEqual(workflow.versiones.get().pk, version_pk)
        self.assertEqual(workflow.versiones.get().etapas.count(), cantidad_etapas)


class WorkflowsListaViewTests(TestCase):
    def setUp(self):
        self.lector = Usuario.objects.create_user(username="wf_lista_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)
        self.workflow = crear_workflow(self.lector, nombre="Listado 1")

    def test_requiere_login(self):
        respuesta = self.client.get(reverse("workflows:lista"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.url)

    def test_requiere_permiso_consultar(self):
        sin_permiso = Usuario.objects.create_user(username="wf_lista_sinpermiso", password=CLAVE_PRUEBA)
        self.client.login(username="wf_lista_sinpermiso", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:lista"))
        self.assertEqual(respuesta.status_code, 403)

    def test_lista_renderiza_para_lector(self):
        self.client.login(username="wf_lista_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:lista"))
        self.assertEqual(respuesta.status_code, 200)
        nombres = [w.nombre for w in respuesta.context["workflows"]]
        self.assertIn("Listado 1", nombres)
        self.assertFalse(respuesta.context["puede_administrar"])


class WorkflowsCrearViewTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="wf_crear_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.lector = Usuario.objects.create_user(username="wf_crear_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)

    def test_get_requiere_administrar(self):
        self.client.login(username="wf_crear_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:crear"))
        self.assertEqual(respuesta.status_code, 403)

    def test_crear_exitoso(self):
        self.client.login(username="wf_crear_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:crear"), {"nombre": "Desde HTTP", "descripcion": "Creado vía UI"}
        )
        self.assertEqual(respuesta.status_code, 302)
        workflow = Workflow.objects.get(nombre="Desde HTTP")
        self.assertEqual(workflow.versiones.count(), 1)
        self.assertRedirects(respuesta, reverse("workflows:detalle", args=[workflow.pk]))

    def test_crear_no_autorizado_devuelve_403(self):
        self.client.login(username="wf_crear_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:crear"), {"nombre": "No debería crearse"})
        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(Workflow.objects.filter(nombre="No debería crearse").exists())

    def test_formulario_invalido_reafirma_pagina_con_errores(self):
        self.client.login(username="wf_crear_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:crear"), {"nombre": ""})
        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.context["form"].errors)
        self.assertEqual(Workflow.objects.count(), 0)


class WorkflowsDetalleEditarViewTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="wf_det_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.lector = Usuario.objects.create_user(username="wf_det_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)
        self.workflow = crear_workflow(self.admin, nombre="Detalle 1")

    def test_detalle_visible_para_lector_sin_form_de_edicion(self):
        self.client.login(username="wf_det_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:detalle", args=[self.workflow.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(respuesta.context["form"])
        self.assertFalse(respuesta.context["puede_administrar"])
        self.assertEqual(list(respuesta.context["versiones"]), list(self.workflow.versiones.all()))

    def test_detalle_no_autorizado_sin_ningun_permiso_devuelve_403(self):
        sin_permiso = Usuario.objects.create_user(username="wf_det_sinpermiso", password=CLAVE_PRUEBA)
        self.client.login(username="wf_det_sinpermiso", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:detalle", args=[self.workflow.pk]))
        self.assertEqual(respuesta.status_code, 403)

    def test_editar_exitoso(self):
        self.client.login(username="wf_det_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(
            reverse("workflows:editar", args=[self.workflow.pk]), {"nombre": "Renombrado", "descripcion": "x"}
        )
        self.assertEqual(respuesta.status_code, 302)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.nombre, "Renombrado")

    def test_editar_no_autorizado_devuelve_403(self):
        self.client.login(username="wf_det_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:editar", args=[self.workflow.pk]), {"nombre": "Hackeado"})
        self.assertEqual(respuesta.status_code, 403)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.nombre, "Detalle 1")


class WorkflowsCrearVersionViewTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="wf_ver_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.lector = Usuario.objects.create_user(username="wf_ver_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)
        self.workflow, self.version1, self.etapas = _crear_workflow_lineal()
        activar_version(self.workflow, self.version1, actor=self.admin)
        self.workflow.refresh_from_db()

    def test_crear_version_clona_version_activa(self):
        """Mismo comportamiento de `clonar_desde` que ya define el
        dominio (por defecto clona `version_activa`) — la vista no decide
        una política distinta ni pasa `clonar_desde` explícito."""
        self.client.login(username="wf_ver_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:crear_version", args=[self.workflow.pk]))
        self.assertEqual(respuesta.status_code, 302)
        version2 = self.workflow.versiones.get(numero=2)
        self.assertEqual(version2.estado, WorkflowVersion.Estado.BORRADOR)
        self.assertEqual(version2.etapas.count(), self.version1.etapas.count())
        self.assertRedirects(respuesta, reverse("workflows:version_detalle", args=[version2.pk]))

    def test_crear_version_no_autorizado_devuelve_403(self):
        self.client.login(username="wf_ver_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:crear_version", args=[self.workflow.pk]))
        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(self.workflow.versiones.count(), 1)


class WorkflowsVersionDetalleViewTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="wf_vd_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.lector = Usuario.objects.create_user(username="wf_vd_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)
        self.workflow, self.version, self.etapas = _crear_workflow_lineal()

    def test_borrador_muestra_etapas_transiciones_y_sin_errores(self):
        self.client.login(username="wf_vd_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[self.version.pk]))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["errores"], [])
        self.assertContains(respuesta, "Inicio")
        self.assertContains(respuesta, "Fin")

    def test_version_invalida_presenta_errores_reales_del_backend(self):
        workflow = Workflow.objects.create(nombre="Incompleto para detalle")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        self.client.login(username="wf_vd_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[version.pk]))
        self.assertEqual(respuesta.context["errores"], validar_estructura(version))
        self.assertTrue(respuesta.context["errores"])
        self.assertContains(respuesta, "FIN")

    def test_activa_no_calcula_errores_ni_expone_controles_de_edicion(self):
        activar_version(self.workflow, self.version, actor=self.admin)
        self.version.refresh_from_db()
        self.client.login(username="wf_vd_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[self.version.pk]))
        self.assertEqual(respuesta.context["errores"], [])
        self.assertNotContains(respuesta, reverse("workflows:activar_version", args=[self.version.pk]))

    def test_version_detalle_no_autorizado_devuelve_403(self):
        sin_permiso = Usuario.objects.create_user(username="wf_vd_sinpermiso", password=CLAVE_PRUEBA)
        self.client.login(username="wf_vd_sinpermiso", password=CLAVE_PRUEBA)
        respuesta = self.client.get(reverse("workflows:version_detalle", args=[self.version.pk]))
        self.assertEqual(respuesta.status_code, 403)


class WorkflowsActivarVersionViewTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.create_user(username="wf_act_admin", password=CLAVE_PRUEBA)
        _otorgar_workflows_administrar(self.admin)
        self.lector = Usuario.objects.create_user(username="wf_act_lector", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(self.lector)

    def test_activar_version_valida_exitoso(self):
        workflow, version, etapas = _crear_workflow_lineal()
        self.client.login(username="wf_act_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:activar_version", args=[version.pk]))
        self.assertEqual(respuesta.status_code, 302)
        version.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.ACTIVA)

    def test_activar_version_invalida_no_activa_y_presenta_errores(self):
        workflow = Workflow.objects.create(nombre="Incompleto")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        Etapa.objects.create(version=version, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        self.client.login(username="wf_act_admin", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:activar_version", args=[version.pk]), follow=True)
        version.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.BORRADOR)
        mensajes = [str(m) for m in respuesta.context["messages"]]
        self.assertTrue(any("FIN" in m for m in mensajes))

    def test_activar_version_no_autorizado_devuelve_403(self):
        workflow, version, etapas = _crear_workflow_lineal()
        self.client.login(username="wf_act_lector", password=CLAVE_PRUEBA)
        respuesta = self.client.post(reverse("workflows:activar_version", args=[version.pk]))
        self.assertEqual(respuesta.status_code, 403)
        version.refresh_from_db()
        self.assertEqual(version.estado, WorkflowVersion.Estado.BORRADOR)


class NavegacionWorkflowsApuntaALaVistaFuncionalTests(TestCase):
    """3.UI.3, punto 8: el ítem "Workflows" del dock deja de apuntar al
    Admin y pasa a la vista funcional nueva."""

    def test_item_workflows_apunta_a_la_vista_funcional(self):
        from apps.core.navegacion import elementos_navegacion

        usuario = Usuario.objects.create_user(username="wf_nav", password=CLAVE_PRUEBA)
        _otorgar_workflows_consultar(usuario)
        elementos = elementos_navegacion(usuario)
        # Desde D1 los flujos viven dentro del Diseñador (entrada unificada).
        item = next(e for e in elementos if e["etiqueta"] == "Diseñador")
        self.assertEqual(item["url_name"], "core:disenador")
        self.assertIn("workflows", item["namespaces"])


class _EscenarioActoresEjecucion:
    """4.3: configuración compartida para integración y concurrencia."""

    def setUp(self):
        from apps.catalogo.models import Categoria, Formulario, Servicio
        from apps.catalogo.versionamiento import crear_nueva_version as crear_entrada, activar_version as activar_entrada

        self.solicitante = Usuario.objects.create_user("solicitante43")
        self.responsable = Usuario.objects.create_user("responsable43")
        self.nuevo = Usuario.objects.create_user("nuevo43")
        self.equipo = Equipo.objects.create(nombre="Equipo43")
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.responsable)
        MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.nuevo)
        permiso, _ = Permiso.objects.get_or_create(codigo="tickets.atender", defaults={"nombre": "Atender"})
        rol = RolFuncional.objects.create(nombre="Atención43")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(usuario=self.responsable, rol=rol, tipo_alcance="GLOBAL")
        formulario = Formulario.objects.create(nombre="Entrada43")
        entrada = crear_entrada(formulario, self.responsable)
        activar_entrada(formulario, entrada, self.responsable)
        self.servicio = Servicio.objects.create(
            nombre="Servicio43", categoria=Categoria.objects.create(nombre="General43"),
            formulario=formulario, activo=True, alcance_visibilidad="PUBLICO_INTERNO",
        )

    def _base(self):
        workflow = Workflow.objects.create(nombre="Ejecución43")
        version = WorkflowVersion.objects.create(workflow=workflow, numero=1)
        inicio = editor.crear_etapa(version, self.responsable, tipo="INICIO", nombre="Inicio")
        fin = editor.crear_etapa(version, self.responsable, tipo="FIN", nombre="Fin")
        self.servicio.workflow = workflow
        self.servicio.save()
        return workflow, version, inicio, fin

    def _tareas(self, *actores):
        workflow, version, anterior, fin = self._base()
        for tipo, referencia in actores:
            etapa = editor.crear_etapa(version, self.responsable, tipo="TAREA", nombre=tipo, descripcion="Instrucciones")
            editor.configurar_etapa_tarea(
                etapa, self.responsable, tipo_responsable=tipo,
                usuario_responsable=referencia if tipo == "USUARIO" else None,
                equipo_responsable=referencia if tipo == "EQUIPO" else None,
            )
            editor.crear_transicion(anterior, etapa, self.responsable)
            anterior = etapa
        editor.crear_transicion(anterior, fin, self.responsable)
        activar_version(workflow, version, self.responsable)
        return workflow, version

    def _radicar(self):
        from apps.tickets.operaciones import crear_borrador, radicar_ticket

        ticket = crear_borrador(self.solicitante, self.servicio)
        return radicar_ticket(ticket, self.solicitante)

    def _tarea_vigente(self, ticket):
        return TareaWorkflow.objects.filter(
            instancia_etapa__instancia_workflow_id=ticket.instancia_workflow_id,
        ).order_by("-instancia_etapa__orden").first().tarea

    def _aprobaciones(self, participantes=None):
        workflow, version, inicio, fin = self._base()
        actividad = editor.crear_etapa(version, self.responsable, tipo="TAREA", nombre="Producir")
        editor.configurar_etapa_tarea(actividad, self.responsable, tipo_responsable="SOLICITANTE")
        primera = editor.crear_etapa(version, self.responsable, tipo="APROBACION", nombre="Revisión interna")
        segunda = editor.crear_etapa(version, self.responsable, tipo="APROBACION", nombre="Otra revisión")
        editor.configurar_etapa_aprobacion(
            primera, self.responsable, modo="SECUENCIAL",
            participantes=participantes or [("RESPONSABLE_TICKET", None)],
        )
        editor.configurar_etapa_aprobacion(
            segunda, self.responsable, modo="SECUENCIAL", participantes=[("SOLICITANTE", None)],
        )
        editor.crear_transicion(inicio, actividad, self.responsable)
        editor.crear_transicion(actividad, primera, self.responsable)
        for etapa, destino in ((primera, segunda), (segunda, fin)):
            for resultado in ("APROBADA", "DEVUELTA", "RECHAZADA"):
                editor.crear_transicion(
                    etapa, destino if resultado == "APROBADA" else actividad,
                    self.responsable, resultado_aprobacion=resultado,
                )
        activar_version(workflow, version, self.responsable)
        return workflow, version

    def _esquema_vigente(self, ticket):
        return EsquemaAprobacionWorkflow.objects.filter(
            instancia_etapa__instancia_workflow_id=ticket.instancia_workflow_id,
        ).order_by("-instancia_etapa__orden").first().esquema

class ActoresEjecucionTests(_EscenarioActoresEjecucion, TestCase):
    def test_tarea_solicitante_usuario_y_equipo_sin_roles_nuevos(self):
        for tipo, referencia, usuario, equipo in (
            ("SOLICITANTE", None, self.solicitante, None),
            ("USUARIO", self.responsable, self.responsable, None),
            ("EQUIPO", self.equipo, None, self.equipo),
        ):
            with self.subTest(tipo=tipo):
                self._tareas((tipo, referencia))
                ticket = self._radicar()
                tarea = self._tarea_vigente(ticket)
                self.assertEqual(tarea.usuario_responsable, usuario)
                self.assertEqual(tarea.equipo_responsable, equipo)
                self.assertEqual(tarea.descripcion, "Instrucciones")
                if equipo:
                    from apps.tareas.operaciones import tomar_tarea
                    tarea = tomar_tarea(tarea, self.nuevo)
                    usuario = self.nuevo
                completar_tarea_workflow(tarea, usuario)
                ticket.instancia_workflow.refresh_from_db()
                self.assertEqual(ticket.instancia_workflow.estado, "COMPLETADA")
                ticket.refresh_from_db()
                self.assertEqual(ticket.estado, "RADICADO")

    def test_responsable_se_resuelve_al_entrar_no_al_radicar_ni_desde_contexto_obsoleto(self):
        from apps.tickets.operaciones import asignar_ticket, reasignar_ticket

        self._tareas(("SOLICITANTE", None), ("RESPONSABLE_TICKET", None))
        ticket = self._radicar()
        primera = self._tarea_vigente(ticket)
        asignar_ticket(ticket, self.responsable, usuario=self.responsable, equipo=self.equipo)
        reasignar_ticket(ticket, self.responsable, usuario=self.nuevo)
        # Una vez vinculada, manda la FK canónica, no el JSON de arranque.
        instancia = ticket.instancia_workflow
        instancia.contexto["datos_iniciales"]["ticket_id"] = -1
        instancia.save(update_fields=["contexto"])
        completar_tarea_workflow(primera, self.solicitante)
        segunda = self._tarea_vigente(ticket)
        self.assertEqual(segunda.usuario_responsable, self.nuevo)
        self.assertIsNone(segunda.equipo_responsable_id)
        reasignar_ticket(ticket, self.responsable, usuario=self.responsable)
        segunda.refresh_from_db()
        self.assertEqual(segunda.usuario_responsable, self.nuevo)  # Asignación propia de la Tarea.

    def test_responsable_ausente_en_inicio_revierte_radicacion_y_no_infiere_equipo(self):
        from apps.tickets.operaciones import crear_borrador, radicar_ticket

        self._tareas(("RESPONSABLE_TICKET", None))
        ticket = crear_borrador(self.solicitante, self.servicio)
        ticket.equipo_responsable = self.equipo
        ticket.save()
        eventos = RegistroAuditoria.objects.count()
        with self.assertRaises(ValidationError):
            radicar_ticket(ticket, self.solicitante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, "BORRADOR")
        self.assertIsNone(ticket.radicado)
        self.assertIsNone(ticket.instancia_workflow_id)
        self.assertFalse(Tarea.objects.exists())
        self.assertFalse(InstanciaWorkflow.objects.exists())
        self.assertEqual(RegistroAuditoria.objects.count(), eventos)

    def test_actor_dinamico_sin_ticket_no_se_convierte_en_asignacion_libre(self):
        workflow, _ = self._tareas(("SOLICITANTE", None))
        instancia = iniciar_workflow(workflow, actor=self.responsable)
        self.assertEqual(instancia.estado, "ERROR")
        self.assertFalse(Tarea.objects.exists())
        self.assertIn("Ticket", instancia.ejecuciones_etapa.order_by("-orden").first().error["mensaje"])

    def test_devolucion_crea_nueva_tarea_y_varias_aprobaciones_secuenciales(self):
        from apps.tickets.operaciones import asignar_ticket

        self._aprobaciones()
        ticket = self._radicar()
        asignar_ticket(ticket, self.responsable, usuario=self.responsable)
        primera_tarea = self._tarea_vigente(ticket)
        completar_tarea_workflow(primera_tarea, self.solicitante)
        primera_aprobacion = self._esquema_vigente(ticket).participaciones.get()
        self.assertEqual(primera_aprobacion.aprobador_usuario, self.responsable)
        resolver_aprobacion_workflow(primera_aprobacion, self.responsable, decision="DEVUELTA", observacion="Ajustar")
        nueva_tarea = self._tarea_vigente(ticket)
        self.assertNotEqual(nueva_tarea.pk, primera_tarea.pk)
        self.assertEqual(nueva_tarea.estado, "PENDIENTE")
        completar_tarea_workflow(nueva_tarea, self.solicitante)
        nueva_aprobacion = self._esquema_vigente(ticket).participaciones.get()
        self.assertNotEqual(nueva_aprobacion.pk, primera_aprobacion.pk)
        resolver_aprobacion_workflow(nueva_aprobacion, self.responsable, decision="APROBADA")
        siguiente = self._esquema_vigente(ticket).participaciones.get()
        self.assertEqual(siguiente.aprobador_usuario, self.solicitante)
        resolver_aprobacion_workflow(siguiente, self.solicitante, decision="APROBADA")
        ticket.instancia_workflow.refresh_from_db()
        self.assertEqual(ticket.instancia_workflow.estado, "COMPLETADA")
        primera_aprobacion.refresh_from_db()
        self.assertEqual(primera_aprobacion.estado, "DEVUELTA")

    def test_aprobacion_equipo_es_una_participacion_no_unanimidad_de_miembros(self):
        self._aprobaciones([("EQUIPO", self.equipo)])
        ticket = self._radicar()
        completar_tarea_workflow(self._tarea_vigente(ticket), self.solicitante)
        esquema = self._esquema_vigente(ticket)
        self.assertEqual(esquema.participaciones.count(), 1)
        aprobacion = esquema.participaciones.get()
        self.assertEqual(aprobacion.aprobador_equipo, self.equipo)
        resolver_aprobacion_workflow(aprobacion, self.nuevo, decision="APROBADA")
        esquema.refresh_from_db()
        self.assertEqual(esquema.resultado, "APROBADA")
        self.assertNotEqual(self._esquema_vigente(ticket).pk, esquema.pk)

    def test_participantes_secuenciales_mezclan_usuario_y_solicitante(self):
        self._aprobaciones([("USUARIO", self.responsable), ("SOLICITANTE", None)])
        ticket = self._radicar()
        completar_tarea_workflow(self._tarea_vigente(ticket), self.solicitante)
        esquema = self._esquema_vigente(ticket)
        primera, segunda = list(esquema.participaciones.order_by("orden"))
        resolver_aprobacion_workflow(primera, self.responsable, decision="APROBADA")
        self.assertEqual(self._esquema_vigente(ticket).pk, esquema.pk)
        resolver_aprobacion_workflow(segunda, self.solicitante, decision="APROBADA")
        self.assertNotEqual(self._esquema_vigente(ticket).pk, esquema.pk)

    def test_clonacion_conserva_actores_sin_modificar_configuracion_activa(self):
        workflow, version = self._aprobaciones()
        copia = crear_nueva_version(workflow, self.responsable)
        self.assertEqual(copia.etapas.get(tipo="TAREA").configuracion_tarea.tipo_responsable, "SOLICITANTE")
        participante = copia.etapas.get(nombre="Revisión interna").configuracion_aprobacion.participantes.get()
        self.assertEqual(participante.tipo_aprobador, "RESPONSABLE_TICKET")
        editor.configurar_etapa_aprobacion(
            copia.etapas.get(nombre="Revisión interna"), self.responsable,
            modo="SECUENCIAL", participantes=[("USUARIO", self.nuevo)],
        )
        original = version.etapas.get(nombre="Revisión interna").configuracion_aprobacion.participantes.get()
        self.assertEqual(original.tipo_aprobador, "RESPONSABLE_TICKET")
        self.assertNotEqual(original.pk, participante.pk)

    def test_entregable_satisfecho_no_completa_actividad_aprueba_entrega_ni_cierra(self):
        from apps.catalogo.models import DefinicionEntregable
        from apps.tickets.entregables import registrar_resultado_entregable
        from apps.tickets.operaciones import asignar_ticket

        self._aprobaciones()
        DefinicionEntregable.objects.create(servicio=self.servicio, nombre="Resultado", tipo="TEXTO", obligatorio=True)
        ticket = self._radicar()
        asignar_ticket(ticket, self.responsable, usuario=self.responsable)
        entregable = registrar_resultado_entregable(ticket.entregables.get(), self.responsable, "Listo")
        self.assertTrue(entregable.satisfecho)
        self.assertEqual(self._tarea_vigente(ticket).estado, "PENDIENTE")
        self.assertFalse(EsquemaAprobacion.objects.exists())
        completar_tarea_workflow(self._tarea_vigente(ticket), self.solicitante)
        esquema = self._esquema_vigente(ticket)
        registrar_resultado_entregable(entregable, self.responsable, "Actualizado")
        esquema.refresh_from_db()
        self.assertIsNone(esquema.resultado)
        self.assertEqual(esquema.participaciones.get().estado, "PENDIENTE")
        ticket.refresh_from_db()
        self.assertEqual(ticket.estado, "EN_ATENCION")

    def test_formularios_tecnicos_admiten_actores_dinamicos(self):
        from apps.workflows.forms import ConfiguracionTareaForm, ParticipanteAprobacionForm

        for tipo in ("SOLICITANTE", "RESPONSABLE_TICKET"):
            with self.subTest(tipo=tipo):
                tarea = ConfiguracionTareaForm({"tipo_responsable": tipo})
                participante = ParticipanteAprobacionForm({"tipo_aprobador": tipo})
                self.assertTrue(tarea.is_valid(), tarea.errors)
                self.assertTrue(participante.is_valid(), participante.errors)


class ConcurrenciaActorTicketTests(_EscenarioActoresEjecucion, TransactionTestCase):
    def test_resolucion_de_actor_espera_reasignacion_y_lee_nuevo_responsable(self):
        from apps.tickets.models import Ticket
        from apps.tickets.operaciones import asignar_ticket, reasignar_ticket

        self._tareas(("SOLICITANTE", None), ("RESPONSABLE_TICKET", None))
        ticket = self._radicar()
        asignar_ticket(ticket, self.responsable, usuario=self.responsable, equipo=self.equipo)
        primera = self._tarea_vigente(ticket)
        intentando = threading.Event()
        resultados = []

        def observar(execute, sql, params, many, context):
            if '"tickets_ticket"' in sql and "FOR UPDATE" in sql:
                intentando.set()
            return execute(sql, params, many, context)

        def completar():
            try:
                with connection.execute_wrapper(observar):
                    completar_tarea_workflow(primera, self.solicitante)
                resultados.append("completada")
            except Exception as exc:
                resultados.append(exc)
            finally:
                connection.close()

        hilo = threading.Thread(target=completar, daemon=True)
        try:
            with transaction.atomic():
                Ticket.objects.select_for_update().get(pk=ticket.pk)
                hilo.start()
                self.assertTrue(intentando.wait(timeout=10))
                reasignar_ticket(ticket, self.responsable, usuario=self.nuevo)
        finally:
            hilo.join(timeout=15)
        self.assertFalse(hilo.is_alive())
        self.assertEqual(resultados, ["completada"])
        self.assertEqual(self._tarea_vigente(ticket).usuario_responsable, self.nuevo)


# ---------------------------------------------------------------------------
# 4.B0 — variables y resultados del motor
# ---------------------------------------------------------------------------


class EvaluacionTipadaTests(TestCase):
    """`contexto.evaluar_operador`: tipos lógicos e INEXISTENTE ≠ NULL."""

    def _ev(self, operador, actual, valor="x"):
        from apps.workflows.contexto import evaluar_operador

        return evaluar_operador(operador, actual, valor)

    def test_inexistente_es_un_singleton_falso_que_no_es_none(self):
        from apps.workflows.contexto import INEXISTENTE, _Inexistente

        self.assertIs(INEXISTENTE, _Inexistente())
        self.assertFalse(INEXISTENTE)
        self.assertIsNot(INEXISTENTE, None)

    def test_ninguna_condicion_se_cumple_con_variable_inexistente(self):
        from apps.workflows.contexto import INEXISTENTE, OPERADORES

        for operador in OPERADORES:
            self.assertFalse(self._ev(operador, INEXISTENTE, "x"), operador)
            self.assertFalse(self._ev(operador, INEXISTENTE, "1"), operador)

    def test_variable_existente_con_null_se_distingue_de_inexistente(self):
        self.assertTrue(self._ev("ESTA_VACIO", None))
        self.assertFalse(self._ev("NO_ESTA_VACIO", None))
        self.assertFalse(self._ev("IGUAL_A", None, "x"))
        self.assertTrue(self._ev("DISTINTO_DE", None, "x"))
        self.assertTrue(self._ev("NO_CONTIENE", None, "x"))
        self.assertFalse(self._ev("MAYOR_QUE", None, "1"))
        self.assertFalse(self._ev("MENOR_QUE", None, "1"))

    def test_numeros_se_comparan_como_numeros_no_como_texto(self):
        from decimal import Decimal

        self.assertTrue(self._ev("MAYOR_QUE", 10, "2"))
        self.assertTrue(self._ev("MENOR_QUE", 2, "10"))
        self.assertTrue(self._ev("MAYOR_QUE", Decimal("10.50"), "9"))
        self.assertTrue(self._ev("IGUAL_A", 2.5, "2.5"))
        self.assertTrue(self._ev("IGUAL_A", 5, "5.0"))
        self.assertTrue(self._ev("IGUAL_A", Decimal("5.00"), "5"))
        self.assertFalse(self._ev("MAYOR_QUE", Decimal("1.5"), "1.5"))

    def test_texto_de_la_transicion_no_numerico_no_cumple_salvo_distinto(self):
        self.assertFalse(self._ev("IGUAL_A", 5, "abc"))
        self.assertFalse(self._ev("MAYOR_QUE", 5, "abc"))
        self.assertTrue(self._ev("DISTINTO_DE", 5, "abc"))
        self.assertFalse(self._ev("MAYOR_QUE", 5, ""))

    def test_numeros_no_se_confunden_con_booleanos(self):
        self.assertFalse(self._ev("IGUAL_A", 1, "true"))
        self.assertTrue(self._ev("IGUAL_A", True, "1"))

    def test_booleanos_se_interpretan_con_el_texto_de_la_transicion(self):
        for texto in ("true", "True", "1", "sí", "Si", "verdadero"):
            self.assertTrue(self._ev("IGUAL_A", True, texto), texto)
            self.assertFalse(self._ev("IGUAL_A", False, texto), texto)
        for texto in ("false", "False", "0", "no", "falso"):
            self.assertTrue(self._ev("IGUAL_A", False, texto), texto)
            self.assertFalse(self._ev("IGUAL_A", True, texto), texto)

    def test_booleano_con_texto_ininterpretable_solo_cumple_distinto(self):
        self.assertFalse(self._ev("IGUAL_A", False, "abc"))
        self.assertTrue(self._ev("DISTINTO_DE", False, "abc"))

    def test_false_y_cero_no_estan_vacios(self):
        self.assertFalse(self._ev("ESTA_VACIO", False))
        self.assertFalse(self._ev("ESTA_VACIO", 0))
        self.assertTrue(self._ev("NO_ESTA_VACIO", 0))
        self.assertTrue(self._ev("ESTA_VACIO", ""))
        self.assertTrue(self._ev("ESTA_VACIO", []))

    def test_texto_y_listas_conservan_su_semantica(self):
        self.assertTrue(self._ev("IGUAL_A", "Alta", "Alta"))
        self.assertFalse(self._ev("IGUAL_A", "Alta", "alta"))
        self.assertTrue(self._ev("CONTIENE", "Prioridad alta", "alta"))
        self.assertTrue(self._ev("CONTIENE", ["a", "b"], "a"))
        self.assertTrue(self._ev("NO_CONTIENE", ["a", "b"], "c"))

    def test_fechas_y_fechas_hora(self):
        from datetime import date, datetime

        from django.utils import timezone

        dia = date(2026, 5, 1)
        self.assertTrue(self._ev("IGUAL_A", dia, "2026-05-01"))
        self.assertTrue(self._ev("MAYOR_QUE", dia, "2026-04-30"))
        self.assertTrue(self._ev("MENOR_QUE", dia, "2026-12-31"))
        self.assertFalse(self._ev("MAYOR_QUE", dia, "no es fecha"))
        self.assertFalse(self._ev("MAYOR_QUE", dia, "2026-13-45"))

        momento = timezone.make_aware(datetime(2026, 5, 1, 10, 30))
        self.assertTrue(self._ev("MAYOR_QUE", momento, "2026-04-30"))
        self.assertTrue(self._ev("MENOR_QUE", momento, "2026-05-02T00:00:00"))
        self.assertFalse(self._ev("MAYOR_QUE", momento, "2026-05-02"))

    def test_valores_incompatibles_no_son_un_error(self):
        self.assertFalse(self._ev("CONTIENE", 5, "5"))
        self.assertFalse(self._ev("NO_CONTIENE", 5, "5"))
        self.assertFalse(self._ev("MAYOR_QUE", 5, "NaN"))


class ContextoResultadosBloqueTests(TestCase):
    def _contexto(self):
        from apps.workflows.contexto import construir_contexto_inicial

        return construir_contexto_inicial({"ticket_id": 7})

    def test_el_contexto_inicial_incluye_resultados_de_bloques(self):
        self.assertEqual(self._contexto()["resultados_bloques"], {})

    def test_publicar_y_leer_un_resultado(self):
        from apps.workflows.contexto import leer_resultado_bloque, publicar_resultado_bloque

        contexto = self._contexto()
        publicar_resultado_bloque(contexto, "aprobaciones", "aprobacion_jefe", {"resultado": "APROBADA"})
        self.assertEqual(leer_resultado_bloque(contexto, "aprobaciones", "aprobacion_jefe", "resultado"), "APROBADA")

    def test_publicar_no_pierde_otras_variables_ni_otros_bloques(self):
        from apps.workflows.contexto import leer_resultado_bloque, publicar_resultado_bloque

        contexto = self._contexto()
        contexto["variables"]["monto"] = 5
        publicar_resultado_bloque(contexto, "aprobaciones", "uno", {"resultado": "APROBADA"})
        publicar_resultado_bloque(contexto, "aprobaciones", "dos", {"resultado": "RECHAZADA"})
        self.assertEqual(contexto["variables"], {"monto": 5})
        self.assertEqual(contexto["datos_iniciales"], {"ticket_id": 7})
        self.assertEqual(leer_resultado_bloque(contexto, "aprobaciones", "uno", "resultado"), "APROBADA")
        self.assertEqual(leer_resultado_bloque(contexto, "aprobaciones", "dos", "resultado"), "RECHAZADA")

    def test_el_resultado_actual_reemplaza_al_anterior_del_mismo_bloque(self):
        from apps.workflows.contexto import leer_resultado_bloque, publicar_resultado_bloque

        contexto = self._contexto()
        publicar_resultado_bloque(contexto, "aprobaciones", "uno", {"resultado": "DEVUELTA"})
        publicar_resultado_bloque(contexto, "aprobaciones", "uno", {"resultado": "APROBADA"})
        self.assertEqual(leer_resultado_bloque(contexto, "aprobaciones", "uno", "resultado"), "APROBADA")

    def test_leer_lo_no_publicado_es_inexistente(self):
        from apps.workflows.contexto import INEXISTENTE, leer_resultado_bloque

        contexto = self._contexto()
        self.assertIs(leer_resultado_bloque(contexto, "aprobaciones", "nadie", "resultado"), INEXISTENTE)
        self.assertIs(leer_resultado_bloque({}, "aprobaciones", "nadie", "resultado"), INEXISTENTE)

    def test_ambito_desconocido_o_bloque_sin_clave_no_se_publican(self):
        from apps.workflows.contexto import publicar_resultado_bloque

        contexto = self._contexto()
        with self.assertRaises(ValueError):
            publicar_resultado_bloque(contexto, "entregables", "uno", {"satisfecho": True})
        with self.assertRaises(ValueError):
            publicar_resultado_bloque(contexto, "aprobaciones", "", {"resultado": "APROBADA"})

    def test_el_contexto_con_resultados_sigue_siendo_json_puro(self):
        import json

        from apps.workflows.contexto import publicar_resultado_bloque

        contexto = self._contexto()
        publicar_resultado_bloque(contexto, "aprobaciones", "uno", {"resultado": "APROBADA"})
        json.dumps(contexto)

    def test_variable_plana_existente_con_null_no_es_inexistente(self):
        from apps.workflows.contexto import INEXISTENTE, resolver_variable

        contexto = self._contexto()
        contexto["variables"]["x"] = None
        self.assertIsNone(resolver_variable(contexto, "x"))
        self.assertIs(resolver_variable(contexto, "y"), INEXISTENTE)


class ResolutorVariablesSinTicketTests(TestCase):
    """Resolución de referencias que no necesitan un Ticket."""

    def _resolutor(self, contexto):
        from apps.workflows.variables import ResolutorVariables

        return ResolutorVariables(None, contexto)

    def test_variable_plana_legacy_sigue_resolviendose(self):
        contexto = {"variables": {"monto": 100, "vacio": None}}
        resolutor = self._resolutor(contexto)
        self.assertEqual(resolutor.resolver("monto"), 100)
        self.assertIsNone(resolutor.resolver("vacio"))

    def test_variable_plana_inexistente(self):
        from apps.workflows.contexto import INEXISTENTE

        self.assertIs(self._resolutor({"variables": {}}).resolver("monto"), INEXISTENTE)
        self.assertIs(self._resolutor({}).resolver("monto"), INEXISTENTE)

    def test_resultado_de_aprobacion_por_clave_de_bloque(self):
        contexto = {"resultados_bloques": {"aprobaciones": {"aprobacion_jefe": {"resultado": "APROBADA"}}}}
        resolutor = self._resolutor(contexto)
        self.assertEqual(resolutor.resolver("aprobaciones.aprobacion_jefe.resultado"), "APROBADA")

    def test_referencias_mal_formadas_o_desconocidas_son_inexistentes(self):
        from apps.workflows.contexto import INEXISTENTE

        contexto = {"resultados_bloques": {"aprobaciones": {"uno": {"resultado": "APROBADA"}}}}
        resolutor = self._resolutor(contexto)
        for nombre in (
            "aprobaciones.uno", "aprobaciones.uno.resultado.extra", "aprobaciones.otro.resultado",
            "aprobaciones.uno.otro", "ultima.resultado", "entregables.uno.satisfecho", "", "ticket.estado",
            "ticket.inexistente", "formulario.monto", "formulario", "ticket",
        ):
            self.assertIs(resolutor.resolver(nombre), INEXISTENTE, nombre)

    def test_una_variable_plana_con_nombre_punteado_sigue_funcionando(self):
        contexto = {"variables": {"a.b": 3}}
        self.assertEqual(self._resolutor(contexto).resolver("a.b"), 3)

    def test_referencias_disponibles_lista_ticket_formulario_y_aprobaciones(self):
        from types import SimpleNamespace

        from apps.workflows.variables import referencias_disponibles

        campos = [SimpleNamespace(clave="valor_estimado", etiqueta="Valor estimado"), SimpleNamespace(clave="", etiqueta="Sin clave")]
        bloques = [
            SimpleNamespace(tipo="APROBACION", clave="aprobacion_jefe", nombre="Aprobación jefe"),
            SimpleNamespace(tipo="ACTIVIDAD", clave="atender", nombre="Atender"),
        ]
        referencias = [r for r, _ in referencias_disponibles(campos=campos, bloques=bloques)]
        self.assertIn("ticket.estado", referencias)
        self.assertIn("formulario.valor_estimado", referencias)
        self.assertIn("aprobaciones.aprobacion_jefe.resultado", referencias)
        self.assertEqual(sum(1 for r in referencias if r.startswith("formulario.")), 1)
        self.assertFalse(any("atender" in r for r in referencias))


class VariablesLegacyEjecutableTests(TestCase):
    """LEGACY_EJECUTABLE (Etapa) conserva su contrato con las variables planas."""

    def setUp(self):
        self.actor = get_user_model().objects.create_user("vars_legacy", password="Clave-Segura-123")

    def test_variable_plana_numerica_alimenta_la_condicion(self):
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)
        alta = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"monto": 2000000})
        baja = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"monto": 99})
        self.assertEqual(
            alta.ejecuciones_etapa.get(etapa=etapas["condicion"]).transicion_tomada_id, etapas["condicional"].pk
        )
        self.assertEqual(
            baja.ejecuciones_etapa.get(etapa=etapas["condicion"]).transicion_tomada_id, etapas["fallback"].pk
        )

    def test_el_orden_numerico_no_es_el_orden_del_texto(self):
        """`"2000000" > "1000000"` y `"99" > "1000000"` como texto darían resultados
        distintos de los numéricos: con 99 (menor) debe tomarse el fallback."""
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)
        instancia = iniciar_workflow(workflow, actor=self.actor, datos_iniciales={"monto": 99})
        self.assertEqual(
            instancia.ejecuciones_etapa.get(etapa=etapas["condicion"]).transicion_tomada_id, etapas["fallback"].pk
        )

    def test_en_modo_legacy_las_referencias_de_ticket_y_formulario_son_inexistentes_sin_ticket(self):
        workflow, etapas = _crear_workflow_activo_con_condicion(self.actor)
        TransicionEtapa.objects.filter(pk=etapas["condicional"].pk).update(
            variable="ticket.estado", operador=TransicionEtapa.Operador.DISTINTO_DE, valor="CERRADO"
        )
        instancia = iniciar_workflow(workflow, actor=self.actor)
        # Sin Ticket vinculado la variable es inexistente: ni siquiera DISTINTO_DE coincide.
        self.assertEqual(
            instancia.ejecuciones_etapa.get(etapa=etapas["condicion"]).transicion_tomada_id, etapas["fallback"].pk
        )
