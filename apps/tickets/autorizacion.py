"""Autorización de Tickets — incremento 2.1.

Solo relación solicitante↔Ticket (corrección del usuario sobre la
propuesta original): un borrador se administra exclusivamente por quién lo
creó, nunca por la visibilidad vigente del Servicio de origen — esa solo
se valida una vez, al crear (`apps.tickets.operaciones.crear_borrador`). Un
cambio posterior en la visibilidad del catálogo no debe hacer que el
solicitante pierda acceso a su propio borrador.

`tickets.atender` (permiso funcional para atención de tickets ajenos) no
existe todavía — no hay atención en 2.1, llega en 2.3.
"""


def es_propietario_borrador(usuario, ticket):
    return bool(getattr(usuario, "is_authenticated", False)) and ticket.solicitante_id == usuario.id
