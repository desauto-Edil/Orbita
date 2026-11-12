"""Vínculos de lectura Tarea / Aprobación → Ticket (UX-1, Espacio unificado del Ticket).

Una Tarea o una Aprobación NO conoce su Ticket (`apps.tareas` y `apps.aprobaciones` son dominios reutilizables):
la relación vive del lado integrador y hoy tiene exactamente estas formas, todas ya existentes e inmutables:

- Tarea del flujo: `workflows.TareaWorkflow` → `InstanciaEtapa` → `InstanciaWorkflow` (la principal o una
  subinstancia de un recorrido por ítem; el Ticket apunta siempre a la principal, `raiz_id`). Una subtarea
  pertenece al Ticket de su Tarea padre.
- Tarea por elemento: `tickets.TareaElemento` → `ElementoParrilla` → `ParrillaTicket.ticket`.
- Corrección de gaceta: `gacetas.RondaRevisionGaceta.tarea_correccion` → `ParticipacionItemGaceta.gaceta.ticket`.
- Aprobación del flujo: `workflows.EsquemaAprobacionWorkflow` → `InstanciaEtapa` → `InstanciaWorkflow`.
- Aprobación de una ronda de gaceta: `RondaRevisionGaceta.esquema`.
- Aprobación de una prórroga: `tickets.ProrrogaTicket.esquema_aprobacion`.
- Aprobación de una cancelación: `tickets.SolicitudCancelacion.esquema_aprobacion`.

Este módulo solo LEE esas relaciones en consultas por lote (una por forma, nunca una por fila) para que la
bandeja de Trabajo agrupe por Ticket y el espacio del Ticket encuentre lo que le pertenece. No decide permisos
ni estados: cada pantalla sigue preguntando a `puede_*` de su dominio."""

from apps.aprobaciones.models import Aprobacion
from apps.tareas.models import Tarea


def _tickets_por_instancia(instancias):
    """`{instancia_id: ticket_id}` para las instancias PRINCIPALES dadas."""
    from apps.tickets.models import Ticket

    if not instancias:
        return {}
    return dict(Ticket.objects.filter(instancia_workflow_id__in=instancias).values_list("instancia_workflow_id", "pk"))


def origen_de_tareas(tarea_ids):
    """`{tarea_id: {"ticket_id", "elemento_id", "origen"}}` de las Tareas dadas que pertenecen a un Ticket.

    `origen` es `FLUJO`, `ELEMENTO` (tarea por elemento) o `GACETA` (corrección); `elemento_id` es el ítem de
    la colección sobre el que trabaja (recorrido por ítem, tarea por elemento o corrección), o `None`. Una Tarea
    independiente no aparece en el resultado."""
    from apps.gacetas.models import RondaRevisionGaceta
    from apps.tickets.models import TareaElemento
    from apps.workflows.models import TareaWorkflow

    ids = set(tarea_ids)
    if not ids:
        return {}
    principal_de = {
        pk: (padre or pk) for pk, padre in Tarea.objects.filter(pk__in=ids).values_list("pk", "tarea_padre_id")
    }
    principales = set(principal_de.values())

    por_principal = {}
    filas_flujo = list(
        TareaWorkflow.objects.filter(tarea_id__in=principales).values_list(
            "tarea_id",
            "instancia_etapa__instancia_workflow_id",
            "instancia_etapa__instancia_workflow__instancia_padre_id",
            "instancia_etapa__instancia_workflow__elemento_id",
        )
    )
    tickets = _tickets_por_instancia({padre or instancia for _, instancia, padre, _ in filas_flujo})
    for tarea_id, instancia, padre, elemento_id in filas_flujo:
        ticket_id = tickets.get(padre or instancia)
        if ticket_id is not None:
            por_principal[tarea_id] = {"ticket_id": ticket_id, "elemento_id": elemento_id, "origen": "FLUJO"}
    for tarea_id, ticket_id, elemento_id in TareaElemento.objects.filter(tarea_id__in=principales).values_list(
        "tarea_id", "elemento__parrilla__ticket_id", "elemento_id"
    ):
        por_principal[tarea_id] = {"ticket_id": ticket_id, "elemento_id": elemento_id, "origen": "ELEMENTO"}
    for tarea_id, ticket_id, elemento_id in RondaRevisionGaceta.objects.filter(
        tarea_correccion_id__in=principales
    ).values_list("tarea_correccion_id", "participacion__gaceta__ticket_id", "participacion__elemento_id"):
        por_principal[tarea_id] = {"ticket_id": ticket_id, "elemento_id": elemento_id, "origen": "GACETA"}

    return {pk: por_principal[principal] for pk, principal in principal_de.items() if principal in por_principal}


