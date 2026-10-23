"""Gaceta ↔ Workflow — Fase 3 del modelo configurable.

La gaceta NO es una fase ni depende del flujo para recibir ítems (entran a mano, solos o al terminar su
recorrido). El bloque GACETA es una forma OPCIONAL de coordinarla desde la instancia principal:

    BloqueOperativo.configuracion = {"gaceta": <clave>, "presentar_items": bool, "esperar": "RESOLUCION"|"NINGUNA"}

- Al entrar: abre la gaceta si seguía sin abrir y, si se pide, presenta los ítems en el alcance que cumplen sus
  requisitos (idempotente).
- `esperar=NINGUNA`: el flujo sigue (la gaceta opera en paralelo con el resto del proceso).
- `esperar=RESOLUCION`: espera (motivo GACETA) hasta que la gaceta se resuelva; `continuar_por_gaceta` lo libera.
- En ambos casos publica `gacetas.<clave_bloque>.{total, aprobados, rechazados, excluidos, resuelta}` para que
  una DECISION posterior elija la ruta. No hay otro motor ni condiciones paralelas.

Sin bloqueos circulares: la resolución de la gaceta no depende de la instancia principal (solo la avisa), los
recorridos de ítems son subinstancias independientes y las decisiones van por `apps.aprobaciones`. Orden de
locks: instancia principal → Ticket → gaceta.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.gacetas.models import GacetaTicket
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow


def entrar_al_bloque(ticket, bloque):
    """Entrada del bloque GACETA (la llama su Strategy, dentro del motor). Devuelve `(esperar, resumen)`."""
    from apps.gacetas import operaciones

    cfg = bloque.configuracion or {}
    gaceta = GacetaTicket.objects.select_for_update().filter(ticket=ticket, clave=cfg.get("gaceta") or "").first()
    if gaceta is None:
        raise ValidationError("Este ticket no tiene la gaceta del bloque (se configuró después de crearlo).")
    if gaceta.estado == GacetaTicket.Estado.CANCELADA:
        raise ValidationError("La gaceta de este bloque está cancelada.")
    operaciones._abrir_bloqueada(gaceta)
    if cfg.get("presentar_items", True) and gaceta.estado == GacetaTicket.Estado.ABIERTA:
        operaciones.presentar_por_flujo(gaceta)
    gaceta.refresh_from_db()
    esperar = cfg.get("esperar", "RESOLUCION") == "RESOLUCION" and gaceta.estado != GacetaTicket.Estado.RESUELTA
    return esperar, operaciones.resumen(gaceta)


@transaction.atomic
def continuar_por_gaceta(gaceta):
    """La gaceta se resolvió: si la instancia principal del ticket la está esperando en un bloque GACETA, sigue
    por su ruta con el resumen publicado. Idempotente (un segundo aviso ya no encuentra esa espera)."""
    from apps.gacetas.operaciones import resumen
    from apps.workflows.motor import continuar_espera_externa

    instancia_id = gaceta.ticket.instancia_workflow_id
    if instancia_id is None:
        return False
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return False
    ejecucion = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if (
        ejecucion is None
        or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA
        or ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.GACETA
        or ejecucion.bloque_operativo is None
        or (ejecucion.bloque_operativo.configuracion or {}).get("gaceta") != gaceta.clave
    ):
        return False
    datos = resumen(gaceta)
    continuar_espera_externa(
        ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.GACETA, resultado_bloque=("gacetas", datos),
        resultado_ejecucion=datos,
    )
    return True
