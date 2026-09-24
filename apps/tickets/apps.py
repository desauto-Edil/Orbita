from django.apps import AppConfig


class TicketsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tickets"
    label = "tickets"
    verbose_name = "Órbita Tickets"

    def ready(self):
        from django.db.models.signals import pre_delete

        from apps.tickets.models import ArchivoRespuestaCampo, eliminar_archivo_fisico

        # Señal explícita, mismo criterio que 0.4 (`apps/core/auditoria.py`):
        # sin infraestructura genérica, solo este modelo conecta su propia
        # señal. Necesaria porque un `ticket.delete()` en cascada NO llama al
        # `.delete()` de Python de cada fila hija (Django usa un Collector a
        # nivel SQL) — solo `pre_delete` se dispara de forma confiable tanto
        # en cascada como en un delete directo, y es donde se borra el
        # archivo físico del storage antes de perder la fila que lo referencia.
        pre_delete.connect(eliminar_archivo_fisico, sender=ArchivoRespuestaCampo)
