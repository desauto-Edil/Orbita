"""Cierre automático de entregas vencidas — Sprint 4.5.

Mismo patrón que `apps.workflows.tasks.reanudar_esperas_vencidas`: reutiliza
Celery + `django_celery_beat` (periodicidad sembrada en la migración
`0011_seed_periodic_task_cerrar_entregas`) y no agrega lógica de dominio — solo
detecta candidatos y delega en `apps.tickets.entregas.cerrar_entrega_vencida`,
que es la única autoridad (lock del ticket, revalidación de estado, cierre por
la transición real y auditoría con origen SISTEMA).

Idempotencia/concurrencia: la tarea no usa locks ni banderas propias. Si dos
ejecuciones coinciden, o el solicitante responde entre la selección y el
intento, `cerrar_entrega_vencida` revalida bajo lock y devuelve `None` (no-op).
Una entrega que falla no impide procesar el resto del lote.
"""

import logging

from celery import shared_task
from django.utils import timezone

from apps.tickets.entregas import cerrar_entrega_vencida
from apps.tickets.models import EntregaTicket

logger = logging.getLogger(__name__)


def _candidatas_vencidas():
    return EntregaTicket.objects.filter(
        estado=EntregaTicket.Estado.PENDIENTE, vence_en__isnull=False, vence_en__lte=timezone.now()
    ).order_by("vence_en", "pk")


@shared_task
def cerrar_entregas_vencidas():
    cerradas = 0
    for entrega in _candidatas_vencidas():
        try:
            if cerrar_entrega_vencida(entrega) is not None:
                cerradas += 1
        except Exception:  # noqa: BLE001 — infraestructura/código inesperado; no debe tumbar el lote.
            logger.exception("Error cerrando la entrega vencida %s.", entrega.pk)
    logger.info("cerrar_entregas_vencidas: %s entrega(s) cerrada(s).", cerradas)
    return cerradas
