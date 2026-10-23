"""Bloque 3.1 — edición de un ítem por la TAREA ABIERTA que lo trabaja.

Quien es responsable de una Tarea abierta asociada a un ítem puede editar SOLO las columnas que la configuración
de esa Tarea autoriza (`columnas_editables`), y solo en ESE ítem, sin convertirse en responsable del ticket ni
en participante de la colección. Hay tres modalidades, y en las tres el ítem y la configuración se identifican
sin ambigüedad y desde datos CONGELADOS:

- ACTIVIDAD de un recorrido por ítem: `TareaWorkflow` → ejecución (`InstanciaEtapa`) → subinstancia
  (`InstanciaWorkflow.elemento`); la configuración es la del `BloqueOperativo` (rama ITEM) de una versión de la
  configuración de ejecución ya activa, que no se edita (`apps.catalogo.actividades`).
- TAREA POR ELEMENTO: `TareaElemento` (elemento + `ConfiguracionTareasTicket`, copia congelada en el ticket).
- CORRECCIÓN DE GACETA: la `RondaRevisionGaceta` VIGENTE del ítem (`numero == participacion.ronda`) en
  corrección; la configuración es la congelada en `GacetaTicket.configuracion`.

La concesión existe mientras la Tarea siga abierta y pertenezca a la persona (`puede_completar_tarea`: responsable
directo, miembro activo del equipo responsable o delegado vigente) y el ticket esté EN ATENCIÓN: completar,
cancelar o reasignar la Tarea la revoca sola, porque nada se guarda. Las claves se resuelven contra la versión de
columnas que congeló la colección del ítem; una clave que esa versión no tiene no concede nada.

Solo calcula. Las operaciones de `apps.tickets.parrilla` lo consultan bajo sus locks y siguen aplicando todas sus
protecciones (alcance, columnas consumidas, columnas que originaron una tarea, ítems retirados).
"""

from dataclasses import dataclass

from django.db.models import F

from apps.catalogo.models import Campo
from apps.tickets.models import ElementoParrilla, Ticket, TareaElemento

ABIERTAS = ("PENDIENTE", "EN_PROGRESO")

ACTIVIDAD, TAREA_ELEMENTO, CORRECCION_GACETA = "ACTIVIDAD", "TAREA_ELEMENTO", "CORRECCION_GACETA"


@dataclass(frozen=True)
class Concesion:
    """Lo que UNA tarea abierta autoriza: el ítem y las claves de columna editables (de su configuración)."""

    tarea: object
    elemento_id: int
    claves: tuple
    origen: str


def _de_actividad(vinculo):
    ejecucion = vinculo.instancia_etapa
    bloque, instancia = ejecucion.bloque_operativo, ejecucion.instancia_workflow
    if bloque is None or bloque.tipo != "ACTIVIDAD" or bloque.rama != "ITEM" or instancia.elemento_id is None:
        return None
    from apps.catalogo import actividades

    claves = tuple(actividades.normalizar(bloque.configuracion)[0]["columnas_editables"])
    return Concesion(vinculo.tarea, instancia.elemento_id, claves, ACTIVIDAD)


def _de_tarea_elemento(vinculo):
    return Concesion(vinculo.tarea, vinculo.elemento_id, tuple(vinculo.configuracion.columnas_editables or ()), TAREA_ELEMENTO)


def _de_correccion(ronda):
    from apps.catalogo import actividades

    claves = actividades.columnas_editables((ronda.participacion.gaceta.configuracion or {}).get("columnas_editables"), [])
    return Concesion(ronda.tarea_correccion, ronda.participacion.elemento_id, tuple(claves), CORRECCION_GACETA)


def _rondas_vigentes():
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta, RondaRevisionGaceta

    return RondaRevisionGaceta.objects.select_related("tarea_correccion", "participacion__gaceta").filter(
        numero=F("participacion__ronda"),
        participacion__estado=ParticipacionItemGaceta.Estado.EN_CORRECCION,
        tarea_correccion__isnull=False,
    ).exclude(participacion__gaceta__estado__in=GacetaTicket.ESTADOS_FINALES)


