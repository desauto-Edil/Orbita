"""Correo de la Conversación del ticket — los mensajes no llegan por correo uno a uno ni a quien está conectado.

Regla (decisión del usuario, revisada el 2026-11-12): un mensaje se avisa por correo SOLO si quien lo recibe SIGUE
SIN LEERLO pasados `ESPERA` minutos (lo marca el despacho en `Notificacion.correo_pendiente_desde`). Estar en línea
(`apps.core.presencia`) ya no lo impide: tener Órbita abierta en otra pestaña no es haber visto el mensaje. Entonces sale UN correo por persona, ticket y canal con todo lo pendiente («3 mensajes
nuevos»), no uno por mensaje. Leer la conversación (`conversacion.marcar_leido`) marca leídos esos avisos y ya no
sale nada. La preferencia de la persona y lo congelado en el ticket se respetaron al marcar el aviso; el envío
real usa la bandeja de salida de siempre (`EnvioCorreo`, `correo.encolar`). Lo corre Beat cada minuto
(`notificaciones.correos_de_conversacion`, sembrada en `notificaciones.0003`). Idempotente: lo que ya salió deja
de estar pendiente en la misma transacción."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

ESPERA = timedelta(minutes=5)


def programar_revision():
    """Encola en el worker una revisión de los correos de conversación para cuando venza la espera (más un margen).
    Si Celery no responde, queda la tarea de Beat de cada minuto."""
    import logging

    from apps.notificaciones.tasks import correos_de_conversacion

    try:
        correos_de_conversacion.apply_async(countdown=int(ESPERA.total_seconds()) + 30, retry=False)
    except Exception:  # noqa: BLE001 — sin broker: lo retoma Beat.
        logging.getLogger(__name__).warning("No se pudo programar la revisión de los correos de conversación.")


def _canal(notificacion):
    from apps.tickets.conversacion import HECHO_CANAL

    return next((valor for etiqueta, valor in notificacion.hechos or [] if etiqueta == HECHO_CANAL), "")


def enviar_pendientes(ahora=None):
    """Envía los correos de conversación que ya esperaron `ESPERA` sin que la persona leyera. Devuelve cuántos
    correos encoló."""
    from apps.notificaciones import correo
    from apps.notificaciones.models import EnvioCorreo, Notificacion

    ahora = ahora or timezone.now()
    with transaction.atomic():
        pendientes = list(
            Notificacion.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(evento="COMENTARIO", correo_pendiente_desde__isnull=False)
            .select_related("destinatario", "ticket")
            .order_by("pk")
        )
        grupos = {}
        for notificacion in pendientes:
            grupos.setdefault((notificacion.destinatario_id, notificacion.ticket_id, _canal(notificacion)), []).append(notificacion)
        enviadas = []
        for avisos in grupos.values():
            sin_leer = [a for a in avisos if a.leida_en is None]
            if not sin_leer:
                Notificacion.objects.filter(pk__in=[a.pk for a in avisos]).update(correo_pendiente_desde=None)
                continue
            if min(a.correo_pendiente_desde for a in sin_leer) > ahora - ESPERA:
                continue  # todavía puede leerlo: se espera
            ultimo = sin_leer[-1]
            persona = ultimo.destinatario
            Notificacion.objects.filter(pk__in=[a.pk for a in avisos]).update(correo_pendiente_desde=None)
            if not persona.is_active or not persona.email or EnvioCorreo.objects.filter(notificacion=ultimo).exists():
                continue
            mensajes = sum(a.cantidad for a in sin_leer)
            if mensajes > 1:
                ultimo.titulo = f"{mensajes} mensajes nuevos"
                ultimo.resumen = f"Tienes {mensajes} mensajes sin leer en la conversación de «{_nombre(ultimo)}»."
                ultimo.cantidad = mensajes
                ultimo.save(update_fields=["titulo", "resumen", "cantidad"])
            EnvioCorreo.objects.create(notificacion=ultimo, correo=persona.email)
            enviadas.append(ultimo)
    correo.encolar(enviadas)
    return len(enviadas)


def _nombre(notificacion):
    from apps.notificaciones.mensajes import nombre_ticket

    return nombre_ticket(notificacion.ticket) or "el ticket"
