"""Redirección de vuelta tras una acción (4.F3).

Una acción POST (tomar, completar, subir un entregable…) puede venir de pantallas distintas (detalle
operativo, Trabajo, Cola). El formulario envía `next` con la pantalla de origen y la vista vuelve a
ella. Solo se aceptan destinos relativos del propio sitio: nunca otro host (sin open redirect)."""

from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def volver_a(request, por_defecto, *args):
    """`redirect` a `next` si es un destino interno seguro; si no, a `por_defecto` (nombre de URL)."""
    destino = request.POST.get("next") or ""
    if destino.startswith("/") and url_has_allowed_host_and_scheme(
        destino, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(destino)
    return redirect(por_defecto, *args)
