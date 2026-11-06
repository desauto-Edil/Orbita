from django import template

from apps.tickets import vista_previa

register = template.Library()


@register.filter
def clase_vista_previa(adjunto):
    """«imagen», «video», «pdf» o «» para un adjunto (por su nombre original)."""
    return vista_previa.clase_de(getattr(adjunto, "nombre_original", ""))
