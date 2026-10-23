"""Lecturas para las pantallas de gacetas — Fase 3. Nada aquí cambia estado ni decide permisos (eso es
`apps.gacetas.autorizacion` y `apps.gacetas.operaciones`)."""

from apps.gacetas import autorizacion
from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
from apps.gacetas.operaciones import estado_operativo, resumen


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


def _aprobacion_pendiente_de(usuario, ronda):
    from apps.aprobaciones.autorizacion import puede_aprobar

    if ronda is None or ronda.esquema_id is None or ronda.esquema.resultado is not None:
        return None
    for aprobacion in ronda.esquema.participaciones.all():
        if puede_aprobar(usuario, aprobacion):
            return aprobacion
    return None


def detalle(usuario, gaceta):
    """Todo lo que pinta la página de una gaceta: resumen, plazos, permisos de coordinación y, por cada ítem,
    estado de gaceta y operativo, valores, rondas (con su versión), observaciones y lo que `usuario` puede
    hacer (observar, responder, reenviar, decidir en su aprobación, excluir)."""
    from apps.tickets import parrilla as parrilla_ops

    coordinador = autorizacion.puede_coordinar(usuario, gaceta)
    filas = []
    for participacion in ParticipacionItemGaceta.objects.filter(gaceta=gaceta).select_related(
        "elemento__parrilla", "presentada_por", "decidida_por"
    ).order_by("elemento__parrilla__orden", "elemento__numero"):
        participacion.gaceta = gaceta
        rondas = list(participacion.rondas.select_related("version", "esquema", "tarea_correccion").order_by("numero"))
        actual = rondas[-1] if rondas else None
        filas.append({
            "participacion": participacion,
            "elemento": participacion.elemento,
            "valores": parrilla_ops.tabla_de_elemento(participacion.elemento),
            "archivos": parrilla_ops.archivos_de(participacion.elemento),
            "estado_operativo": estado_operativo(participacion.elemento),
            "rondas": rondas,
            "observaciones": list(participacion.observaciones.select_related("autor", "respondida_por", "ronda")),
            "puede_observar": autorizacion.puede_observar(usuario, participacion),
            "puede_corregir": autorizacion.puede_corregir(usuario, participacion),
            "puede_excluir": coordinador and not participacion.es_final,
            "aprobacion_pendiente": _aprobacion_pendiente_de(usuario, actual),
        })
    presentables = []
    if coordinador and gaceta.estado == GacetaTicket.Estado.ABIERTA:
        ya = {f["elemento"].pk for f in filas}
        for parrilla in parrilla_ops.parrillas_de(gaceta.ticket):
            if parrilla.clave in (gaceta.configuracion.get("colecciones") or []):
                presentables.extend(e for e in parrilla_ops.elementos_en_alcance(parrilla) if e.pk not in ya)
    return {
        "gaceta": gaceta,
        "resumen": resumen(gaceta),
        "filas": filas,
        "coordinador": coordinador,
        "presentables": presentables,
        "es_revisor": autorizacion.es_revisor(usuario, gaceta),
        "es_aprobador": autorizacion.es_aprobador(usuario, gaceta),
    }


def del_ticket(usuario, ticket):
    """Resumen de las gacetas de un ticket para su detalle / espacio de trabajo."""
    return [
        {"gaceta": g, "resumen": resumen(g), "puede_ver": autorizacion.puede_consultar_gaceta(usuario, g)}
        for g in GacetaTicket.objects.filter(ticket=ticket).order_by("pk")
    ]
