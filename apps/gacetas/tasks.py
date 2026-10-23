"""Vencimientos de gacetas — Fase 3. Mismo patrón que `apps.tickets.tasks`: Celery + `django_celery_beat`
(periodicidad sembrada en `gacetas.0002_seed_periodic_task_vencimientos`), sin lógica de dominio propia: detecta
candidatos y delega en `apps.gacetas.operaciones`, que revalida todo bajo lock (idempotente: dos ejecuciones a la
vez, o una acción manual entre medias, no repiten efectos). Un error en una gaceta no detiene el lote."""

import logging

from celery import shared_task
from django.utils import timezone

from apps.gacetas import operaciones
from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta

logger = logging.getLogger(__name__)


def reconciliar(ahora=None):
    """1) cierra el periodo de las gacetas abiertas cuyo plazo de revisión venció (con aceptación automática si
    está configurada); 2) registra las correcciones vencidas; 3) intenta la resolución automática de las gacetas
    con el periodo cerrado. Devuelve cuántas acciones hizo."""
    ahora = ahora or timezone.now()
    acciones = 0
    for gaceta in GacetaTicket.objects.filter(estado=GacetaTicket.Estado.ABIERTA, vence_revision_en__lte=ahora).order_by("pk"):
        try:
            operaciones.cerrar_periodo(gaceta, None)
            acciones += 1
        except Exception:  # noqa: BLE001 — aislar el error de una gaceta
            logger.exception("No se pudo cerrar el periodo de la gaceta %s", gaceta.pk)
    for participacion in ParticipacionItemGaceta.objects.filter(
        estado=ParticipacionItemGaceta.Estado.EN_CORRECCION, vence_correccion_en__lte=ahora,
        correccion_vencida_en__isnull=True,
    ).order_by("pk"):
        try:
            operaciones.marcar_correccion_vencida(participacion)
            acciones += 1
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo registrar el vencimiento de la corrección %s", participacion.pk)
    for gaceta in GacetaTicket.objects.filter(
        estado=GacetaTicket.Estado.PERIODO_CERRADO, configuracion__resolucion_automatica=True
    ).order_by("pk"):
        try:
            if operaciones.resolver_si_corresponde(gaceta) is not None:
                acciones += 1
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo resolver automáticamente la gaceta %s", gaceta.pk)
    return acciones


@shared_task
def reconciliar_vencimientos():
    return reconciliar()
