"""Configuración empresarial de ejecución — Sprint 4.3.

CU-010/CU-020: Servicio.workflow es la única asociación y las Etapas son
los propios bloques. Coordina pertenencia, permisos, borrador y creación
atómica de bloque + configuración. El editor conserva validación y auditoría.
No persiste otro grafo ni distingue un supuesto Workflow «de Studio».
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import Servicio
from apps.catalogo.operaciones import _exigir_administracion, activar_servicio
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.workflows import editor, versionamiento
from apps.workflows.actores import validar_actor
from apps.workflows.autorizacion import puede_administrar_workflows
from apps.workflows.models import Etapa, TransicionEtapa, Workflow, WorkflowVersion


TIPOS_BLOQUE = {
    "ACTIVIDAD": Etapa.Tipo.TAREA,
    "APROBACION": Etapa.Tipo.APROBACION,
    "ESPERA": Etapa.Tipo.ESPERA,
    "DECISION": Etapa.Tipo.CONDICION,
}


def _exigir_configuracion(actor):
    _exigir_administracion(actor)
    # Servicio.workflow puede ser compartido con otros Servicios y con el
    # editor técnico. Administrar catálogo no concede administrar Workflow.
    if not puede_administrar_workflows(actor):
        raise PermissionDenied("Se requiere autorización para administrar la ejecución.")


def _bloquear_version(servicio, version, actor):
    _exigir_configuracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.workflow_id is None:
        raise ValidationError("La definición no tiene una ejecución configurada.")
    # Compartido con crear_nueva_version/activar_version. Relectura bajo
    # lock: un objeto BORRADOR obsoleto no permite editar una versión activa.
    Workflow.objects.select_for_update().get(pk=servicio.workflow_id)
    version = WorkflowVersion.objects.get(pk=version.pk)
    if version.workflow_id != servicio.workflow_id:
        raise ValidationError("La versión no pertenece a la ejecución de esta definición.")
    version.exigir_editable()
    return version


def _etapa(version, bloque, *, permitir_terminales=False):
    etapa = Etapa.objects.get(pk=bloque.pk)
    if etapa.version_id != version.pk:
        raise ValidationError("El bloque no pertenece al borrador indicado.")
    if not permitir_terminales and etapa.tipo not in TIPOS_BLOQUE.values():
        raise ValidationError("Esta operación no modifica los nodos técnicos de inicio/fin.")
    return etapa


@transaction.atomic
def preparar_ejecucion(servicio, actor):
    """Obtiene un borrador o clona la activa; para un flujo nuevo crea INICIO→FIN.

    Retorna WorkflowVersion. El llamador conecta sus bloques usando las
    transiciones existentes; no se reconstruye ni reordena un grafo implícito.
    No publica ni modifica el valor histórico de Servicio.activo.
    """
    _exigir_configuracion(actor)
    servicio_original = servicio
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.workflow_id is None:
        workflow = versionamiento.crear_workflow(actor, nombre=servicio.nombre)
        servicio.workflow = workflow
        servicio.save(update_fields=["workflow", "actualizado_en"])
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores={"workflow_id": None}, datos_nuevos={"workflow_id": workflow.pk},
        )
    else:
        workflow = Workflow.objects.select_for_update().get(pk=servicio.workflow_id)
    version = workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()
    if version is None:
        version = versionamiento.crear_nueva_version(workflow, actor)
    if not version.etapas.exists():
        inicio = editor.crear_etapa(version, actor, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        fin = editor.crear_etapa(version, actor, tipo=Etapa.Tipo.FIN, nombre="Fin")
        editor.crear_transicion(inicio, fin, actor)
    servicio_original.refresh_from_db()
    return version


def _configurar_actividad(etapa, actor, *, tipo_actor="", usuario=None, equipo=None, permite_subtareas=False):
    validar_actor(tipo_actor, usuario=usuario, equipo=equipo)
    return editor.configurar_etapa_tarea(
        etapa, actor, tipo_responsable=tipo_actor, usuario_responsable=usuario,
        equipo_responsable=equipo, permite_subtareas=permite_subtareas,
    )


def _configurar_aprobacion(etapa, actor, *, modo, participantes, politica=""):
    # Mismo contrato de participantes del editor: (tipo, usuario/equipo/None).
    if not participantes:
        raise ValidationError("Debe indicar al menos un participante.")
    for tipo, referencia in participantes:
        validar_actor(
            tipo, usuario=referencia if tipo == "USUARIO" else None,
            equipo=referencia if tipo == "EQUIPO" else None,
        )
        if tipo not in ("USUARIO", "EQUIPO") and referencia is not None:
            raise ValidationError("Un actor dinámico no admite una referencia fija.")
    return editor.configurar_etapa_aprobacion(
        etapa, actor, modo=modo, politica=politica, participantes=participantes,
    )


def _configurar_decision(etapa, actor, **configuracion):
    if configuracion:
        raise ValidationError("Una decisión se configura mediante sus conexiones condicionales y fallback.")


_CONFIGURAR = {
    Etapa.Tipo.TAREA: _configurar_actividad,
    Etapa.Tipo.APROBACION: _configurar_aprobacion,
    Etapa.Tipo.ESPERA: editor.configurar_etapa_espera,
    Etapa.Tipo.CONDICION: _configurar_decision,
}


@transaction.atomic
def agregar_bloque(servicio, version, actor, *, tipo, nombre, descripcion="", **configuracion):
    """ACTIVIDAD/APROBACION/ESPERA/DECISION; alta y configuración indivisibles."""
    version = _bloquear_version(servicio, version, actor)
    if tipo not in TIPOS_BLOQUE:
        raise ValidationError("Tipo de bloque empresarial no soportado.")
    etapa = editor.crear_etapa(version, actor, tipo=TIPOS_BLOQUE[tipo], nombre=nombre, descripcion=descripcion)
    _CONFIGURAR[etapa.tipo](etapa, actor, **configuracion)
    return etapa


@transaction.atomic
def configurar_bloque(servicio, version, bloque, actor, **configuracion):
    version = _bloquear_version(servicio, version, actor)
    etapa = _etapa(version, bloque)
    return _CONFIGURAR[etapa.tipo](etapa, actor, **configuracion)


@transaction.atomic
def editar_bloque(servicio, version, bloque, actor, *, nombre, descripcion=""):
    version = _bloquear_version(servicio, version, actor)
    return editor.editar_etapa(_etapa(version, bloque), actor, nombre=nombre, descripcion=descripcion)


@transaction.atomic
def conectar_bloques(servicio, version, origen, destino, actor, *, conexion=None, **reglas):
    """Crea o redirige una conexión, con las reglas del motor vigente.

    `conexion` permite insertar un bloque redirigiendo INICIO→FIN sin
    generar una segunda salida. Las ramas de aprobación usan resultado;
    las decisiones usan variable/operador/valor/prioridad/fallback.
    """
    version = _bloquear_version(servicio, version, actor)
    origen = _etapa(version, origen, permitir_terminales=True)
    destino = _etapa(version, destino, permitir_terminales=True)
    if conexion is None:
        return editor.crear_transicion(origen, destino, actor, **reglas)
    conexion = TransicionEtapa.objects.get(pk=conexion.pk)
    if conexion.etapa_origen_id != origen.pk:
        raise ValidationError("La conexión no pertenece al bloque de origen.")
    return editor.editar_transicion(conexion, actor, etapa_destino=destino, **reglas)


@transaction.atomic
def desconectar_bloques(servicio, version, conexion, actor):
    version = _bloquear_version(servicio, version, actor)
    conexion = TransicionEtapa.objects.get(pk=conexion.pk)
    _etapa(version, conexion.etapa_origen, permitir_terminales=True)
    return editor.eliminar_transicion(conexion, actor)


@transaction.atomic
def eliminar_bloque(servicio, version, bloque, actor):
    version = _bloquear_version(servicio, version, actor)
    # Conserva la regla del editor: desconectar explícitamente antes de eliminar.
    return editor.eliminar_etapa(_etapa(version, bloque), actor)


@transaction.atomic
def publicar_ejecucion(servicio, version, actor):
    """Activa la versión y publica el catálogo en una transacción.

    Si falla el formulario, la estructura o cualquier auditoría, tampoco
    queda una nueva versión activa. No duplica eventos de las operaciones.
    """
    version = _bloquear_version(servicio, version, actor)
    versionamiento.activar_version(version.workflow, version, actor)
    return activar_servicio(servicio, actor)
