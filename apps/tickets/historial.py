"""Bitácora operacional de Tickets — incremento 2.3 (CU-016, RQF-060).

Único punto que escribe `HistorialTicket`: un helper cohesivo, no Domain
Events/Observer. `HistorialTicket` sigue siendo el único consumidor real de
las operaciones de atención (radicar/tomar/asignar/reasignar) — introducir
clases de evento y un dispatcher para despachar a un único manejador local
no reduciría acoplamiento hoy (decisión ya evaluada y rechazada en la
propuesta de 2.3). Se reevalúa cuando SLA/Notificaciones (Sprint 8) aporten
un segundo consumidor real.
"""

from apps.tickets.models import HistorialTicket


def registrar(ticket, tipo_evento, actor, **datos):
    return HistorialTicket.objects.create(
        ticket=ticket,
        tipo_evento=tipo_evento,
        actor=actor,
        datos=datos or None,
    )
