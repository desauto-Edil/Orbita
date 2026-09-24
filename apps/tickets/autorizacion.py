"""Autorización de Tickets — incrementos 2.1 y 2.2.

Solo relación solicitante↔Ticket (corrección del usuario sobre la
propuesta original): un ticket (borrador o ya radicado) se administra/
consulta exclusivamente por quién lo creó, nunca por la visibilidad
vigente del Servicio de origen — esa solo se valida una vez, al crear
(`apps.tickets.operaciones.crear_borrador`). Un cambio posterior en la
visibilidad del catálogo no debe hacer que el solicitante pierda acceso a
su propio ticket (RQF-049/2.1) — mismo criterio se extiende a radicar
(2.2, ver `operaciones.radicar_ticket`).

2.2 reutiliza `es_propietario_borrador` también para el detalle de solo
lectura de un ticket YA radicado (`views.detalle_view`) — el nombre queda
como está (el resto del código y las pruebas ya lo usan así), pero su
alcance real es "es el solicitante de este ticket", no solo "de este
borrador".

`tickets.atender` (permiso funcional para atención de tickets ajenos) no
existe todavía — no hay atención en 2.1/2.2, llega en 2.3.
"""


def es_propietario_borrador(usuario, ticket):
    return bool(getattr(usuario, "is_authenticated", False)) and ticket.solicitante_id == usuario.id
