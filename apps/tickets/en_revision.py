"""Un ítem ENVIADO A REVISIÓN no se edita (requerimiento del usuario, 2026-11-12; CU por asignar).

Regla, para cualquier actividad de cualquier ticket: quien envió algo a revisión ya no puede cambiarlo, solo verlo,
hasta que se lo DEVUELVAN con correcciones. Mientras está en revisión NADIE lo edita por ninguna vía (ni quien
atiende el ticket, ni un participante de la colección, ni el responsable de otra tarea abierta): lo que el
revisor está mirando no puede cambiar debajo de él.

Un ítem está EN REVISIÓN cuando:
- su recorrido (subinstancia del flujo) está ESPERANDO una APROBACIÓN; o
- participa EN REVISIÓN en una gaceta activa (abierta o con el periodo cerrado sin resolver). En una gaceta manda el
  estado de la PARTICIPACIÓN, no la espera del recorrido: con una observación el ítem pasa a EN CORRECCIÓN (se
  edita aunque su recorrido siga esperando en el paso GACETA) y, aceptado, ya no está en revisión.

Devolverlo (aprobación DEVUELTA → el recorrido vuelve a una actividad; comentario en la gaceta → EN CORRECCIÓN)
deja de cumplir lo anterior y la tarea de corrección vuelve a conceder la edición (`edicion_por_tarea`). La regla
se aplica en el dominio, bajo lock (`parrilla._bloquear_elemento`, por donde pasa TODA edición de un ítem
existente: valores, archivos, evidencias y versiones); la interfaz solo la lee para mostrar el ítem en solo
lectura.

APROBADO = también solo lectura (2026-11-12): si la última aprobación resuelta del recorrido del ítem lo APROBÓ, ya
no se edita por la vía de la colección (quien atiende el ticket o un participante). Solo lo cambia un paso
POSTERIOR del flujo que lo pida explícitamente: el responsable de una tarea ABIERTA sobre ese ítem (producción,
corrección de gaceta, evidencias…), limitado a las columnas que esa tarea autoriza (`edicion_por_tarea`)."""

from django.core.exceptions import ValidationError

_ESPERAS_DE_REVISION = ("APROBACION",)


def _recorridos_en_revision(elemento_ids):
    from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

    return set(
        InstanciaEtapa.objects.filter(
            instancia_workflow__elemento_id__in=elemento_ids,
            instancia_workflow__estado=InstanciaWorkflow.Estado.EN_ESPERA,
            estado=InstanciaEtapa.Estado.EN_ESPERA,
            motivo_espera__in=_ESPERAS_DE_REVISION,
        ).values_list("instancia_workflow__elemento_id", flat=True)
    )


def _en_gaceta(elemento_ids):
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta

    return set(
        ParticipacionItemGaceta.objects.filter(
            elemento_id__in=elemento_ids,
            estado=ParticipacionItemGaceta.Estado.EN_REVISION,
            gaceta__estado__in=GacetaTicket.ESTADOS_ACTIVOS,
        ).values_list("elemento_id", flat=True)
    )


def _aprobados(elemento_ids):
    """Ítems cuya ÚLTIMA aprobación resuelta de su recorrido fue APROBADA."""
    from apps.aprobaciones.models import EsquemaAprobacion

    ultimo = {}
    for elemento_id, resultado in (
        EsquemaAprobacion.objects.filter(
            vinculo_workflow__instancia_etapa__instancia_workflow__elemento_id__in=elemento_ids,
            resultado__isnull=False,
        ).order_by("resuelto_en", "pk")
        .values_list("vinculo_workflow__instancia_etapa__instancia_workflow__elemento_id", "resultado")
    ):
        ultimo[elemento_id] = resultado
    return {e for e, r in ultimo.items() if r == EsquemaAprobacion.Resultado.APROBADA}


def aprobado(elemento):
    """¿`elemento` ya fue aprobado (y no se ha devuelto después)? Entonces solo lo edita una tarea abierta."""
    return elemento is not None and elemento.pk is not None and bool(_aprobados([elemento.pk]))


def ids_aprobados(elemento_ids):
    elemento_ids = [i for i in elemento_ids if i]
    return _aprobados(elemento_ids) if elemento_ids else set()


MENSAJE_APROBADO = (
    "Ya está aprobado: solo se puede ver. Solo lo cambia un paso posterior del flujo que te lo pida."
)


def ids_en_revision(elemento_ids):
    """De `elemento_ids`, los que están en revisión (en lote, para tablas)."""
    elemento_ids = [i for i in elemento_ids if i]
    if not elemento_ids:
        return set()
    return _recorridos_en_revision(elemento_ids) | _en_gaceta(elemento_ids)


def motivo(elemento):
    """Por qué `elemento` no se puede editar ahora, o `None` si no está en revisión."""
    if elemento is None or elemento.pk is None:
        return None
    if _recorridos_en_revision([elemento.pk]):
        return "Está en revisión: solo se puede ver. Se podrá editar si lo devuelven con correcciones."
    if _en_gaceta([elemento.pk]):
        return "Está en revisión en la gaceta: solo se puede ver. Se podrá editar si recibe una observación."
    return None


def exigir_editable(elemento):
    texto = motivo(elemento)
    if texto:
        raise ValidationError(texto)
