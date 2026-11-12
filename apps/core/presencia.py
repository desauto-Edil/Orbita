"""Presencia — ¿quién tiene Órbita abierta ahora? (Conversación del ticket: ver quién de los compañeros está en línea
y decidir si un mensaje se avisa también por correo).

Regla: una persona está EN LÍNEA si Órbita le confirmó su presencia en los últimos `VENTANA` (2 minutos). La confirma
cualquier petición suya (`PresenciaMiddleware`) y, mientras tenga una pestaña de Órbita visible, la señal que el
navegador envía cada minuto (`core:presencia`, `static/js/app.js`). Se guarda en `Usuario.visto_en` (no hay una
caché compartida entre procesos: la web y Celery deben leer lo mismo) y se escribe como mucho cada
`INTERVALO_ESCRITURA` para no cargar la base de datos. No es auditoría ni un registro de accesos (eso es
`AccesoUsuario`): solo la marca más reciente."""

from datetime import timedelta

from django.utils import timezone

VENTANA = timedelta(minutes=2)
INTERVALO_ESCRITURA = timedelta(seconds=30)


def registrar(usuario, ahora=None):
    """Confirma que `usuario` tiene Órbita abierta. Devuelve True si escribió."""
    if not getattr(usuario, "is_authenticated", False):
        return False
    ahora = ahora or timezone.now()
    visto = getattr(usuario, "visto_en", None)
    if visto is not None and ahora - visto < INTERVALO_ESCRITURA:
        return False
    from apps.core.models import Usuario

    Usuario.objects.filter(pk=usuario.pk).update(visto_en=ahora)
    usuario.visto_en = ahora
    return True


def esta_en_linea(visto_en, ahora=None):
    return visto_en is not None and (ahora or timezone.now()) - visto_en < VENTANA


def en_linea(usuario, ahora=None):
    """¿`usuario` está en línea? Lee la marca guardada (no la del objeto en memoria, que puede ser vieja)."""
    from apps.core.models import Usuario

    visto = Usuario.objects.filter(pk=usuario.pk).values_list("visto_en", flat=True).first()
    return esta_en_linea(visto, ahora)


class PresenciaMiddleware:
    """Cada petición de una persona autenticada confirma su presencia (como mucho una escritura cada 30 s)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        respuesta = self.get_response(request)
        try:
            registrar(getattr(request, "user", None))
        except Exception:  # noqa: BLE001 — la presencia nunca rompe una petición
            pass
        return respuesta
