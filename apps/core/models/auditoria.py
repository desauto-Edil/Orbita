from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q


class RegistroAuditoria(models.Model):
    """Evento de auditoría inmutable. CU-040 (RQF-008/116/117/120, RN-033/034).

    No hereda `RegistroBase`: un evento de auditoría nunca se actualiza
    (RN-033), así que un campo `actualizado_en` insinuaría justo lo
    contrario. Lleva su propio `creado_en`.

    Identidad histórica: `content_type`/`object_id`/`objeto` (GenericFK)
    sirven únicamente para navegar al objeto mientras exista. La identidad
    permanente del evento — la que sobrevive aunque el objeto o su
    `ContentType` desaparezcan — vive en `modelo` (texto plano
    "app_label.model") y `objeto_repr` (representación legible capturada en
    el momento del evento). `content_type` usa `PROTECT`: una limpieza de
    contenttypes nunca debe arrastrar silenciosamente auditoría con ella.
    """

    class Accion(models.TextChoices):
        CREAR = "CREAR", "Crear"
        ACTUALIZAR = "ACTUALIZAR", "Actualizar"
        ELIMINAR = "ELIMINAR", "Eliminar"

    class Origen(models.TextChoices):
        USUARIO = "USUARIO", "Usuario"
        SISTEMA = "SISTEMA", "Sistema"

    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    objeto = GenericForeignKey("content_type", "object_id")

    modelo = models.CharField(max_length=150)
    objeto_repr = models.CharField(max_length=255)

    accion = models.CharField(max_length=10, choices=Accion.choices)
    origen = models.CharField(max_length=10, choices=Origen.choices)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="eventos_auditoria",
    )

    datos_anteriores = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    datos_nuevos = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)

    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(origen="USUARIO", usuario__isnull=False)
                    | Q(origen="SISTEMA", usuario__isnull=True)
                ),
                name="ck_registroauditoria_origen_coherente",
            ),
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["creado_en"]),
        ]
        ordering = ["-creado_en"]

    def __str__(self):
        return f"{self.accion} {self.modelo} #{self.object_id} — {self.creado_en:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "RegistroAuditoria es inmutable (RN-033): no puede actualizarse un evento existente."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(
            "RegistroAuditoria es inmutable (RN-033): no puede eliminarse un evento de auditoría."
        )
