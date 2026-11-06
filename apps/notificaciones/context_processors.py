"""Campana del encabezado (CU-035, RQF-125): cuántas notificaciones sin leer tiene la persona y, solo si el template
lo pide (al pintar el panel), las más recientes. Perezoso: una página que no pinta el encabezado no consulta."""

from django.utils.functional import SimpleLazyObject

RECIENTES = 6


def notificaciones(request):
    usuario = getattr(request, "user", None)
    if usuario is None or not usuario.is_authenticated:
        return {}

    def _sin_leer():
        from apps.notificaciones.models import Notificacion

        return Notificacion.objects.filter(destinatario=usuario, leida_en__isnull=True).count()

    def _recientes():
        from apps.notificaciones.models import Notificacion

        return list(Notificacion.objects.filter(destinatario=usuario).select_related("ticket")[:RECIENTES])

    return {
        "notif_sin_leer": SimpleLazyObject(lambda: _Numero(_sin_leer())),
        "notif_recientes": SimpleLazyObject(_recientes),
    }


class _Numero(int):
    """Un entero que también se puede usar donde un objeto perezoso necesita atributos."""
