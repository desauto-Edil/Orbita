"""Lecturas para las pantallas de gacetas — Fase 3. Nada aquí cambia estado ni decide permisos (eso es
`apps.gacetas.autorizacion` y `apps.gacetas.operaciones`)."""

from apps.gacetas import autorizacion
from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
from apps.gacetas.operaciones import resumen


def gacetas_visibles_para(usuario):
    """Gacetas activas (abiertas o con el periodo cerrado) donde `usuario` es revisor, aprobador o coordinador
    configurado (directo o por equipo). La consulta filtra en Python sobre las activas: las gacetas activas son pocas a la vez."""
    activas = GacetaTicket.objects.filter(estado__in=GacetaTicket.ESTADOS_ACTIVOS).select_related(
        "ticket__detalle_servicio__servicio"
    ).order_by("vence_revision_en", "pk")
    return [
        g for g in activas
        if autorizacion.es_revisor(usuario, g) or autorizacion.es_aprobador(usuario, g)
        or autorizacion.es_coordinador_configurado(usuario, g)
    ]


def presentables(gaceta):
    """Ítems que el coordinador puede presentar a mano: en el alcance de las colecciones de la gaceta y aún sin
    presentar. Solo con la gaceta ABIERTA (la operación `presentar` vuelve a validarlo todo)."""
    from apps.tickets import parrilla as parrilla_ops

    if gaceta.estado != GacetaTicket.Estado.ABIERTA:
        return []
    ya = set(ParticipacionItemGaceta.objects.filter(gaceta=gaceta).values_list("elemento_id", flat=True))
    elementos = []
    for parrilla in parrilla_ops.parrillas_de(gaceta.ticket):
        if parrilla.clave in (gaceta.configuracion.get("colecciones") or []):
            elementos.extend(e for e in parrilla_ops.elementos_en_alcance(parrilla) if e.pk not in ya)
    return elementos


def del_ticket(usuario, ticket):
    """Resumen de las gacetas de un ticket para su detalle / espacio de trabajo."""
    return [
        {"gaceta": g, "resumen": resumen(g), "puede_ver": autorizacion.puede_consultar_gaceta(usuario, g)}
        for g in GacetaTicket.objects.filter(ticket=ticket).order_by("pk")
    ]