def concesiones_del_elemento(elemento):
    """Las concesiones de TODAS las tareas abiertas asociadas al ítem (sin mirar quién las tiene)."""
    from apps.workflows.models import TareaWorkflow

    concesiones = []
    for vinculo in TareaWorkflow.objects.select_related(
        "tarea", "instancia_etapa__bloque_operativo", "instancia_etapa__instancia_workflow"
    ).filter(instancia_etapa__instancia_workflow__elemento_id=elemento.pk, tarea__estado__in=ABIERTAS):
        concesion = _de_actividad(vinculo)
        if concesion is not None:
            concesiones.append(concesion)
    for vinculo in TareaElemento.objects.select_related("tarea", "configuracion").filter(
        elemento_id=elemento.pk, tarea__estado__in=ABIERTAS
    ):
        concesiones.append(_de_tarea_elemento(vinculo))
    for ronda in _rondas_vigentes().filter(participacion__elemento_id=elemento.pk, tarea_correccion__estado__in=ABIERTAS):
        concesiones.append(_de_correccion(ronda))
    return [c for c in concesiones if c.claves]


def concesion_de_tarea(tarea):
    """Lo que autoriza ESTA tarea (`Concesion`), o `None` si no está abierta, no trabaja un ítem o su
    configuración no permite editar columnas. Sirve para ofrecer «Editar el ítem» desde la tarea."""
    if tarea is None or tarea.estado not in ABIERTAS:
        return None
    from apps.workflows.models import TareaWorkflow

    vinculo = TareaWorkflow.objects.select_related(
        "tarea", "instancia_etapa__bloque_operativo", "instancia_etapa__instancia_workflow"
    ).filter(tarea=tarea).first()
    concesion = _de_actividad(vinculo) if vinculo is not None else None
    if concesion is None:
        vinculo = TareaElemento.objects.select_related("tarea", "configuracion").filter(tarea=tarea).first()
        concesion = _de_tarea_elemento(vinculo) if vinculo is not None else None
    if concesion is None:
        ronda = _rondas_vigentes().filter(tarea_correccion=tarea).first()
        concesion = _de_correccion(ronda) if ronda is not None else None
    return concesion if concesion is not None and concesion.claves else None


def _campos(elemento, claves):
    version_id = elemento.parrilla.formulario_version_id
    return frozenset(Campo.objects.filter(version_id=version_id, clave__in=claves).values_list("pk", flat=True))


def columnas_por_tarea(usuario, elemento, ticket=None):
    """Ids de las columnas (de la versión congelada) que `usuario` puede editar en `elemento` por sus tareas
    abiertas. Vacío = ninguna. Exige el ticket EN ATENCIÓN y el ítem vigente."""
    if not (getattr(usuario, "is_authenticated", False) and getattr(usuario, "is_active", False)):
        return frozenset()
    if not isinstance(elemento, ElementoParrilla) or elemento.retirado_en is not None:
        return frozenset()
    ticket = ticket or elemento.parrilla.ticket
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        return frozenset()
    from apps.tareas.autorizacion import puede_completar_tarea

    claves = {
        clave
        for concesion in concesiones_del_elemento(elemento)
        if puede_completar_tarea(usuario, concesion.tarea)
        for clave in concesion.claves
    }
    return _campos(elemento, claves) if claves else frozenset()


def puede_editar_por_tarea(usuario, elemento, ticket=None):
    return bool(columnas_por_tarea(usuario, elemento, ticket))


def elemento_de_tarea(tarea, usuario):
    """El ítem que `usuario` puede editar desde ESTA tarea (o `None`): para el enlace en el detalle de la tarea."""
    concesion = concesion_de_tarea(tarea)
    if concesion is None:
        return None
    elemento = ElementoParrilla.objects.select_related("parrilla__ticket").filter(pk=concesion.elemento_id).first()
    if elemento is None or not columnas_por_tarea(usuario, elemento):
        return None
    return elemento
