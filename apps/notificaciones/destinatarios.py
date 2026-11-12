"""¿A quién le llega un aviso? Siempre por RELACIÓN con el objeto, nunca por nombre de rol (RN-007).

Cada función devuelve personas ACTIVAS (`Usuario`), ya sin duplicados. Quién hizo la acción se excluye en el
despacho, no aquí."""

from django.utils import timezone

from apps.core.models import MiembroEquipo, Usuario


def _activos(consulta):
    return list(consulta.filter(is_active=True).distinct().order_by("pk"))


def personas(ids):
    ids = [i for i in ids if i]
    if not ids:
        return []
    return _activos(Usuario.objects.filter(pk__in=ids))


def miembros_de(equipo_ids):
    """Miembros vigentes de equipos ACTIVOS."""
    equipo_ids = [e for e in (equipo_ids if isinstance(equipo_ids, (list, tuple, set)) else [equipo_ids]) if e]
    if not equipo_ids:
        return []
    hoy = timezone.localdate()
    usuarios = MiembroEquipo.objects.filter(
        equipo_id__in=equipo_ids, equipo__activo=True, activo=True, fecha_inicio__lte=hoy,
    ).exclude(fecha_fin__lt=hoy).values_list("usuario_id", flat=True)
    return personas(list(usuarios))


def de_actores(actores):
    """Actores congelados `[{"tipo": "USUARIO"|"EQUIPO", "usuario_id"|"equipo_id"}]` (gacetas, participantes)."""
    usuarios = [a.get("usuario_id") for a in actores or [] if a.get("tipo") == "USUARIO"]
    equipos = [a.get("equipo_id") for a in actores or [] if a.get("tipo") == "EQUIPO"]
    return unir(personas(usuarios), miembros_de(equipos))


def unir(*listas):
    vistos, salida = set(), []
    for lista in listas:
        for usuario in lista or []:
            if usuario is not None and usuario.pk not in vistos:
                vistos.add(usuario.pk)
                salida.append(usuario)
    return salida


# --- Ticket -------------------------------------------------------------------------------------------------


def solicitante(ticket):
    if ticket is None or ticket.solicitante_id is None:
        return []  # Un Proceso programado no tiene solicitante: nunca se inventa uno.
    return personas([ticket.solicitante_id])


def responsable(ticket):
    """El responsable individual; sin él, los miembros del equipo responsable."""
    if ticket is None:
        return []
    if ticket.usuario_responsable_id:
        return personas([ticket.usuario_responsable_id])
    return miembros_de(ticket.equipo_responsable_id)


def equipo_responsable(ticket):
    return miembros_de(ticket.equipo_responsable_id) if ticket is not None else []


def responsables_configurados(ticket):
    """Las personas y los miembros vigentes de los equipos configurados como responsables (`ServicioResponsable`
    activos) del Servicio/Proceso del ticket."""
    from apps.catalogo.models import ServicioResponsable

    if ticket is None:
        return []
    filas = ServicioResponsable.objects.filter(
        servicio_id=ticket.detalle_servicio.servicio_id, activo=True
    ).values_list("tipo_responsable", "usuario_id", "equipo_id")
    usuarios = [u for t, u, _ in filas if t == ServicioResponsable.TipoResponsable.USUARIO]
    equipos = [e for t, _, e in filas if t == ServicioResponsable.TipoResponsable.EQUIPO]
    return unir(personas(usuarios), miembros_de(equipos))


def pueden_tomar(ticket):
    """Quien puede TOMAR el ticket ahora (la regla de la Cola, `puede_tomar`, que excluye al solicitante): los
    miembros del equipo al que se dirigió y, sobre todo, los responsables configurados del Servicio/Proceso (un
    ticket normal nace sin equipo puesto: su Cola la forman esos responsables)."""
    from apps.tickets.autorizacion import puede_tomar

    candidatos = unir(equipo_responsable(ticket), responsables_configurados(ticket))
    return [u for u in candidatos if puede_tomar(u, ticket)]


