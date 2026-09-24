"""Servicio de autorización de Órbita. CU-009 (RQF-026, RQF-028, RN-006,
RN-007, RN-008, RN-009).

Único lugar donde se resuelven decisiones de autorización — ninguna otra
vista, template o modelo debe comparar `rol.nombre`, `rol.pk` ni ningún
otro identificador de rol directamente (RN-007). Toda decisión pasa por
los permisos efectivos asociados al rol vía `RolPermiso`.

Tres responsabilidades distintas, deliberadamente separadas:

1. `usuario_tiene_permiso(usuario, permiso_codigo, area=None, unidad_negocio=None)`
   — ¿tiene el usuario el permiso EN ESTE CONTEXTO? Sin `area`/`unidad_negocio`
   el contexto consultado es explícitamente GLOBAL (no "cualquier alcance").
   Una autorización GLOBAL siempre aplica, también cuando se consulta un
   Área o Unidad concreta.
2. Existencia de algún alcance autorizado — no es una función aparte: se
   responde comprobando si `alcances_autorizados(...)` devuelve algo no
   vacío (`global` en True, o `areas`/`unidades_negocio` no vacíos). Crear
   una tercera función solo para esto sería una abstracción sin necesidad
   real sobre la misma consulta.
3. `alcances_autorizados(usuario, permiso_codigo)` — ¿qué áreas/unidades
   puede ver/operar el usuario para este permiso? Para navegación/visibilidad
   y, en incrementos futuros, para filtrar querysets de dominios que todavía
   no existen (Ticket, Servicio, Proceso).
"""

from django.db.models import Q
from django.utils import timezone

from apps.core.models import AsignacionRol


def _asignaciones_vigentes(usuario, permiso_codigo):
    """Asignaciones de `usuario` que otorgan `permiso_codigo` hoy.

    Respeta `activo` en los cuatro niveles que intervienen en la decisión
    (Permiso, RolFuncional, RolPermiso, AsignacionRol) y la vigencia por
    fecha (ni vencida ni todavía no iniciada).
    """
    if not getattr(usuario, "is_authenticated", False) or not usuario.is_active:
        return AsignacionRol.objects.none()

    hoy = timezone.now().date()
    return AsignacionRol.objects.filter(
        usuario=usuario,
        activo=True,
        fecha_inicio__lte=hoy,
        rol__activo=True,
        rol__rolpermiso__activo=True,
        rol__rolpermiso__permiso__codigo=permiso_codigo,
        rol__rolpermiso__permiso__activo=True,
    ).filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))


def usuario_tiene_permiso(usuario, permiso_codigo, area=None, unidad_negocio=None):
    """¿Puede `usuario` ejecutar la acción de `permiso_codigo` en este contexto?

    - Sin `area` ni `unidad_negocio`: pregunta específicamente por alcance
      GLOBAL (no "en cualquier alcance").
    - Con `area` y/o `unidad_negocio`: una asignación GLOBAL siempre
      satisface la consulta (RQF-025, RN-008); además se acepta una
      asignación AREA que coincida exactamente con `area`, o UNIDAD que
      coincida exactamente con `unidad_negocio`.
    """
    vigentes = _asignaciones_vigentes(usuario, permiso_codigo)

    if vigentes.filter(tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL).exists():
        return True

    if area is not None and vigentes.filter(
        tipo_alcance=AsignacionRol.TipoAlcance.AREA, area=area
    ).exists():
        return True

    if unidad_negocio is not None and vigentes.filter(
        tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD, unidad_negocio=unidad_negocio
    ).exists():
        return True

    return False


def alcances_autorizados(usuario, permiso_codigo):
    """Alcances en los que `usuario` tiene `permiso_codigo` vigente.

    Devuelve {"global": bool, "areas": [ids], "unidades_negocio": [ids]}.
    Un resultado completamente vacío (`global` False y ambas listas vacías)
    equivale a "no tiene el permiso en ningún alcance".
    """
    vigentes = _asignaciones_vigentes(usuario, permiso_codigo)

    return {
        "global": vigentes.filter(tipo_alcance=AsignacionRol.TipoAlcance.GLOBAL).exists(),
        "areas": list(
            vigentes.filter(tipo_alcance=AsignacionRol.TipoAlcance.AREA).values_list(
                "area_id", flat=True
            )
        ),
        "unidades_negocio": list(
            vigentes.filter(tipo_alcance=AsignacionRol.TipoAlcance.UNIDAD).values_list(
                "unidad_negocio_id", flat=True
            )
        ),
    }
