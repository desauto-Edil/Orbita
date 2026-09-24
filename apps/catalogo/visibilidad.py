"""Resolución de visibilidad de Servicio. CU-011 (RQF-032/036/037, RN-038).

No es autorización funcional (ver docstring de `apps/catalogo/admin.py`
para esa distinción): es un filtro de datos sobre qué servicios concretos
puede consultar un usuario final, sin pasar por `Permiso`/`AsignacionRol`.

Sin patrón de diseño: es una única consulta uniforme (PUBLICO_INTERNO O
RESTRINGIDO-con-concesión), sin variación de comportamiento que justifique
Strategy/Specification u otra abstracción.
"""

from django.db.models import Q

from apps.catalogo.models import Servicio, ServicioVisibilidad


def servicios_visibles_para(usuario):
    """Servicios activos que `usuario` puede consultar (CU-011).

    RN-038: PUBLICO_INTERNO es visible para cualquier autenticado activo;
    RESTRINGIDO exige una `ServicioVisibilidad` activa por usuario, área o
    unidad — sin concesión, no es visible (consecuencia literal del
    mecanismo de concesión, no una regla implícita).
    """
    if not getattr(usuario, "is_authenticated", False) or not usuario.is_active:
        return Servicio.objects.none()

    areas_ids = usuario.areas.filter(activo=True).values_list("area_id", flat=True)
    unidades_ids = usuario.unidades_negocio.filter(activo=True).values_list("unidad_negocio_id", flat=True)

    concedidos = ServicioVisibilidad.objects.filter(activo=True).filter(
        Q(tipo_alcance=ServicioVisibilidad.TipoAlcance.USUARIO, usuario=usuario)
        | Q(tipo_alcance=ServicioVisibilidad.TipoAlcance.AREA, area_id__in=areas_ids)
        | Q(tipo_alcance=ServicioVisibilidad.TipoAlcance.UNIDAD, unidad_negocio_id__in=unidades_ids)
    ).values_list("servicio_id", flat=True)

    return Servicio.objects.filter(activo=True).filter(
        Q(alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO)
        | Q(alcance_visibilidad=Servicio.AlcanceVisibilidad.RESTRINGIDO, pk__in=concedidos)
    ).distinct()
