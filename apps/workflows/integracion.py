"""Integración Tarea/Aprobación ↔ Workflow — 3.3/3.4, corrección
arquitectónica aprobada.

Ni `apps.tareas` ni `apps.aprobaciones` conocen `apps.workflows` (ni
siquiera de forma diferida) — la orquestación de "completar/resolver un
trabajo externo que además debe reanudar su Workflow" vive exclusivamente
aquí, del lado integrador:

    apps.tareas / apps.aprobaciones
        completan/resuelven su propio trabajo, no saben que existe un
        Workflow
    apps.workflows (este módulo)
        sabe que estaban vinculados, y reanuda el motor

Cada función pública corre en una única transacción — "Tarea COMPLETADA/
Aprobación decidida pero Workflow no avanzó" queda estructuralmente
imposible (todo o nada). Sin `signals`, sin Domain Events/Observer todavía
(ese patrón está atado a Sprint 7 en `CLAUDE.md`, no se adelanta aquí)."""

from django.db import transaction

from apps.aprobaciones.operaciones import resolver_aprobacion
from apps.tareas.operaciones import completar_tarea
from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaEtapa, TareaWorkflow
from apps.workflows.motor import continuar_espera_externa


@transaction.atomic
def completar_tarea_workflow(tarea, actor):
    """Única forma correcta de completar la Tarea principal que una Etapa
    TAREA generó y que debe reanudar su Workflow (W.6/W.7, correcciones
    aprobadas).

    Completar una SUBTAREA nunca pasa por aquí ni reanuda nada — una
    subtarea nunca tiene fila `TareaWorkflow` (solo la Tarea que
    `EstrategiaTarea` creó directamente la tiene), así que esta función
    falla explícitamente si se le pasa una: no es una omisión, es la
    consecuencia directa y buscada del esquema (1 ejecución TAREA → 1
    Tarea, W.6: "solo completar la Tarea principal vinculada al Workflow
    puede producir continuación")."""
    vinculo = TareaWorkflow.objects.filter(tarea=tarea).select_related("instancia_etapa").first()
    if vinculo is None:
        raise ValueError(
            "Esta tarea no está vinculada a ningún Workflow — use "
            "apps.tareas.operaciones.completar_tarea directamente."
        )

    tarea_completada = completar_tarea(tarea, actor)
    continuar_espera_externa(vinculo.instancia_etapa, motivo_espera=InstanciaEtapa.MotivoEspera.TAREA)
    return tarea_completada


@transaction.atomic
def resolver_aprobacion_workflow(aprobacion, actor, *, decision, observacion=""):
    """Única forma correcta de decidir una `Aprobacion` cuyo
    `EsquemaAprobacion` está vinculado a un Workflow y que, al cerrar,
    debe reanudar su ejecución (3.4, diseño técnico aprobado).

    A diferencia de `completar_tarea_workflow` (TAREA: una sola Tarea → un
    solo cierre posible), aquí el cierre del `EsquemaAprobacion` puede NO
    ocurrir todavía con esta decisión (SECUENCIAL que solo avanza al
    siguiente participante, o PARALELA que sigue esperando otras
    decisiones) — en ese caso no hay nada que continuar aún, y esta
    función retorna sin tocar el Workflow. Cuando sí cierra, resuelve la
    `TransicionEtapa` que corresponde al `resultado` final
    (APROBADA/RECHAZADA/DEVUELTA) contra la propia etapa APROBACION —
    búsqueda exacta por `resultado_aprobacion`, sin usar
    `contexto.evaluar_operador`/`resolver_variable` (esos son de
    CONDICION; una decisión de aprobación no es una expresión a evaluar,
    es una de 3 etiquetas cerradas — RN-021, la resolución de la
    transición es responsabilidad de este módulo integrador, nunca del
    motor)."""
    aprobacion, esquema = resolver_aprobacion(aprobacion, actor, decision=decision, observacion=observacion)
    if esquema.resultado is None:
        return aprobacion, esquema

    vinculo = EsquemaAprobacionWorkflow.objects.filter(esquema=esquema).select_related("instancia_etapa").first()
    if vinculo is None:
        raise ValueError(
            "Este esquema de aprobación no está vinculado a ningún Workflow — use "
            "apps.aprobaciones.operaciones.resolver_aprobacion directamente."
        )

    ejecucion = vinculo.instancia_etapa
    definicion = ejecucion.etapa or ejecucion.bloque_operativo
    transicion = definicion.transiciones_salientes.get(resultado_aprobacion=esquema.resultado)
    continuar_espera_externa(
        ejecucion,
        motivo_espera=InstanciaEtapa.MotivoEspera.APROBACION,
        transicion_seleccionada=transicion,
    )
    return aprobacion, esquema
