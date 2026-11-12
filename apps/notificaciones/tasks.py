"""Tareas Celery de las notificaciones. Mismo patrón que `apps.tickets.tasks`: sin lógica de dominio propia, cada
una delega en su módulo y un error aislado no tumba el lote. Periodicidad sembrada por migración
(`0002_seed_periodic_tasks`, `0003_correo_de_conversacion`)."""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def enviar_correo(envio_id):
    from apps.notificaciones.correo import enviar

    envio = enviar(envio_id)
    return getattr(envio, "estado", None)


@shared_task
def reintentar_correos():
    """Retoma los correos PENDIENTES que no se pudieron encolar y los FALLIDOS (antes de llegar a Google) con
    intentos disponibles. Nunca los INCIERTOS (podrían duplicarse)."""
    from apps.notificaciones.correo import MAX_INTENTOS, enviar
    from apps.notificaciones.models import EnvioCorreo

    limite = timezone.now() - timedelta(minutes=5)
    candidatos = EnvioCorreo.objects.filter(
        estado__in=(EnvioCorreo.Estado.PENDIENTE, EnvioCorreo.Estado.FALLIDO), intentos__lt=MAX_INTENTOS,
        creado_en__lte=limite,
    ).values_list("pk", flat=True)[:200]
    procesados = 0
    for envio_id in candidatos:
        try:
            enviar(envio_id)
            procesados += 1
        except Exception:  # noqa: BLE001
            logger.exception("Error reintentando el correo %s", envio_id)
    return procesados


@shared_task
def correos_de_conversacion():
    """El correo de los mensajes que siguen sin leer tras la espera. Lo corre Beat cada minuto y, además, el worker
    cuando vence la espera de cada lote (`correo_conversacion.programar_revision`)."""
    from apps.notificaciones.correo_conversacion import enviar_pendientes

    return enviar_pendientes()


@shared_task
def evaluar_alertas():
    from apps.notificaciones.alertas import evaluar

    resumen = evaluar()
    logger.info("evaluar_alertas: %s", resumen)
    return resumen
