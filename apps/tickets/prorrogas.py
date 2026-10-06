"""Prórrogas de la fecha objetivo de un Ticket — Sprint 4.A2.

Una prórroga pertenece al TICKET y solo modifica `Ticket.fecha_objetivo_vigente`;
`fecha_objetivo_original` nunca cambia. No toca el Workflow (ni fase, bloque,
transición, pausa o reanudación), ni las Tareas, ni `EntregaTicket.vence_en`
(ese plazo tiene otra semántica).

Política CONGELADA en el Ticket (`prorroga_politica`):

- `SIN_APROBACION`: la solicitud nace APROBADA y mueve la fecha vigente en la
  misma transacción. No hay una aprobación artificial: `resuelta_por` queda
  NULL (la resolvió el Sistema por política).
- `CON_APROBACION`: la solicitud queda PENDIENTE y crea un `EsquemaAprobacion`
  de `apps.aprobaciones` con el aprobador congelado en el Ticket (usuario o
  equipo). La relación vive aquí (`ProrrogaTicket.esquema_aprobacion`);
  aprobaciones no conoce a Tickets. El aprobador decide con la pantalla de
  Aprobaciones de siempre y su decisión llega por `resolver_prorroga_por_aprobacion`.

Concurrencia: todas las operaciones bloquean primero la fila del Ticket, luego la
prórroga y, si aplica, el esquema de aprobación (orden único Ticket → Prórroga →
Esquema → Aprobaciones, igual en solicitar, resolver y cancelar, de modo que no
hay interbloqueo). Como mucho UNA prórroga PENDIENTE por Ticket: el lock lo
serializa y un constraint parcial lo respalda.

Trazabilidad: cada operación escribe `HistorialTicket` (con datos estructurados)
y `RegistroAuditoria`.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.aprobaciones.models import Aprobacion
from apps.aprobaciones.operaciones import anular_esquema_aprobacion, crear_esquema_aprobacion, resolver_aprobacion
from apps.catalogo.models import Servicio
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets import historial
from apps.tickets.autorizacion import es_responsable_actual, motivo_no_elegible_para_prorroga
from apps.tickets.models import HistorialTicket, ProrrogaTicket, Ticket

LIMITE_MOTIVO = 2000


def _iso(momento):
    return momento.isoformat() if momento is not None else None


def _auditar(instancia, actor, accion, anterior, nuevo):
    registrar_evento(
        accion=accion, instancia=instancia,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=nuevo,
    )


def _mover_fecha_vigente(ticket, prorroga, actor):
    """Única escritura de `fecha_objetivo_vigente`. `Ticket.save` rechaza
    cualquier intento de tocar la original."""
    anterior = ticket.fecha_objetivo_vigente
    ticket.fecha_objetivo_vigente = prorroga.nueva_fecha_solicitada
    ticket.save(update_fields=["fecha_objetivo_vigente", "actualizado_en"])
    _auditar(
        ticket, actor, RegistroAuditoria.Accion.ACTUALIZAR,
        {"fecha_objetivo_vigente": _iso(anterior)},
        {"fecha_objetivo_vigente": _iso(ticket.fecha_objetivo_vigente), "prorroga_id": prorroga.pk},
    )


def _datos_historial(prorroga, **extra):
    return {
        "prorroga_id": prorroga.pk,
        "numero": prorroga.numero,
        "fecha_objetivo_anterior": _iso(prorroga.fecha_objetivo_vigente_al_solicitar),
        "nueva_fecha": _iso(prorroga.nueva_fecha_solicitada),
        **extra,
    }


def _bloquear(prorroga_pk):
    """Ticket → Prórroga, siempre en ese orden."""
    ticket_id = ProrrogaTicket.objects.values_list("ticket_id", flat=True).get(pk=prorroga_pk)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    prorroga = ProrrogaTicket.objects.select_for_update().get(pk=prorroga_pk)
    return ticket, prorroga


@transaction.atomic
def solicitar_prorroga(ticket, actor, *, nueva_fecha, motivo):
    """Solicita mover la fecha objetivo vigente de `ticket` a `nueva_fecha`.

    Exige: responsable del ticket (`PermissionDenied` si no), ticket EN_ATENCION
    con fecha objetivo, política congelada que la permita, ninguna otra prórroga
    PENDIENTE y una nueva fecha posterior a la vigente (`ValidationError` en
    cada caso). Con SIN_APROBACION queda APROBADA y la fecha vigente cambia ya;
    con CON_APROBACION queda PENDIENTE para su aprobador."""
    ticket_original = ticket
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if not es_responsable_actual(actor, ticket):
        raise PermissionDenied("Solo quien atiende este ticket puede solicitar una prórroga.")
    bloqueo = motivo_no_elegible_para_prorroga(ticket)
    if bloqueo is not None:
        raise ValidationError(bloqueo)

    motivo = (motivo or "").strip()
    if not motivo:
        raise ValidationError("Indica el motivo de la prórroga.")
    if len(motivo) > LIMITE_MOTIVO:
        raise ValidationError(f"El motivo no puede superar {LIMITE_MOTIVO} caracteres.")
    if nueva_fecha is None or timezone.is_naive(nueva_fecha):
        raise ValidationError("Indica una nueva fecha válida.")
    vigente = ticket.fecha_objetivo_vigente
    if nueva_fecha <= vigente:
        raise ValidationError(
            "La nueva fecha debe ser posterior a la fecha objetivo vigente "
            f"({timezone.localtime(vigente):%d/%m/%Y %H:%M})."
        )

    politica = ticket.prorroga_politica
    esquema = None
    if politica == Servicio.PoliticaProrroga.CON_APROBACION:
        if ticket.prorroga_aprobador_usuario_id is not None:
            participante = (Aprobacion.TipoAprobador.USUARIO, ticket.prorroga_aprobador_usuario)
        elif ticket.prorroga_aprobador_equipo_id is not None:
            participante = (Aprobacion.TipoAprobador.EQUIPO, ticket.prorroga_aprobador_equipo)
        else:
            raise ValidationError("Este ticket no tiene un aprobador de prórrogas configurado.")
        esquema = crear_esquema_aprobacion(modo="SECUENCIAL", participantes=[participante], actor=actor)

    ahora = timezone.now()
    numero = (ticket.prorrogas.aggregate(maximo=Max("numero"))["maximo"] or 0) + 1
    automatica = politica == Servicio.PoliticaProrroga.SIN_APROBACION
    try:
        with transaction.atomic():
            prorroga = ProrrogaTicket.objects.create(
                ticket=ticket, numero=numero, politica=politica, solicitada_por=actor, solicitada_en=ahora,
                fecha_objetivo_vigente_al_solicitar=vigente, nueva_fecha_solicitada=nueva_fecha, motivo=motivo,
                estado=ProrrogaTicket.Estado.APROBADA if automatica else ProrrogaTicket.Estado.PENDIENTE,
                esquema_aprobacion=esquema, resuelta_en=ahora if automatica else None,
            )
    except IntegrityError as exc:  # respaldo del lock: constraint de una sola PENDIENTE
        raise ValidationError("Ya hay una prórroga pendiente de resolver para este ticket.") from exc

    _auditar(
        prorroga, actor, RegistroAuditoria.Accion.CREAR, None,
        {
            "ticket_id": ticket.pk, "numero": numero, "politica": politica, "estado": prorroga.estado,
            "fecha_objetivo_vigente_al_solicitar": _iso(vigente), "nueva_fecha_solicitada": _iso(nueva_fecha),
            "motivo": motivo,
        },
    )
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.PRORROGA_SOLICITADA, actor, **_datos_historial(prorroga, motivo=motivo, politica=politica)
    )
    if automatica:
        _mover_fecha_vigente(ticket, prorroga, actor)
        historial.registrar(
            ticket, HistorialTicket.TipoEvento.PRORROGA_APROBADA, None,
            **_datos_historial(prorroga, causa="POLITICA_SIN_APROBACION"),
        )
    ticket_original.refresh_from_db()
    return prorroga


@transaction.atomic
def resolver_prorroga_por_aprobacion(aprobacion, actor, *, decision, observacion=""):
    """Decide la `Aprobacion` de una prórroga CON_APROBACION y, en la misma
    transacción, aplica (o no) la nueva fecha. Única forma correcta de decidir
    una aprobación de prórroga: `resolver_aprobacion` a secas dejaría la
    prórroga PENDIENTE para siempre.

    Solo se aprueba o se rechaza (`DEVUELTA` no existe para una prórroga). Se
    aprueba únicamente con el ticket aún EN_ATENCION; rechazar siempre es
    posible, para no dejar solicitudes huérfanas. La autorización es la de la
    propia Aprobación (`puede_aprobar`, RN-025)."""
    if decision not in (Aprobacion.Estado.APROBADA, Aprobacion.Estado.RECHAZADA):
        raise ValidationError("Una prórroga solo puede aprobarse o rechazarse.")
    prorroga_pk = (
        ProrrogaTicket.objects.filter(esquema_aprobacion_id=aprobacion.esquema_id).values_list("pk", flat=True).first()
    )
    if prorroga_pk is None:
        raise ValueError("Este esquema de aprobación no corresponde a ninguna prórroga de ticket.")
    ticket, prorroga = _bloquear(prorroga_pk)
    if prorroga.estado != ProrrogaTicket.Estado.PENDIENTE:
        raise ValidationError("Esta prórroga ya fue resuelta o cancelada: no puede decidirse de nuevo.")
    aprobar = decision == Aprobacion.Estado.APROBADA
    if aprobar:
        if ticket.estado != Ticket.Estado.EN_ATENCION:
            raise ValidationError("El ticket ya no está en atención: la prórroga no puede aprobarse.")
        if prorroga.nueva_fecha_solicitada <= ticket.fecha_objetivo_vigente:
            raise ValidationError("La fecha solicitada ya no es posterior a la fecha objetivo vigente.")

    _, esquema = resolver_aprobacion(aprobacion, actor, decision=decision, observacion=observacion)
    if esquema.resultado is None:  # no ocurre con un único aprobador; defensa ante un esquema ajeno
        return prorroga

    prorroga.estado = ProrrogaTicket.Estado.APROBADA if aprobar else ProrrogaTicket.Estado.RECHAZADA
    prorroga.resuelta_por = actor
    prorroga.resuelta_en = timezone.now()
    prorroga.observaciones_resolucion = observacion or ""
    prorroga.save(update_fields=["estado", "resuelta_por", "resuelta_en", "observaciones_resolucion", "actualizado_en"])
    _auditar(
        prorroga, actor, RegistroAuditoria.Accion.ACTUALIZAR,
        {"estado": ProrrogaTicket.Estado.PENDIENTE},
        {"estado": prorroga.estado, "observaciones_resolucion": prorroga.observaciones_resolucion},
    )
    if aprobar:
        _mover_fecha_vigente(ticket, prorroga, actor)
    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.PRORROGA_APROBADA if aprobar else HistorialTicket.TipoEvento.PRORROGA_RECHAZADA,
        actor,
        **_datos_historial(prorroga, observaciones=prorroga.observaciones_resolucion),
    )
    return prorroga


@transaction.atomic
def cancelar_prorroga(prorroga, actor, *, motivo=""):
    """Cancela una solicitud PENDIENTE. Solo quien la solicitó (`PermissionDenied`
    si no); una prórroga ya aprobada, rechazada o cancelada no se cancela
    (`ValidationError`). No cambia la fecha vigente y retira la aprobación
    pendiente para que el aprobador ya no la vea."""
    ticket, prorroga = _bloquear(prorroga.pk)
    if prorroga.solicitada_por_id != getattr(actor, "pk", None):
        raise PermissionDenied("Solo quien solicitó la prórroga puede cancelarla.")
    if prorroga.estado != ProrrogaTicket.Estado.PENDIENTE:
        raise ValidationError("Solo puede cancelarse una prórroga pendiente.")

    if prorroga.esquema_aprobacion_id is not None:
        anular_esquema_aprobacion(prorroga.esquema_aprobacion, actor)
    prorroga.estado = ProrrogaTicket.Estado.CANCELADA
    prorroga.resuelta_por = actor
    prorroga.resuelta_en = timezone.now()
    prorroga.observaciones_resolucion = (motivo or "").strip()
    prorroga.save(update_fields=["estado", "resuelta_por", "resuelta_en", "observaciones_resolucion", "actualizado_en"])
    _auditar(
        prorroga, actor, RegistroAuditoria.Accion.ACTUALIZAR,
        {"estado": ProrrogaTicket.Estado.PENDIENTE},
        {"estado": prorroga.estado, "observaciones_resolucion": prorroga.observaciones_resolucion},
    )
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.PRORROGA_CANCELADA, actor,
        **_datos_historial(prorroga, observaciones=prorroga.observaciones_resolucion),
    )
    return prorroga
