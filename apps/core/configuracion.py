"""Configuración — administración de la plataforma dentro de Órbita.

Sustituye a Django Admin como interfaz cotidiana para lo que ya existía desde
Sprint 0 (CU-004 a CU-008: usuarios, áreas, unidades de negocio, roles,
permisos y asignaciones) y para las categorías del catálogo (CU-010), y añade
la identidad del sistema (nombre y logo). No crea reglas de dominio nuevas:
los modelos, sus restricciones y la auditoría (CU-040) son los mismos.

Secciones y la capacidad real que abre cada una (nunca un nombre de rol):

  General            `sistema.configurar`        nombre y logo de la plataforma
  Usuarios           `usuarios.administrar`      cuentas, perfil, áreas y unidades
                     (`permisos.administrar` también entra, solo para
                     asignar o retirar roles en la ficha de cada persona)
  Roles y permisos   `permisos.administrar`      roles, permisos y asignaciones
  Áreas / Unidades   `organizacion.administrar`  catálogos organizacionales
  Categorías         `catalogo.administrar`      categorías del catálogo

Todas en alcance GLOBAL. `is_superuser` abre las cuatro secciones de
`apps.core`: esa cuenta ya administra esos mismos modelos desde Django Admin,
así que no se le concede nada nuevo y evita que nadie pueda otorgar el primer
permiso. Categorías no entra en esa regla: `catalogo.administrar` ya se exige
sin excepción en Django Admin y en Studio.

Este módulo importa el dominio de catálogo de forma perezosa (mismo criterio
que `navegacion.py` y `disenador.py`).
"""

from apps.core.autorizacion import permisos_globales

PERMISO_SISTEMA = "sistema.configurar"
PERMISO_USUARIOS = "usuarios.administrar"
PERMISO_PERMISOS = "permisos.administrar"
PERMISO_ORGANIZACION = "organizacion.administrar"
PERMISO_CATEGORIAS = "catalogo.administrar"

# capacidad → permiso que la otorga.
_PERMISOS = {
    "sistema": PERMISO_SISTEMA,
    "usuarios": PERMISO_USUARIOS,
    "permisos": PERMISO_PERMISOS,
    "organizacion": PERMISO_ORGANIZACION,
    "categorias": PERMISO_CATEGORIAS,
}
_SOLO_POR_PERMISO = {"categorias"}

# Secciones locales, en el orden en que se muestran.
SECCIONES = (
    {
        "clave": "general",
        "etiqueta": "General",
        "url_name": "core:configuracion_general",
        "capacidad": "sistema",
        "icono": "settings",
        "descripcion": "Nombre y logo de la plataforma.",
    },
    {
        "clave": "usuarios",
        "etiqueta": "Usuarios",
        "url_name": "core:configuracion_usuarios",
        "capacidad": "ve_usuarios",
        "icono": "user",
        "descripcion": "Cuentas, datos de perfil y a qué áreas y unidades pertenece cada persona.",
    },
    {
        "clave": "roles",
        "etiqueta": "Roles y permisos",
        "url_name": "core:configuracion_roles",
        "capacidad": "permisos",
        "icono": "shield",
        "descripcion": "Qué puede hacer cada rol y a quién se le asigna.",
    },
    {
        "clave": "areas",
        "etiqueta": "Áreas",
        "url_name": "core:configuracion_areas",
        "capacidad": "organizacion",
        "icono": "grid",
        "descripcion": "Áreas de la organización y las unidades con las que se relacionan.",
    },
    {
        "clave": "unidades",
        "etiqueta": "Unidades",
        "url_name": "core:configuracion_unidades",
        "capacidad": "organizacion",
        "icono": "compass",
        "descripcion": "Unidades de negocio y las áreas con las que se relacionan.",
    },
    {
        "clave": "categorias",
        "etiqueta": "Categorías",
        "url_name": "core:configuracion_categorias",
        "capacidad": "categorias",
        "icono": "inbox",
        "descripcion": "Cómo se agrupan los servicios y procesos que se pueden solicitar.",
    },
)


def capacidades(usuario):
    """Qué secciones de Configuración puede administrar `usuario`, resuelto
    una sola vez (una consulta)."""
    if not getattr(usuario, "is_authenticated", False) or not usuario.is_active:
        return dict.fromkeys(_PERMISOS, False) | {"ve_usuarios": False, "accede": False}
    otorgados = permisos_globales(usuario, _PERMISOS.values())
    caps = {
        clave: codigo in otorgados or (usuario.is_superuser and clave not in _SOLO_POR_PERMISO)
        for clave, codigo in _PERMISOS.items()
    }
    caps["accede"] = any(caps.values())
    # Quien asigna roles necesita encontrar a la persona, aunque no pueda
    # modificar su cuenta: la ficha le muestra solo la parte de roles.
    caps["ve_usuarios"] = caps["usuarios"] or caps["permisos"]
    return caps


def accede_a_configuracion(usuario):
    return capacidades(usuario)["accede"]


def secciones(caps, activa=None):
    """Pestañas LOCALES de Configuración (no son navegación global): solo las
    que el usuario puede administrar."""
    return [
        {**seccion, "activa": seccion["clave"] == activa}
        for seccion in SECCIONES
        if caps[seccion["capacidad"]]
    ]


def resumen(caps):
    """Tarjetas de la portada: cada sección visible con su dato real."""
    from apps.core.models import Area, RolFuncional, UnidadNegocio, Usuario

    def _conteo(clave):
        if clave == "usuarios":
            return Usuario.objects.filter(is_active=True).count(), "activos"
        if clave == "roles":
            return RolFuncional.objects.filter(activo=True).count(), "activos"
        if clave == "areas":
            return Area.objects.filter(activo=True).count(), "activas"
        if clave == "unidades":
            return UnidadNegocio.objects.filter(activo=True).count(), "activas"
        if clave == "categorias":
            from apps.catalogo.models import Categoria

            return Categoria.objects.filter(activo=True).count(), "activas"
        return None, ""

    tarjetas = []
    for seccion in secciones(caps):
        total, unidad = _conteo(seccion["clave"])
        tarjetas.append({**seccion, "total": total, "unidad": unidad})
    return tarjetas