def origen_de_aprobaciones(aprobaciones):
    """`{aprobacion_id: {"ticket_id", "elemento_id", "origen"}}` (`FLUJO`, `GACETA`, `PRORROGA` o `CANCELACION`) de las
    Aprobaciones dadas que pertenecen a un Ticket. Recibe instancias (usa `esquema_id`, sin consultas extra)."""
    from apps.gacetas.models import RondaRevisionGaceta
    from apps.tickets.models import ProrrogaTicket, SolicitudCancelacion
    from apps.workflows.models import EsquemaAprobacionWorkflow

    por_esquema = {}
    esquemas = {a.esquema_id for a in aprobaciones}
    if not esquemas:
        return {}
    filas_flujo = list(
        EsquemaAprobacionWorkflow.objects.filter(esquema_id__in=esquemas).values_list(
            "esquema_id",
            "instancia_etapa__instancia_workflow_id",
            "instancia_etapa__instancia_workflow__instancia_padre_id",
            "instancia_etapa__instancia_workflow__elemento_id",
        )
    )
    tickets = _tickets_por_instancia({padre or instancia for _, instancia, padre, _ in filas_flujo})
    for esquema_id, instancia, padre, elemento_id in filas_flujo:
        ticket_id = tickets.get(padre or instancia)
        if ticket_id is not None:
            por_esquema[esquema_id] = {"ticket_id": ticket_id, "elemento_id": elemento_id, "origen": "FLUJO"}
    for esquema_id, ticket_id, elemento_id in RondaRevisionGaceta.objects.filter(esquema_id__in=esquemas).values_list(
        "esquema_id", "participacion__gaceta__ticket_id", "participacion__elemento_id"
    ):
        por_esquema[esquema_id] = {"ticket_id": ticket_id, "elemento_id": elemento_id, "origen": "GACETA"}
    for esquema_id, ticket_id in ProrrogaTicket.objects.filter(esquema_aprobacion_id__in=esquemas).values_list(
        "esquema_aprobacion_id", "ticket_id"
    ):
        por_esquema[esquema_id] = {"ticket_id": ticket_id, "elemento_id": None, "origen": "PRORROGA"}
    for esquema_id, ticket_id in SolicitudCancelacion.objects.filter(esquema_aprobacion_id__in=esquemas).values_list(
        "esquema_aprobacion_id", "ticket_id"
    ):
        por_esquema[esquema_id] = {"ticket_id": ticket_id, "elemento_id": None, "origen": "CANCELACION"}
    return {a.pk: por_esquema[a.esquema_id] for a in aprobaciones if a.esquema_id in por_esquema}


def instancias_del_ticket(ticket):
    """La instancia principal del Ticket y sus subinstancias (vacío si no tiene flujo)."""
    if ticket.instancia_workflow_id is None:
        return []
    from apps.workflows.integracion import ids_de_ejecucion

    return ids_de_ejecucion(ticket.instancia_workflow_id)


def tareas_del_ticket(ticket):
    """QuerySet de TODAS las Tareas que pertenecen al Ticket (del flujo, por elemento, correcciones de gaceta y
    sus subtareas), en cualquier estado. Solo identifica: quién puede verlas lo decide `apps.tareas`."""
    from django.db.models import Q

    instancias = instancias_del_ticket(ticket)
    principales = Q(vinculo_elemento__elemento__parrilla__ticket_id=ticket.pk) | Q(
        ronda_gaceta__participacion__gaceta__ticket_id=ticket.pk
    )
    if instancias:
        principales |= Q(vinculo_workflow__instancia_etapa__instancia_workflow_id__in=instancias)
    ids = set(Tarea.objects.filter(principales).values_list("pk", flat=True))
    ids |= set(Tarea.objects.filter(tarea_padre_id__in=ids).values_list("pk", flat=True))
    return Tarea.objects.filter(pk__in=ids)


def aprobaciones_del_ticket(ticket):
    """QuerySet de TODAS las Aprobaciones (participaciones) que pertenecen al Ticket: del flujo, de las rondas de
    sus gacetas, de sus prórrogas y de sus solicitudes de cancelación."""
    from django.db.models import Q

    instancias = instancias_del_ticket(ticket)
    condicion = (
        Q(esquema__ronda_gaceta__participacion__gaceta__ticket_id=ticket.pk)
        | Q(esquema__prorroga__ticket_id=ticket.pk)
        | Q(esquema__cancelacion__ticket_id=ticket.pk)
    )
    if instancias:
        condicion |= Q(esquema__vinculo_workflow__instancia_etapa__instancia_workflow_id__in=instancias)
    return Aprobacion.objects.filter(condicion).distinct()


def url_de_tarea(tarea):
    """Dónde se trabaja una Tarea: el espacio de SU ticket (hay una sola vista de trabajo; las páginas de lista,
    detalle y vista previa de tareas se retiraron). Una tarea sin ticket lleva a Mi trabajo. Solo navegación: el
    espacio vuelve a autorizar al abrirse."""
    from django.urls import reverse

    origen = origen_de_tareas([tarea.pk]).get(tarea.pk)
    if origen is None:
        return reverse("core:mi_trabajo")
    return reverse("tickets:espacio", args=[origen["ticket_id"]])


def url_de_aprobacion(aprobacion):
    """Dónde se decide una Aprobación: el espacio de SU ticket (sin ticket, Mi trabajo). Solo navegación."""
    from django.urls import reverse

    origen = origen_de_aprobaciones([aprobacion]).get(aprobacion.pk)
    if origen is None:
        return reverse("core:mi_trabajo")
    return reverse("tickets:espacio", args=[origen["ticket_id"]])
