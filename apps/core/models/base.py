from django.db import models


class RegistroBase(models.Model):
    """Marca de tiempo de creación/actualización.

    No es auditoría: no registra quién hizo el cambio ni los valores
    anteriores/nuevos. Esa capacidad (`RegistroAuditoria`, registrada
    explícitamente desde `apps/core/auditoria.py` — sin señales globales,
    middleware ni thread-local) corresponde al incremento 0.4.
    """

    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
