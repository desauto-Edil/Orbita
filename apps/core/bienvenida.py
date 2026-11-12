"""Bienvenida a Órbita (Versión 1, etapa 7; requerimiento del 2026-11-12, CU por asignar; extiende CU-003).

La primera vez que una persona entra (también quienes ya existían: `Usuario.bienvenida_completada_en` vacío), un
recorrido de CINCO pasos la ayuda a dejar su espacio a su gusto:

  1. Bienvenida · 2. Apariencia (claro/oscuro/automático) · 3. Color (uno de los curados o el del sistema; SOLO para
  ella) · 4. Tu organización · 5. Tus compañeros.

Lo que la persona elige es suyo y no afecta a nadie: `Usuario.apariencia` y `Usuario.tema`. «Tu organización»
es de SOLO LECTURA (decisión del usuario, 2026-11-12): área, unidades, equipos y jefe deciden accesos (servicios
restringidos por área, tomar tickets de un equipo, «Mi equipo» del jefe), así que los registra el administrador en
Configuración; el paso los muestra y dice a quién pedir una corrección. Se puede OMITIR en cualquier paso y todo se
cambia después en Mi perfil. `BienvenidaMiddleware` la muestra hasta completarla u omitirla."""

from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

PASOS = (
    ("bienvenida", "Bienvenida"),
    ("apariencia", "Apariencia"),
    ("color", "Color"),
    ("organizacion", "Tu organización"),
    ("companeros", "Tus compañeros"),
)
CLAVES = tuple(clave for clave, _ in PASOS)

# Rutas que nunca se interrumpen con la bienvenida: la bienvenida misma, salir, guardar preferencias, señales de
# fondo y archivos. La administración avanzada tiene su propia puerta.
_LIBRES = {
    "core:bienvenida", "core:logout", "core:apariencia", "core:tema", "core:presencia", "core:salud", "core:login",
    "core:google_login", "core:google_callback", "core:logo", "core:logo_oscuro",
}
_NAMESPACES_LIBRES = {"admin"}


def pendiente(usuario):
    return bool(getattr(usuario, "is_authenticated", False)) and usuario.bienvenida_completada_en is None


def paso_valido(clave):
    return clave if clave in CLAVES else CLAVES[0]


def vecinos(clave):
    i = CLAVES.index(clave)
    return (CLAVES[i - 1] if i > 0 else None), (CLAVES[i + 1] if i < len(CLAVES) - 1 else None)


def completar(usuario):
    """Termina (u omite) la bienvenida. Idempotente: la fecha no cambia una vez puesta."""
    if usuario.bienvenida_completada_en is None:
        usuario.bienvenida_completada_en = timezone.now()
        usuario.save(update_fields=["bienvenida_completada_en"])


def organizacion_de(usuario):
    """Lo que el administrador registró (solo lectura): áreas, unidades, equipos y jefe directo."""
    from apps.core import jefaturas
    from apps.core import perfil as perfil_ops

    areas = list(usuario.areas.filter(activo=True, area__activo=True).select_related("area").order_by("-es_principal"))
    return {
        "areas": areas,
        "area_principal": next((a for a in areas if a.es_principal), None),
        "unidades": list(
            usuario.unidades_negocio.filter(activo=True).select_related("unidad_negocio").order_by("-es_principal")
        ),
        "equipos": list(perfil_ops.equipos_de(usuario)),
        "jefe": jefaturas.jefe_de(usuario),
        "perfil": getattr(usuario, "perfil_organizacional", None),
    }


class BienvenidaMiddleware:
    """Mientras la bienvenida esté pendiente, una página normal (GET, no `fetch`) lleva a ella conservando a dónde
    iba la persona (`next`). Nunca interrumpe un envío de formulario ni las peticiones de fondo."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not getattr(settings, "BIENVENIDA_OBLIGATORIA", True):
            return None
        if request.method != "GET" or request.headers.get("X-Requested-With") == "fetch":
            return None
        if not pendiente(getattr(request, "user", None)):
            return None
        coincidencia = request.resolver_match
        if coincidencia is None or coincidencia.view_name in _LIBRES or coincidencia.namespace in _NAMESPACES_LIBRES:
            return None
        if request.path.startswith(tuple("/" + u.lstrip("/") for u in (settings.STATIC_URL, settings.MEDIA_URL))):
            return None
        return redirect(f"{reverse('core:bienvenida')}?{urlencode({'next': request.get_full_path()})}")
