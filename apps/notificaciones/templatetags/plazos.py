"""`{{ ticket|estado_plazo }}` → "vencido" · "por_vencer" · "" — la MISMA regla del evaluador de alertas
(`apps.notificaciones.alertas.estado_de_plazo`), para marcar las filas de Mi trabajo, Cola y Mis tickets."""

from django import template

register = template.Library()


@register.filter
def estado_plazo(ticket):
    from apps.notificaciones.alertas import estado_de_plazo

    try:
        return estado_de_plazo(ticket)
    except Exception:  # noqa: BLE001 — una marca visual nunca rompe una lista.
        return ""