def supervisores(ticket):
    """Quien supervisa la atención del ticket: `tickets.atender` vigente en un alcance de ÁREA o UNIDAD que cubre el
    ticket (sus contextos de atención congelados). No incluye el alcance GLOBAL (sería toda la organización)."""
    from django.db.models import Q

    from apps.core.autorizacion import _vigentes
    from apps.core.models import AsignacionRol
    from apps.tickets.autorizacion import PERMISO_ATENDER

    if ticket is None:
        return []
    contextos = list(ticket.contextos_atencion.all())
    areas = [c.area_id for c in contextos if c.area_id]
    unidades = [c.unidad_negocio_id for c in contextos if c.unidad_negocio_id]
    if not areas and not unidades:
        return []
    filtro = Q(tipo_alcance=AsignacionRol.TipoAlcance.AREA, area_id__in=areas) | Q(
        tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD, unidad_negocio_id__in=unidades
    )
    ids = _vigentes(PERMISO_ATENDER).filter(filtro).values_list("usuario_id", flat=True)
    return personas(list(ids))


def de_campo_formulario(ticket, clave):
    """La persona (o los miembros del equipo) que el solicitante eligió en un campo USUARIO/EQUIPO del formulario
    CONGELADO del ticket. Campo inexistente o vacío = nadie (nunca un error)."""
    from apps.tickets.models import RespuestaCampo

    if ticket is None or not clave:
        return []
    respuesta = (
        RespuestaCampo.objects.filter(respuesta_formulario__ticket=ticket, campo__clave=clave)
        .values("valor_usuario_id", "valor_equipo_id").first()
    )
    if not respuesta:
        return []
    return unir(personas([respuesta["valor_usuario_id"]]), miembros_de(respuesta["valor_equipo_id"]))


def de_columna_item(elemento_id, clave):
    from apps.tickets.models import ValorElemento

    if not elemento_id or not clave:
        return []
    valor = (
        ValorElemento.objects.filter(elemento_id=elemento_id, campo__clave=clave)
        .values("valor_usuario_id", "valor_equipo_id").first()
    )
    if not valor:
        return []
    return unir(personas([valor["valor_usuario_id"]]), miembros_de(valor["valor_equipo_id"]))


# --- Tareas y aprobaciones ----------------------------------------------------------------------------------


def de_tarea(tarea):
    """Quien debe atender la Tarea: el responsable (persona o miembros de su equipo)."""
    if tarea is None:
        return []
    if tarea.usuario_responsable_id:
        return personas([tarea.usuario_responsable_id])
    if getattr(tarea, "responde_por_id", None):
        # Actividad del equipo con una persona que RESPONDE: los avisos son para ella, no para todo el equipo.
        return personas([tarea.responde_por_id])
    return miembros_de(tarea.equipo_responsable_id)


def de_aprobacion(aprobacion):
    if aprobacion.aprobador_usuario_id:
        return personas([aprobacion.aprobador_usuario_id])
    return miembros_de(aprobacion.aprobador_equipo_id)


def aprobaciones_en_turno(esquema):
    """Las participaciones PENDIENTES que ya se pueden decidir: en SECUENCIAL solo la de menor orden; en PARALELA
    todas (la regla de `apps.aprobaciones.autorizacion`)."""
    from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion

    if esquema.resultado is not None:
        return []
    pendientes = list(esquema.participaciones.filter(estado=Aprobacion.Estado.PENDIENTE).order_by("orden", "pk"))
    if esquema.modo == EsquemaAprobacion.Modo.SECUENCIAL:
        return pendientes[:1]
    return pendientes


# --- Gacetas ------------------------------------------------------------------------------------------------


def revisores_gaceta(gaceta):
    """Quienes comentan en la gaceta (los antiguos «aprobadores» de una configuración congelada incluidos)."""
    from apps.gacetas.autorizacion import revisores_de

    return de_actores(revisores_de(gaceta))


def coordinadores_gaceta(gaceta):
    """El coordinador congelado (persona o equipo); sin él coordina quien atiende el ticket (su responsable)."""
    actor = (gaceta.configuracion or {}).get("coordinador")
    if actor:
        return de_actores([actor])
    return responsable(gaceta.ticket)
