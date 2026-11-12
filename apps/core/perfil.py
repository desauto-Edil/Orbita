"""Composición de lectura de Mi perfil (CU-003). No escribe ni decide permisos.

Compañeros: las personas activas con quienes la persona comparte un EQUIPO activo (membresía
`activo=True`, la misma regla que usan las autorizaciones de tickets/tareas). Sin equipos, quienes
comparten su ÁREA PRINCIPAL. Nunca se muestra a sí misma.
"""

from django.db.models import Prefetch

from apps.core.models import Equipo, MiembroEquipo, UsuarioArea

MAXIMO_COMPANEROS = 8


def _grupo(nombre, tipo, miembros):
    personas = [m.usuario for m in miembros]
    return {
        "nombre": nombre,
        "tipo": tipo,
        "personas": personas[:MAXIMO_COMPANEROS],
        "restantes": max(0, len(personas) - MAXIMO_COMPANEROS),
        "total": len(personas),
    }


def _orden(qs):
    return qs.select_related("usuario", "usuario__perfil_organizacional").order_by(
        "usuario__first_name", "usuario__last_name", "usuario__email"
    )


def companeros_de(usuario, area_principal=None):
    """Lista de grupos `{nombre, tipo, personas, restantes, total}` (equipos o, en su defecto, el área)."""
    otros = _orden(MiembroEquipo.objects.filter(activo=True, usuario__is_active=True).exclude(usuario=usuario))
    equipos = (
        Equipo.objects.filter(activo=True, miembros__usuario=usuario, miembros__activo=True)
        .distinct()
        .order_by("nombre")
        .prefetch_related(Prefetch("miembros", queryset=otros, to_attr="companeros"))
    )
    grupos = [_grupo(e.nombre, "equipo", e.companeros) for e in equipos if e.companeros]
    if grupos or area_principal is None:
        return grupos
    del_area = _orden(
        UsuarioArea.objects.filter(area_id=area_principal.area_id, activo=True, usuario__is_active=True).exclude(
            usuario=usuario
        )
    )
    del_area = list(del_area)
    return [_grupo(area_principal.area.nombre, "area", del_area)] if del_area else []


def equipos_de(usuario):
    return (
        Equipo.objects.filter(activo=True, miembros__usuario=usuario, miembros__activo=True)
        .distinct()
        .order_by("nombre")
    )
