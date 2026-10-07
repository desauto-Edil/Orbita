"""Código público del Ticket (4.F2): `TCK-000123`.

El código es la cara visible del Ticket; la PK y el UUID `radicado` siguen siendo las
referencias internas. Se asigna UNA vez, al radicar, desde un contador con lock
(`ConsecutivoTicket`): único, estable, sin huecos y no editable. No hay ningún otro punto
que genere o cambie el número."""

import re

from django.db import transaction

from apps.tickets.models import ConsecutivoTicket

PREFIJO = "TCK"
DIGITOS = 6
_PATRON = re.compile(rf"^\s*{PREFIJO}[-\s]?0*(\d+)\s*$", re.IGNORECASE)


def formatear(consecutivo):
    """`TCK-000123` para un consecutivo, `None` si el ticket todavía no tiene uno."""
    if consecutivo is None:
        return None
    return f"{PREFIJO}-{consecutivo:0{DIGITOS}d}"


def consecutivo_desde_codigo(texto):
    """Número de un código escrito por una persona (`tck-123`, `TCK-000123`, `TCK 123`);
    `None` si el texto no tiene la forma de un código. Para buscar un ticket por su código."""
    coincidencia = _PATRON.match(texto or "")
    return int(coincidencia.group(1)) if coincidencia else None


@transaction.atomic
def siguiente_consecutivo():
    """Reserva el siguiente número. Debe llamarse dentro de la transacción de la radicación:
    el lock de la fila del contador se mantiene hasta que esta termina."""
    contador, _creado = ConsecutivoTicket.objects.select_for_update().get_or_create(pk=1)
    contador.ultimo += 1
    contador.save(update_fields=["ultimo"])
    return contador.ultimo
