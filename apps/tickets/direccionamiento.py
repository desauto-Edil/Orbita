"""Direccionamiento del Ticket General (Sprint 4.C2).

Un Ticket General nace sin destinatario: el usuario elige —mientras es BORRADOR—
a quién quiere dirigir su solicitud (un destino configurado en
`apps.catalogo.destinos_ticket_general`), o la deja sin elegir si existe un
destino de reserva. Al RADICAR se resuelve el destino, se asigna el responsable
inicial del destino al Ticket (`usuario_responsable` o `equipo_responsable`) y se
FIJA la foto del destino (`DireccionamientoTicket`).

Direccionar NO es iniciar la atención: el ticket sigue RADICADO. Pasa a EN_ATENCION
solo por las operaciones de siempre (`tomar_ticket`, `asignar_ticket`) o por
`iniciar_atencion_ticket` cuando el responsable ya es una persona concreta.

`asignar_ticket` no se usa aquí: dispara RADICADO→EN_ATENCION al asignar un
usuario y su contrato no cambia. Las funciones de este módulo no escriben el
Ticket (lo hace `radicar_ticket`, que ya lo tiene bloqueado) ni deciden permisos
de atención (`apps.tickets.autorizacion`).

Concurrencia: `preparar` bloquea la fila del destino (`FOR UPDATE`) antes de
validarlo. Las operaciones administrativas bloquean configuración → destino y
`radicar_ticket` bloquea ticket → destino, sin tomar nunca la configuración, así
que no hay ciclos: radicar y desactivar/cambiar el responsable se serializan, y
el ticket queda con el destino y el responsable de uno de los dos órdenes
posibles, nunca con una mezcla.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalogo import destinos_ticket_general as destinos
from apps.catalogo.models import ConfiguracionTicketGeneral, DestinoTicketGeneral
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets import historial
from apps.tickets.autorizacion import es_propietario_borrador
from apps.tickets.models import DireccionamientoTicket, HistorialTicket, Ticket

MENSAJE_ELEGIR = "Selecciona a quién diriges tu solicitud."
MENSAJE_NO_DISPONIBLE = "El destino que elegiste ya no está disponible. Elige otro."


def es_ticket_general(ticket):
    return bool(ticket.detalle_servicio.servicio.es_ticket_general)


def direccionamiento_de(ticket):
    """La fila de direccionamiento del ticket (selección o foto), o `None`."""
    return DireccionamientoTicket.objects.filter(ticket=ticket).select_related("destino").first()


@transaction.atomic
def seleccionar_destino_borrador(ticket, actor, destino_id):
    """Guarda la selección del usuario en su borrador de Ticket General. `destino_id`
    vacío/`None` = "No estoy seguro" (válido solo si hay destino de reserva, lo cual
    se exige al radicar). Solo el solicitante, solo en BORRADOR, y solo un destino
    activo y utilizable hoy."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if not es_propietario_borrador(actor, ticket):
        raise PermissionDenied("Solo el solicitante puede elegir el destino de este ticket.")
    if ticket.estado != Ticket.Estado.BORRADOR:
        raise ValidationError("El destino solo puede elegirse mientras el ticket es un borrador.")
    if not es_ticket_general(ticket):
        raise ValidationError("Solo el ticket general se dirige a un destino.")
    destino = None
    if destino_id not in (None, ""):
        try:
            destino = DestinoTicketGeneral.objects.get(pk=int(destino_id))
        except (ValueError, TypeError, DestinoTicketGeneral.DoesNotExist):
            raise ValidationError(MENSAJE_NO_DISPONIBLE)
        if not destinos.es_utilizable(destino):
            raise ValidationError(MENSAJE_NO_DISPONIBLE)
    fila = DireccionamientoTicket.objects.filter(ticket=ticket).first()
    if fila is None:
        if destino is None:
            return None
        fila = DireccionamientoTicket(ticket=ticket)
    fila.destino = destino
    fila.save()
    return fila


def _resolver(ticket, *, bloquear):
    """`(destino, es_predeterminado)` con el que se radicaría `ticket`, validado
    contra el dominio real. `bloquear=True` toma el lock de la fila del destino
    (dentro de `radicar_ticket`)."""
    fila = DireccionamientoTicket.objects.filter(ticket=ticket).first()
    if fila is not None and fila.destino_id is not None:
        destino_id, es_predeterminado, mensaje = fila.destino_id, False, MENSAJE_NO_DISPONIBLE
    else:
        destino_id = ConfiguracionTicketGeneral.actual().destino_predeterminado_id
        es_predeterminado, mensaje = True, MENSAJE_ELEGIR
        if destino_id is None:
            raise ValidationError(MENSAJE_ELEGIR)
    consulta = DestinoTicketGeneral.objects.filter(pk=destino_id)
    if bloquear:
        consulta = consulta.select_for_update()
    destino = consulta.first()
    if destino is None or not destinos.es_utilizable(destino):
        raise ValidationError(mensaje)
    return destino, es_predeterminado


def error_de_destino(ticket):
    """Mensaje de por qué `ticket` no podría radicarse por su destino (para
    avisar en la revisión antes de enviar), o `None`. Solo lectura."""
    try:
        _resolver(ticket, bloquear=False)
    except ValidationError as exc:
        return "; ".join(exc.messages)
    return None


def preparar(ticket):
    """Dentro de `radicar_ticket` (con el ticket bloqueado): resuelve y valida el
    destino bajo lock y deja el responsable inicial en el ticket EN MEMORIA
    (`radicar_ticket` lo guarda). Devuelve `(destino, es_predeterminado)`; levanta
    `ValidationError` si no hay un destino/responsable válido: un Ticket General
    nunca se radica sin poder determinar quién lo atiende."""
    destino, es_predeterminado = _resolver(ticket, bloquear=True)
    ticket.usuario_responsable = destino.responsable_usuario
    ticket.equipo_responsable = destino.responsable_equipo
    return destino, es_predeterminado


def fijar(ticket, actor, destino, es_predeterminado):
    """Con el ticket ya RADICADO y guardado: congela la foto del destino, anota el
    direccionamiento en el historial y lo audita. El estado no cambia."""
    objeto = destino.objeto
    fila = DireccionamientoTicket.objects.filter(ticket=ticket).first() or DireccionamientoTicket(ticket=ticket)
    fila.destino = destino
    fila.tipo = destino.tipo
    fila.referencia_id = objeto.pk
    fila.etiqueta = destino.etiqueta[:200]
    fila.es_predeterminado = es_predeterminado
    fila.fijado_en = timezone.now()
    fila.save()
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.DIRECCIONADO, actor,
        destino_tipo=destino.tipo, destino_id=destino.pk, destino_etiqueta=fila.etiqueta,
        es_predeterminado=es_predeterminado,
        responsable_usuario_id=destino.responsable_usuario_id,
        responsable_equipo_id=destino.responsable_equipo_id,
        responsable_etiqueta=destino.responsable_etiqueta,
    )
    # `serializar` omite los campos no editables (la foto): se audita explícito.
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=fila,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_nuevos={
            "ticket_id": ticket.pk, "destino_id": destino.pk, "tipo": fila.tipo,
            "referencia_id": fila.referencia_id, "etiqueta": fila.etiqueta,
            "es_predeterminado": es_predeterminado,
        },
    )
    return fila
