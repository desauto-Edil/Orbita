"""Resolución de actores de definición a responsables reales (4.3).

Se resuelven al entrar en la etapa; las Tareas/Aprobaciones ya creadas
conservan su asignación y sus propias operaciones de reasignación.
No cambia la responsabilidad del Ticket ni la policy de Entregables.
"""

from django.core.exceptions import ValidationError


ACTORES_DINAMICOS = frozenset({"RESPONSABLE_TICKET", "SOLICITANTE"})
ACTORES = frozenset({"USUARIO", "EQUIPO"}) | ACTORES_DINAMICOS


def validar_actor(tipo, *, usuario=None, equipo=None, permite_vacio=False):
    """Contrato común para configuración y resolución, sin inferir referencias."""
    if tipo == "USUARIO" and usuario is not None and equipo is None:
        return
    if tipo == "EQUIPO" and equipo is not None and usuario is None:
        return
    if (tipo in ACTORES_DINAMICOS or (permite_vacio and tipo == "")) and usuario is None and equipo is None:
        return
    raise ValidationError("Actor inválido o referencias incompatibles con su tipo.")


def _ticket_vigente(instancia):
    from apps.tickets.models import Ticket

    # Relación canónica existente: Ticket.instancia_workflow. No nueva FK.
    ticket = Ticket.objects.select_for_update(of=("self",)).filter(instancia_workflow=instancia).first()
    if ticket is not None:
        return ticket

    # iniciar_workflow ejecuta INICIO y las siguientes etapas ANTES de
    # retornar a radicar_ticket, que entonces guarda la relación. Solo en
    # ese arranque se usa el ticket_id que la radicación ya proporcionaba.
    ticket_id = instancia.contexto.get("datos_iniciales", {}).get("ticket_id")
    if not isinstance(ticket_id, int) or isinstance(ticket_id, bool):
        raise ValidationError("Este actor necesita una ejecución vinculada a un Ticket.")
    ticket = Ticket.objects.select_for_update(of=("self",)).filter(
        pk=ticket_id, estado=Ticket.Estado.BORRADOR, instancia_workflow__isnull=True,
        detalle_servicio__servicio__workflow_id=instancia.workflow_version.workflow_id,
    ).first()
    if ticket is None:
        raise ValidationError("El Ticket no corresponde al inicio de esta ejecución.")
    return ticket


def resolver_actor(tipo, *, instancia, usuario=None, equipo=None, permite_vacio=False):
    """Devuelve (usuario, equipo), nunca expande un equipo a todos sus miembros.

    Se llama dentro de la transacción del motor. El lock del Ticket evita
    leer una responsabilidad que está siendo reasignada simultáneamente.
    Un responsable individual ausente es un error explícito, no un equipo
    ni una Tarea sin asignar inventados por el resolutor.
    """
    validar_actor(tipo, usuario=usuario, equipo=equipo, permite_vacio=permite_vacio)
    if tipo in ACTORES_DINAMICOS:
        ticket = _ticket_vigente(instancia)
        usuario = ticket.solicitante if tipo == "SOLICITANTE" else ticket.usuario_responsable
        if usuario is None:
            raise ValidationError("El Ticket todavía no tiene un responsable individual asignado.")
    return usuario, equipo
