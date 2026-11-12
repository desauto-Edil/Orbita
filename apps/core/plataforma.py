"""Administración de la PLATAFORMA (Versión 1, despliegue; requerimiento del 2026-11-12, CU por asignar).

`is_staff` e `is_superuser` ya no dan ningún acceso en Órbita: lo que antes abrían (la administración avanzada y
el catálogo visual interno) lo decide el permiso funcional `plataforma.administracion_avanzada` en alcance GLOBAL,
resuelto como cualquier otro permiso (`apps.core.autorizacion`, Permiso → Rol → Asignación), nunca por rol.

El PRIMER administrador no se crea desde la web: lo crea `manage.py otorgar_administrador`, que solo puede correr
quien tiene acceso al servidor. Desde ahí, los demás se otorgan en Configuración › Roles y permisos.

La administración avanzada (`apps.core.admin_sitio`) exige además haber ingresado CON GOOGLE en esta misma sesión
hace poco (`ingreso_google_reciente`): una sesión olvidada abierta no basta para entrar."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

PERMISO_ADMINISTRACION_AVANZADA = "plataforma.administracion_avanzada"

# Lo que recibe el primer administrador (`otorgar_administrador`): entrar a la administración avanzada y todas las
# secciones de Configuración, para que desde ahí otorgue lo demás.
PERMISOS_DEL_ADMINISTRADOR = (
    PERMISO_ADMINISTRACION_AVANZADA,
    "sistema.configurar",
    "usuarios.administrar",
    "permisos.administrar",
    "organizacion.administrar",
    "catalogo.administrar",
)
NOMBRE_ROL_ADMINISTRADOR = "Administrador de Plataforma"

_CACHE = "_orbita_administra_plataforma"


def administra_plataforma(usuario):
    """¿`usuario` tiene hoy `plataforma.administracion_avanzada` en alcance GLOBAL? (memorizado en el objeto, porque
    la administración avanzada lo pregunta muchas veces por página)."""
    if not getattr(usuario, "is_authenticated", False) or not usuario.is_active:
        return False
    if not hasattr(usuario, _CACHE):
        from apps.core.autorizacion import usuario_tiene_permiso

        setattr(usuario, _CACHE, usuario_tiene_permiso(usuario, PERMISO_ADMINISTRACION_AVANZADA))
    return getattr(usuario, _CACHE)


def minutos_de_reautenticacion():
    return max(1, int(getattr(settings, "ADMIN_REAUTENTICACION_MINUTOS", 30)))


def ingreso_google_reciente(request):
    """¿La sesión ACTUAL nació (o se confirmó) con Google en los últimos `ADMIN_REAUTENTICACION_MINUTOS`? Se lee
    del registro de accesos (CU-001): mismo usuario, método Google y la huella de esta sesión."""
    from apps.core.accesos import huella_de_sesion
    from apps.core.models import AccesoUsuario

    usuario = getattr(request, "user", None)
    huella = huella_de_sesion(request)
    if not huella or not getattr(usuario, "is_authenticated", False):
        return False
    desde = timezone.now() - timedelta(minutes=minutos_de_reautenticacion())
    return AccesoUsuario.objects.filter(
        usuario=usuario, metodo=AccesoUsuario.Metodo.GOOGLE, huella_sesion=huella, fecha__gte=desde
    ).exists()
