"""Cancelación con aprobación de un Ticket — requerimiento nuevo (CU por asignar; extiende CU-019/RQF-059).

Quién PIDE cancelar no cambia: el solicitante o el responsable actual (`puede_cancelar_ticket`). Lo que cambia es
quién la ACEPTA cuando el Servicio/Proceso lo configuró (Studio › Básico, `Servicio.cancelacion_con_aprobacion`,
congelado en el Ticket al crearlo):

- Sin aprobación (lo de siempre, y todos los tickets anteriores): `solicitar_cancelacion` cancela en el acto con
  `operaciones.cancelar_ticket`.
- Con aprobación: nace una `SolicitudCancelacion` PENDIENTE con un esquema de `apps.aprobaciones` para el
  aprobador congelado (usuario o equipo, igual que la prórroga). El aprobador decide en el espacio del ticket y su
  decisión llega por `resolver_cancelacion_por_aprobacion` (`aprobaciones.decisiones.aplicar`): APROBADA cancela el
  ticket con la operación de siempre (detiene flujo, tareas, aprobaciones, gacetas y prórroga en UNA transacción);
  RECHAZADA lo deja tal como estaba. Quien la pidió puede retirarla (`retirar_cancelacion`).

CONGELADO: mientras hay una solicitud PENDIENTE nadie avanza el trabajo del ticket. Lo hacen cumplir las
operaciones que mueven el trabajo llamando a `exigir_sin_cancelacion_pendiente` (tomar, asignar, reasignar,
resolver, entregar, pedir información o prórroga, ítems y evidencias, datos de inicio, entregables, revisión por
conjunto, completar actividades del flujo, decidir sus aprobaciones y operar sus gacetas). La conversación sigue
abierta: hablar del ticket no lo avanza.

Locks: instancia → Ticket → solicitud → esquema/aprobación (el mismo orden de `cancelar_ticket`).
Trazabilidad: `HistorialTicket` (CANCELACION_SOLICITADA / _RECHAZADA / _RETIRADA; aprobarla registra CANCELADO) y
`RegistroAuditoria`; nada se borra.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.aprobaciones.models import Aprobacion
from apps.aprobaciones.operaciones import anular_esquema_aprobacion, crear_esquema_aprobacion, resolver_aprobacion
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets import historial
from apps.tickets.models import HistorialTicket, SolicitudCancelacion, Ticket

LIMITE_MOTIVO = 2000
MENSAJE_CONGELADO = (
    "Hay una cancelación de este ticket esperando la decisión de su aprobador: mientras tanto el trabajo queda detenido."
)


# --- Lectura ------------------------------------------------------------------------------------------------------


def pendiente_de(ticket):
    """La `SolicitudCancelacion` PENDIENTE del ticket, o `None`."""
    ticket_id = getattr(ticket, "pk", ticket)
    if ticket_id is None:
        return None
    return (
        SolicitudCancelacion.objects.filter(ticket_id=ticket_id, estado=SolicitudCancelacion.Estado.PENDIENTE)
        .select_related("solicitada_por").first()
    )


def esta_congelado(ticket):
    ticket_id = getattr(ticket, "pk", ticket)
    return ticket_id is not None and SolicitudCancelacion.objects.filter(
        ticket_id=ticket_id, estado=SolicitudCancelacion.Estado.PENDIENTE
    ).exists()


def exigir_sin_cancelacion_pendiente(ticket):
    """Regla de dominio del congelamiento: rechaza avanzar el trabajo de un ticket con una cancelación por aprobar.
    Acepta el Ticket o su pk."""
    if esta_congelado(ticket):
        raise ValidationError(MENSAJE_CONGELADO)


def aprobador_de(ticket):
    """El aprobador congelado del ticket (usuario o equipo), o `None` si cancelar es directo."""
    if not ticket.cancelacion_con_aprobacion:
        return None
    return ticket.cancelacion_aprobador_usuario or ticket.cancelacion_aprobador_equipo


def solicitud_del_esquema(esquema):
    return getattr(esquema, "cancelacion", None)


# --- Escritura -----------------------------------------------------------------------------------------------------


def _auditar(instancia, actor, accion, anterior, nuevo):
    registrar_evento(
        accion=accion, instancia=instancia,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=nuevo,
    )


def _bloquear_ticket(ticket_pk):
    from apps.tickets.operaciones import _bloquear_instancia_del_ticket

    _bloquear_instancia_del_ticket(ticket_pk)
    return Ticket.objects.select_for_update().get(pk=ticket_pk)


@transaction.atomic
def solicitar_cancelacion(ticket, actor, motivo):
    """Pide cancelar `ticket`. Sin aprobación lo cancela ya (devuelve el Ticket); con aprobación crea la solicitud
    PENDIENTE para el aprobador congelado y congela el ticket (devuelve la `SolicitudCancelacion`)."""
    from apps.tickets.autorizacion import puede_cancelar_ticket
    from apps.tickets.operaciones import cancelar_ticket

    if not ticket.cancelacion_con_aprobacion:
        return cancelar_ticket(ticket, actor, motivo)

    ticket = _bloquear_ticket(ticket.pk)
    if ticket.estado not in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION):
        raise ValidationError("Solo un ticket RADICADO o EN_ATENCION puede cancelarse.")
    if not puede_cancelar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para cancelar este ticket.")
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValidationError("El motivo de cancelación no puede estar vacío.")
    if len(motivo) > LIMITE_MOTIVO:
        raise ValidationError(f"El motivo no puede superar {LIMITE_MOTIVO} caracteres.")
    if esta_congelado(ticket):
        raise ValidationError("Ya hay una cancelación de este ticket esperando la decisión de su aprobador.")
    if ticket.cancelacion_aprobador_usuario_id is not None:
        aprobador = ticket.cancelacion_aprobador_usuario
        if not aprobador.is_active:
            raise ValidationError("El aprobador de cancelaciones de este ticket ya no está activo.")
        participante = (Aprobacion.TipoAprobador.USUARIO, aprobador)
    else:
        aprobador = ticket.cancelacion_aprobador_equipo
        if not aprobador.activo:
            raise ValidationError("El equipo aprobador de cancelaciones de este ticket ya no está activo.")
        participante = (Aprobacion.TipoAprobador.EQUIPO, aprobador)

    esquema = crear_esquema_aprobacion(modo="SECUENCIAL", participantes=[participante], actor=actor)
    try:
        with transaction.atomic():
            solicitud = SolicitudCancelacion.objects.create(
                ticket=ticket, solicitada_por=actor, motivo=motivo, esquema_aprobacion=esquema,
            )
    except IntegrityError as exc:  # respaldo del lock: una sola PENDIENTE por ticket
        raise ValidationError("Ya hay una cancelación de este ticket esperando la decisión de su aprobador.") from exc
    _auditar(
        solicitud, actor, RegistroAuditoria.Accion.CREAR, None,
        {"ticket_id": ticket.pk, "motivo": motivo, "estado": solicitud.estado, "esquema_aprobacion_id": esquema.pk},
    )
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.CANCELACION_SOLICITADA, actor,
        solicitud_id=solicitud.pk, motivo=motivo, aprobador=str(aprobador),
    )
    return solicitud


def _cerrar(solicitud, actor, estado, observacion):
    anterior = solicitud.estado
    solicitud.estado = estado
    solicitud.resuelta_por = actor
    solicitud.resuelta_en = timezone.now()
    solicitud.observaciones_resolucion = (observacion or "").strip()
    solicitud.save(update_fields=["estado", "resuelta_por", "resuelta_en", "observaciones_resolucion", "actualizado_en"])
    _auditar(
        solicitud, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": anterior},
        {"estado": estado, "observaciones_resolucion": solicitud.observaciones_resolucion},
    )


@transaction.atomic
def resolver_cancelacion_por_aprobacion(aprobacion, actor, *, decision, observacion=""):
    """Decide la `Aprobacion` de una solicitud de cancelación. Solo aprobar o rechazar (observación obligatoria al
    rechazar, la regla de `resolver_aprobacion`). Aprobar cancela el ticket con `cancelar_ticket` en la misma
    transacción; rechazar descongela el ticket sin tocar nada más. La autorización es la de la Aprobación."""
    from apps.tickets.operaciones import cancelar_ticket

    if decision not in (Aprobacion.Estado.APROBADA, Aprobacion.Estado.RECHAZADA):
        raise ValidationError("Una cancelación solo puede aprobarse o rechazarse.")
    datos = SolicitudCancelacion.objects.filter(esquema_aprobacion_id=aprobacion.esquema_id).values_list(
        "pk", "ticket_id"
    ).first()
    if datos is None:
        raise ValueError("Este esquema de aprobación no corresponde a ninguna solicitud de cancelación.")
    solicitud_pk, ticket_pk = datos
    ticket = _bloquear_ticket(ticket_pk)
    solicitud = SolicitudCancelacion.objects.select_for_update().get(pk=solicitud_pk)
    if solicitud.estado != SolicitudCancelacion.Estado.PENDIENTE:
        raise ValidationError("Esta solicitud de cancelación ya se resolvió.")
    aprobar = decision == Aprobacion.Estado.APROBADA
    if aprobar and ticket.estado not in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION):
        raise ValidationError("El ticket ya no puede cancelarse.")

    _, esquema = resolver_aprobacion(aprobacion, actor, decision=decision, observacion=observacion)
    if esquema.resultado is None:  # un único aprobador: no ocurre; defensa ante un esquema ajeno
        return solicitud
    if aprobar:
        # Primero se cierra la solicitud (el ticket deja de estar congelado) y luego se cancela con la operación
        # de siempre: así detener las gacetas, tareas y aprobaciones no tropieza con el congelamiento.
        _cerrar(solicitud, actor, SolicitudCancelacion.Estado.APROBADA, observacion)
        cancelar_ticket(ticket, actor, solicitud.motivo, por_solicitud=solicitud)
    else:
        _cerrar(solicitud, actor, SolicitudCancelacion.Estado.RECHAZADA, observacion)
        historial.registrar(
            ticket, HistorialTicket.TipoEvento.CANCELACION_RECHAZADA, actor,
            solicitud_id=solicitud.pk, observacion=solicitud.observaciones_resolucion,
        )
    return solicitud


@transaction.atomic
def retirar_cancelacion(solicitud, actor, *, motivo=""):
    """Quien pidió la cancelación desiste mientras sigue PENDIENTE: se retira su aprobación y el ticket sigue."""
    ticket = _bloquear_ticket(solicitud.ticket_id)
    solicitud = SolicitudCancelacion.objects.select_for_update().get(pk=solicitud.pk)
    if solicitud.solicitada_por_id != getattr(actor, "pk", None):
        raise PermissionDenied("Solo quien pidió la cancelación puede retirarla.")
    if solicitud.estado != SolicitudCancelacion.Estado.PENDIENTE:
        raise ValidationError("Esta solicitud de cancelación ya se resolvió.")
    anular_esquema_aprobacion(solicitud.esquema_aprobacion, actor)
    _cerrar(solicitud, actor, SolicitudCancelacion.Estado.RETIRADA, motivo)
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.CANCELACION_RETIRADA, actor, solicitud_id=solicitud.pk,
    )
    return solicitud
