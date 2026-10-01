"""Operaciones de dominio de versionamiento y activación de Workflow
(CU-020, RQF-069/070, RN-020). Réplica funcional de
`apps/catalogo/versionamiento.py` para `Formulario`, con una diferencia
real: `activar_version` aquí además ejecuta `validar_estructura` (RQF-069)
antes de permitir la activación — `Formulario` no tiene topología de grafo,
así que esa validación no tiene equivalente allí.

Funciones simples, no una clase Facade — mismo criterio ya aprobado para
Formulario (1.2, sección G): cada una coordina pasos secuenciales sobre un
único agregado (`Workflow` + sus versiones), no varios subsistemas
independientes.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria
from apps.workflows.models import (
    ConfiguracionEtapaAprobacion,
    ConfiguracionEtapaTarea,
    Etapa,
    ParticipanteEtapaAprobacion,
    TransicionEtapa,
    Workflow,
    WorkflowVersion,
)
from apps.workflows.validacion import validar_estructura


def _siguiente_numero(workflow):
    ultimo = WorkflowVersion.objects.filter(workflow=workflow).order_by("-numero").first()
    return (ultimo.numero + 1) if ultimo else 1


def _clonar_configuracion_tarea(etapa_origen, etapa_clon):
    """Clona la plantilla de definición de una Etapa TAREA (FASE 3.C,
    cierre correctivo aprobado) — nunca copia PK ni timestamps, solo los
    4 campos de definición reales de `ConfiguracionEtapaTarea`. Sin
    configuración en la etapa origen, no crea nada (una Etapa TAREA sin
    responsable configurado es válida, ver `ConfiguracionEtapaTarea`)."""
    configuracion = getattr(etapa_origen, "configuracion_tarea", None)
    if configuracion is None:
        return
    ConfiguracionEtapaTarea.objects.create(
        etapa=etapa_clon,
        tipo_responsable=configuracion.tipo_responsable,
        usuario_responsable=configuracion.usuario_responsable,
        equipo_responsable=configuracion.equipo_responsable,
        permite_subtareas=configuracion.permite_subtareas,
    )


def _clonar_configuracion_aprobacion(etapa_origen, etapa_clon):
    """Clona la plantilla de definición de una Etapa APROBACION (FASE 3.C,
    cierre correctivo aprobado): `ConfiguracionEtapaAprobacion` (modo/
    política) y TODOS sus `ParticipanteEtapaAprobacion` (orden/tipo/
    usuario/equipo) — los participantes nuevos son filas propias de la
    nueva versión, nunca las de la versión origen reutilizadas."""
    configuracion = getattr(etapa_origen, "configuracion_aprobacion", None)
    if configuracion is None:
        return
    clon = ConfiguracionEtapaAprobacion.objects.create(
        etapa=etapa_clon,
        modo=configuracion.modo,
        politica=configuracion.politica,
    )
    for participante in configuracion.participantes.order_by("orden"):
        ParticipanteEtapaAprobacion.objects.create(
            configuracion=clon,
            orden=participante.orden,
            tipo_aprobador=participante.tipo_aprobador,
            usuario=participante.usuario,
            equipo=participante.equipo,
        )


# Un clonador explícito por tipo de etapa que tiene configuración real
# (relación, no JSON) — mismo criterio que `ESTRATEGIAS_POR_TIPO`/
# `COLUMNA_POR_TIPO` ya existentes en el proyecto: diccionario plano, sin
# Factory/Strategy nuevo. Un tipo sin entrada aquí (INICIO/CONDICION/
# ESPERA/HITO/FIN/TICKET/GACETA) simplemente no tiene nada que clonar más
# allá de `Etapa`/`TransicionEtapa` (ya clonados en el bucle principal).
_CLONADORES_CONFIGURACION_POR_TIPO = {
    Etapa.Tipo.TAREA: _clonar_configuracion_tarea,
    Etapa.Tipo.APROBACION: _clonar_configuracion_aprobacion,
}


@transaction.atomic
def crear_workflow(actor, *, nombre, descripcion=""):
    """3.UI.3 — cierra un GAP real: `workflows.administrar` (ver docstring
    de `apps.workflows.autorizacion.PERMISO_ADMINISTRAR`) ya documentaba
    "crear/editar Workflow" como parte de su alcance, pero hasta ahora esa
    alta solo existía vía Django Admin (auditada gratis por
    `AdminAuditableMixin`) — ninguna función de dominio la exponía. Mismo
    criterio que `crear_nueva_version`/`activar_version` en este mismo
    módulo: NO verifica autorización internamente, es responsabilidad de
    quien invoca (la vista) confirmar `puede_administrar_workflows(actor)`
    antes de llamar.

    Crea el `Workflow` y delega la v1 BORRADOR a `crear_nueva_version` sin
    `clonar_desde`: como el Workflow es nuevo, `workflow.version_activa` es
    `None`, así que `crear_nueva_version` ya construye por su propio
    comportamiento existente una v1 vacía (nada de esa lógica se
    reimplementa aquí). Alta de `Workflow` + v1 en una sola transacción."""
    workflow = Workflow.objects.create(nombre=nombre, descripcion=descripcion)
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=workflow,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(workflow),
    )
    crear_nueva_version(workflow, actor)
    return workflow


@transaction.atomic
def editar_workflow(workflow, actor, *, nombre=None, descripcion=None):
    """Edita únicamente los metadatos propios del contenedor `Workflow`
    (`nombre`/`descripcion`) — nunca `Etapa`/`TransicionEtapa`/
    configuraciones/`WorkflowVersion`/`version_activa`: esos ya tienen sus
    propias vías de mutación (una versión BORRADOR editable, y
    `activar_version` para `version_activa`). Editar estos metadatos NO
    constituye una nueva versión — son datos del contenedor, no de la
    definición funcional congelada que `WorkflowVersion` versiona. Mismo
    criterio que `crear_workflow`: no verifica autorización internamente."""
    anterior = serializar(workflow)
    campos = []
    if nombre is not None:
        workflow.nombre = nombre
        campos.append("nombre")
    if descripcion is not None:
        workflow.descripcion = descripcion
        campos.append("descripcion")
    if not campos:
        return workflow
    workflow.save(update_fields=campos + ["actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=workflow,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(workflow),
    )
    return workflow


@transaction.atomic
def crear_nueva_version(workflow, actor, clonar_desde=None):
    """Crea una `WorkflowVersion` BORRADOR nueva para `workflow`.

    Si `clonar_desde` no se indica, clona desde `workflow.version_activa`
    (la base natural para seguir iterando). Si no hay versión activa
    (primer workflow), crea una versión 1 vacía. `clonar_desde` es también
    el mecanismo para "recuperar" el contenido de una versión HISTORICA: se
    clona a un borrador nuevo en vez de reactivarla directamente — ver
    `activar_version`. Preserva la definición funcional completa: etapas
    (incluida su `configuracion` JSON), sus transiciones salientes
    (remapeadas vía `mapa_etapas`, incluido `resultado_aprobacion`) y,
    desde FASE 3.C, la configuración relacional de TAREA/APROBACION
    (`_CLONADORES_CONFIGURACION_POR_TIPO`) — nunca información de
    ejecución (`InstanciaWorkflow`/`InstanciaEtapa`/`TareaWorkflow`/
    `EsquemaAprobacionWorkflow`/`Aprobacion`), que vive exclusivamente
    atada a la versión que efectivamente se ejecutó.
    """
    # Misma fila que bloquea la configuración empresarial y la activación:
    # evita dos números de versión iguales y clonar una vigente obsoleta.
    workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
    origen = clonar_desde if clonar_desde is not None else workflow.version_activa

    nueva = WorkflowVersion.objects.create(
        workflow=workflow,
        numero=_siguiente_numero(workflow),
        estado=WorkflowVersion.Estado.BORRADOR,
    )

    if origen is not None:
        mapa_etapas = {}
        for etapa in origen.etapas.all():
            clon = Etapa.objects.create(
                version=nueva,
                tipo=etapa.tipo,
                nombre=etapa.nombre,
                descripcion=etapa.descripcion,
                configuracion=etapa.configuracion,
            )
            mapa_etapas[etapa.pk] = clon
            clonador = _CLONADORES_CONFIGURACION_POR_TIPO.get(etapa.tipo)
            if clonador:
                clonador(etapa, clon)
        for etapa in origen.etapas.all():
            for transicion in etapa.transiciones_salientes.all():
                TransicionEtapa.objects.create(
                    etapa_origen=mapa_etapas[transicion.etapa_origen_id],
                    etapa_destino=mapa_etapas[transicion.etapa_destino_id],
                    nombre=transicion.nombre,
                    prioridad=transicion.prioridad,
                    variable=transicion.variable,
                    operador=transicion.operador,
                    valor=transicion.valor,
                    es_fallback=transicion.es_fallback,
                    resultado_aprobacion=transicion.resultado_aprobacion,
                )

    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=nueva,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(nueva),
    )
    return nueva


@transaction.atomic
def activar_version(workflow, version, actor):
    """Activa `version` como versión vigente de `workflow` (RQF-070).

    Exige `version.estado == BORRADOR` y que `validar_estructura(version)`
    no reporte errores (RQF-069) — si hay errores, se lanza
    `ValidationError` con la lista completa, sin mutar nada. La versión
    previamente activa pasa a HISTORICA. Recuperar una HISTORICA no es
    "reactivarla": se clona a un borrador nuevo (`crear_nueva_version`) y se
    activa ese borrador.

    La activación queda auditada en `RegistroAuditoria` sobre el propio
    `Workflow` (cambia su configuración efectiva, `workflow.version_activa`)
    — no se crea un historial de Workflow aparte, `RegistroAuditoria` ya
    resuelve esa trazabilidad.
    """
    workflow_original, version_original = workflow, version
    workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
    version = WorkflowVersion.objects.get(pk=version.pk)
    if version.workflow_id != workflow.pk:
        raise ValueError("La versión no pertenece a este workflow.")
    if version.estado != WorkflowVersion.Estado.BORRADOR:
        raise ValueError("Solo se puede activar una versión en estado BORRADOR.")

    errores = validar_estructura(version)
    if errores:
        raise ValidationError(errores)

    datos_anteriores = {"version_activa_id": workflow.version_activa_id}

    anterior = workflow.version_activa
    if anterior is not None:
        anterior.estado = WorkflowVersion.Estado.HISTORICA
        anterior.save(update_fields=["estado", "actualizado_en"])

    version.estado = WorkflowVersion.Estado.ACTIVA
    version.save(update_fields=["estado", "actualizado_en"])

    workflow.version_activa = version
    workflow.save(update_fields=["version_activa", "actualizado_en"])

    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=workflow,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=datos_anteriores,
        datos_nuevos={"version_activa_id": version.pk},
    )
    workflow_original.refresh_from_db()
    version_original.refresh_from_db()
