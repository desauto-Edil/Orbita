"""Autorización de las gacetas — Fase 3. Relación con el objeto, nunca por nombre de rol.

- REVISOR: persona configurada (o miembro activo de un equipo configurado) como revisor de ESTA gaceta, congelado
  en el ticket. Registra observaciones mientras el periodo de revisión está abierto.
- APROBADOR: ídem como aprobador; decide en `apps.aprobaciones` y también puede observar.
- COORDINADOR: presenta ítems, cierra el periodo, resuelve la gaceta y excluye un ítem sin otra aprobación.
  Fase 4.A: si la gaceta congeló un coordinador propio (persona o equipo), es esa persona o un miembro activo de
  ese equipo, mientras el ticket esté radicado o en atención. Si no lo tiene (incluidas todas las gacetas creadas
  antes de 4.A), es quien edita los ítems del ticket (`puede_editar_parrilla`), como siempre.
- CORRECTOR: el responsable de la tarea de corrección del ítem (o el coordinador). Responde observaciones y
  reenvía la corrección.
- CONSULTA: cualquiera de los anteriores o quien puede consultar el ticket. Revisores y aprobadores ven SOLO la
  gaceta (ítems, evidencias, versiones, observaciones), no el resto del ticket.

Las operaciones revalidan bajo lock (`apps.gacetas.operaciones`)."""

from apps.core.models import MiembroEquipo


def _activo(usuario):
    return bool(getattr(usuario, "is_authenticated", False)) and bool(getattr(usuario, "is_active", False))


def _esta_en(usuario, actores):
    if not _activo(usuario):
        return False
    if any(a.get("tipo") == "USUARIO" and a.get("usuario_id") == usuario.pk for a in actores or []):
        return True
    equipos = [a.get("equipo_id") for a in actores or [] if a.get("tipo") == "EQUIPO"]
    return bool(equipos) and MiembroEquipo.objects.filter(equipo_id__in=equipos, usuario_id=usuario.pk, activo=True).exists()


def es_revisor(usuario, gaceta):
    return _esta_en(usuario, (gaceta.configuracion or {}).get("revisores"))


def es_aprobador(usuario, gaceta):
    return _esta_en(usuario, (gaceta.configuracion or {}).get("aprobadores"))


def coordinador_configurado(gaceta):
    """El actor coordinador congelado en la gaceta (`{"tipo", "usuario_id"|"equipo_id"}`) o `None`."""
    return (gaceta.configuracion or {}).get("coordinador") or None


def es_coordinador_configurado(usuario, gaceta):
    actor = coordinador_configurado(gaceta)
    return actor is not None and _esta_en(usuario, [actor])


def puede_coordinar(usuario, gaceta):
    from apps.tickets.autorizacion import puede_editar_parrilla
    from apps.tickets.models import Ticket

    if coordinador_configurado(gaceta) is not None:
        return (
            gaceta.ticket.estado in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)
            and es_coordinador_configurado(usuario, gaceta)
        )
    return puede_editar_parrilla(usuario, gaceta.ticket)


def es_corrector(usuario, gaceta):
    """Responsable de alguna tarea de corrección ABIERTA de esta gaceta: necesita verla para responder las
    observaciones y reenviar (sin ganar acceso al resto del ticket)."""
    if not _activo(usuario):
        return False
    from apps.gacetas.models import RondaRevisionGaceta
    from apps.tareas.autorizacion import puede_completar_tarea

    for ronda in RondaRevisionGaceta.objects.filter(
        participacion__gaceta=gaceta, participacion__estado="EN_CORRECCION", tarea_correccion__isnull=False
    ).select_related("tarea_correccion"):
        if puede_completar_tarea(usuario, ronda.tarea_correccion):
            return True
    return False


def puede_consultar_gaceta(usuario, gaceta):
    if not _activo(usuario):
        return False
    from apps.tickets.autorizacion import puede_consultar_ticket

    return (
        es_revisor(usuario, gaceta) or es_aprobador(usuario, gaceta) or es_corrector(usuario, gaceta)
        or es_coordinador_configurado(usuario, gaceta) or puede_consultar_ticket(usuario, gaceta.ticket)
    )


def puede_observar(usuario, participacion):
    gaceta = participacion.gaceta
    return (
        gaceta.periodo_abierto
        and participacion.estado in participacion.ESTADOS_PENDIENTES
        and (es_revisor(usuario, gaceta) or es_aprobador(usuario, gaceta))
    )


def puede_corregir(usuario, participacion):
    """Responde observaciones y reenvía: el responsable de la tarea de corrección vigente o el coordinador."""
    if not _activo(usuario) or participacion.estado != participacion.Estado.EN_CORRECCION:
        return False
    if puede_coordinar(usuario, participacion.gaceta):
        return True
    ronda = participacion.rondas.filter(numero=participacion.ronda).select_related("tarea_correccion").first()
    tarea = ronda.tarea_correccion if ronda is not None else None
    if tarea is None:
        return False
    from apps.tareas.autorizacion import puede_completar_tarea

    return puede_completar_tarea(usuario, tarea)


def puede_ver_elemento(usuario, elemento):
    """Revisores y aprobadores de una gaceta en la que participa el ítem ven sus evidencias (descarga)."""
    from apps.gacetas.models import ParticipacionItemGaceta

    for participacion in ParticipacionItemGaceta.objects.filter(elemento=elemento).select_related("gaceta"):
        if es_revisor(usuario, participacion.gaceta) or es_aprobador(usuario, participacion.gaceta):
            return True
    return False
