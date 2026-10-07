"""Resolución de visibilidad de Servicio. CU-011 (RQF-032/036/037, RN-038).

No es autorización funcional (ver docstring de `apps/catalogo/admin.py`
para esa distinción): es un filtro de datos sobre qué servicios concretos
puede consultar un usuario final, sin pasar por `Permiso`/`AsignacionRol`.

Sin patrón de diseño: es una única consulta uniforme (PUBLICO_INTERNO O
RESTRINGIDO-con-concesión), sin variación de comportamiento que justifique
Strategy/Specification u otra abstracción.
"""

from django.db.models import Exists, OuterRef, Q

from apps.catalogo.models import ProgramacionProceso, Servicio, ServicioVisibilidad


def servicios_visibles_para(usuario):
    """Servicios CATALOGADOS que `usuario` puede consultar: los de
    `servicios_accesibles_para` sin el Servicio interno del Ticket General y sin los Procesos
    con programación activa (4.G1: un Proceso programado se inicia por programación, no se solicita).

    Es la única autoridad de qué se ofrece en el catálogo, el explorador, las
    búsquedas, los frecuentes y cualquier selector de Servicio/Proceso; excluir
    aquí al Ticket General evita repetir ese filtro en cada pantalla. Tampoco
    permite crear un ticket por la vía normal (`crear_borrador`): el Ticket
    General tiene su propia entrada de dominio (`crear_borrador_ticket_general`).
    """
    programados = ProgramacionProceso.objects.filter(servicio=OuterRef("pk"), activa=True)
    return servicios_accesibles_para(usuario).filter(es_ticket_general=False).exclude(Exists(programados))


def servicios_accesibles_para(usuario):
    """Servicios activos que `usuario` puede consultar (CU-011), INCLUYENDO el
    Servicio interno del Ticket General. Uso restringido a las entradas que
    deben alcanzarlo (Ticket General); el resto del producto usa
    `servicios_visibles_para`.

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
