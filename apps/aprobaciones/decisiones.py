"""Aplicar la decisión de una aprobación por la vía que corresponde a su origen — un solo lugar.

Una `Aprobacion` puede venir de una prórroga (4.A2: la decisión también mueve la fecha del Ticket), de una ronda de
gaceta (Fase 3: aplica el resultado a ESE ítem en la gaceta), de un Workflow (continúa el flujo) o ser una
aprobación simple. `aplicar` elige la operación de dominio de siempre; cada una revalida permisos y estado y lanza
`ValidationError` si la decisión no procede. Lo usan la pantalla de la aprobación (`aprobaciones:decidir`) y la
revisión de piezas del espacio del ticket (`tickets:decidir_pieza`)."""

from apps.aprobaciones.operaciones import resolver_aprobacion


def prorroga_del_esquema(esquema):
    """4.A2 — la `ProrrogaTicket` que originó este esquema, o `None`."""
    return getattr(esquema, "prorroga", None)


def aplicar(aprobacion, usuario, *, decision, observacion=""):
    esquema = aprobacion.esquema
    if prorroga_del_esquema(esquema) is not None:
        from apps.tickets.prorrogas import resolver_prorroga_por_aprobacion

        return resolver_prorroga_por_aprobacion(aprobacion, usuario, decision=decision, observacion=observacion)
    if hasattr(esquema, "ronda_gaceta"):
        from apps.gacetas.operaciones import resolver_aprobacion_gaceta

        return resolver_aprobacion_gaceta(aprobacion, usuario, decision=decision, observacion=observacion)
    if hasattr(esquema, "vinculo_workflow"):
        from apps.workflows.integracion import resolver_aprobacion_workflow

        return resolver_aprobacion_workflow(aprobacion, usuario, decision=decision, observacion=observacion)
    return resolver_aprobacion(aprobacion, usuario, decision=decision, observacion=observacion)
