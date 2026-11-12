"""Seguimiento del Ticket para el SOLICITANTE (4.F2).

Solo lectura y solo lenguaje de quien pidió algo: en qué fase va, quién lo tiene, cuándo se
espera y qué pidió. No ejecuta ni expone nada operativo (ni bloques, ni permisos, ni
correos). Reutiliza el estado real de la ejecución (`InstanciaWorkflow`/`InstanciaEtapa`/
`fase_workflow`); no existe otro «estado de progreso» paralelo."""

from apps.workflows.models import InstanciaWorkflow

COMPLETADA = "completada"
ACTUAL = "actual"
PENDIENTE = "pendiente"
NO_APLICO = "no_aplico"


def nombre_visible(usuario):
    """Nombre para mostrar a otras personas: nombre completo, o el usuario solo si no hay otro."""
    if usuario is None:
        return ""
    return usuario.nombre_visible


def responsable_visible(ticket):
    """Quién tiene el ticket, en términos de equipo/persona. Nada técnico."""
    equipo = ticket.equipo_responsable.nombre if ticket.equipo_responsable_id else ""
    persona = nombre_visible(ticket.usuario_responsable) if ticket.usuario_responsable_id else ""
    return {"equipo": equipo, "persona": persona, "asignado": bool(equipo or persona)}


def progreso(ticket):
    """Fases del flujo con su situación para este ticket, o `None` si el ticket no tiene un
    flujo por fases (sin Workflow, o Workflow legado de etapas).

    Cada fase es `completada` (ya se pasó por ella), `actual`, `pendiente` (aún por delante) o
    `no_aplico` (el flujo terminó sin necesitarla). No se promete que todas las pendientes se
    recorrerán: un flujo puede saltar fases (p. ej. «Ajustes» solo existe si hay devolución),
    así que no se calcula un porcentaje ni un camino futuro, solo lo ya ocurrido y lo que aún
    queda abierto."""
    if ticket.instancia_workflow_id is None:
        return None
    instancia = ticket.instancia_workflow
    fases = list(instancia.workflow_version.fases.order_by("orden", "pk"))
    if not fases or instancia.configuracion_ejecucion_version_id is None:
        return None

    pasos = list(
        instancia.ejecuciones_etapa.filter(fase_workflow__isnull=False)
        .order_by("orden")
        .values_list("fase_workflow_id", flat=True)
    )
    visitadas = set(pasos)
    terminado = instancia.estado == InstanciaWorkflow.Estado.COMPLETADA
    actual_id = None if terminado or not pasos else pasos[-1]

    filas = []
    for fase in fases:
        if fase.pk == actual_id:
            situacion = ACTUAL
        elif fase.pk in visitadas:
            situacion = COMPLETADA
        elif terminado:
            situacion = NO_APLICO
        else:
            situacion = PENDIENTE
        filas.append({"fase": fase, "nombre": fase.nombre, "situacion": situacion})
    actual = next((fila for fila in filas if fila["situacion"] == ACTUAL), None)
    return {
        "fases": filas,
        "actual": actual["nombre"] if actual else "",
        "terminado": terminado,
        "hay_pendientes": any(fila["situacion"] == PENDIENTE for fila in filas),
    }


def fase_actual(ticket):
    """Nombre de la fase en la que va un ticket abierto, o `""` (sin flujo por fases, flujo
    terminado o ticket ya finalizado)."""
    if ticket.instancia_workflow_id is None or ticket.estado not in ("RADICADO", "EN_ATENCION"):
        return ""
    datos = progreso(ticket)
    return datos["actual"] if datos else ""


def fases_actuales(tickets):
    """`{ticket_id: nombre de la fase actual}` para una LISTA de tickets en dos consultas (bandejas de Mi trabajo y
    Mis tickets). Misma regla que `fase_actual` (ticket abierto, flujo por fases sin terminar, última fase
    recorrida), sin consultar ticket por ticket. Los tickets sin fase actual no aparecen."""
    from apps.workflows.models import InstanciaEtapa

    abiertos = [
        t for t in tickets if t.instancia_workflow_id is not None and t.estado in ("RADICADO", "EN_ATENCION")
    ]
    if not abiertos:
        return {}
    ids = {t.instancia_workflow_id for t in abiertos}
    vigentes = set(
        InstanciaWorkflow.objects.filter(pk__in=ids, configuracion_ejecucion_version__isnull=False)
        .exclude(estado=InstanciaWorkflow.Estado.COMPLETADA)
        .values_list("pk", flat=True)
    )
    ultimas = {}
    for instancia_id, nombre in (
        InstanciaEtapa.objects.filter(instancia_workflow_id__in=vigentes, fase_workflow__isnull=False)
        .order_by("instancia_workflow_id", "orden")
        .values_list("instancia_workflow_id", "fase_workflow__nombre")
    ):
        ultimas[instancia_id] = nombre
    return {t.pk: ultimas[t.instancia_workflow_id] for t in abiertos if t.instancia_workflow_id in ultimas}
