"""State de Ticket — incrementos 2.3 (CU-017) y 2.5 (CU-019). RN-018: "Los
cambios de estado solo podrán realizarse mediante transiciones permitidas".

Única fuente de verdad de qué transiciones de estado son válidas: ninguna
vista ni operación de `apps.tickets.operaciones` compara `ticket.estado`
directamente para decidir si una transición es válida — todas consultan
esta tabla. Tabla compacta (`TRANSICIONES` + dos funciones), no una clase
por estado: ninguna transición tiene comportamiento propio más allá de su
estado destino, ni en 2.5 (7 entradas en total no justifican un
`EstadoResuelto`/`EstadoCerrado`/... por transición).

Solo dos entradas producen RADICADO→EN_ATENCION: `TOMAR` (autoasignación)
y `ASIGNAR_USUARIO` (un tercero asigna un `usuario_responsable` por primera
vez). `REASIGNAR` y "asignar solo `equipo_responsable`, sin usuario" no
aparecen aquí — no cambian de estado (decisión explícita del usuario: no
existe una falsa transición EN_ATENCION→EN_ATENCION) — `operaciones.py` las
ejecuta sin pasar por esta tabla.

**2.5** (V1 aprobado — máquina de estados final, sin las transiciones
marcadas con "¿?" en la propuesta): `CANCELAR` desde RADICADO o EN_ATENCION;
`RESOLVER` solo desde EN_ATENCION; `CERRAR`/`REABRIR` solo desde RESUELTO.
Deliberadamente **ausentes** (no "¿?" pendientes, sino decisiones ya
cerradas): `RESUELTO→CANCELADO`, `CERRADO→EN_ATENCION` (CERRADO no se
reabre en V1) y cualquier salida desde CANCELADO (terminal). `estados.py`
responde únicamente "¿existe esta transición?" — la autorización ("¿puede
ESTE usuario ejecutarla?") es responsabilidad separada de
`apps.tickets.autorizacion`.
"""

from django.core.exceptions import ValidationError

from apps.tickets.models import Ticket

TRANSICIONES = {
    (Ticket.Estado.RADICADO, "TOMAR"): Ticket.Estado.EN_ATENCION,
    (Ticket.Estado.RADICADO, "ASIGNAR_USUARIO"): Ticket.Estado.EN_ATENCION,
    (Ticket.Estado.RADICADO, "CANCELAR"): Ticket.Estado.CANCELADO,
    (Ticket.Estado.EN_ATENCION, "CANCELAR"): Ticket.Estado.CANCELADO,
    (Ticket.Estado.EN_ATENCION, "RESOLVER"): Ticket.Estado.RESUELTO,
    (Ticket.Estado.RESUELTO, "CERRAR"): Ticket.Estado.CERRADO,
    (Ticket.Estado.RESUELTO, "REABRIR"): Ticket.Estado.EN_ATENCION,
}


def puede_ejecutar(ticket, accion):
    return (ticket.estado, accion) in TRANSICIONES


def exigir_transicion(ticket, accion):
    """Muta `ticket.estado` en memoria (sin guardar) al estado resultante
    de `accion` y lo devuelve. Lanza `ValidationError` (RN-018) si no hay
    una transición definida para el estado actual del ticket."""
    nuevo_estado = TRANSICIONES.get((ticket.estado, accion))
    if nuevo_estado is None:
        raise ValidationError(
            f"No es posible ejecutar '{accion}' desde el estado {ticket.get_estado_display()}."
        )
    ticket.estado = nuevo_estado
    return ticket
