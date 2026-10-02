from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.5 — política de entrega del Servicio/Proceso. Aditiva: los registros
    existentes quedan con política vacía (flujo anterior a 4.5)."""

    dependencies = [("catalogo", "0007_definicionentregable")]

    operations = [
        migrations.AddField(
            model_name="servicio",
            name="politica_entrega",
            field=models.CharField(
                blank=True,
                choices=[
                    ("CIERRE_DIRECTO", "Cierre al entregar, sin esperar respuesta"),
                    ("PERIODO_OBSERVACIONES", "Periodo de observaciones"),
                ],
                default="",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="servicio",
            name="dias_observacion",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="servicio",
            constraint=models.CheckConstraint(
                condition=(
                    Q(politica_entrega="PERIODO_OBSERVACIONES", dias_observacion__isnull=False, dias_observacion__gte=1)
                    | (~Q(politica_entrega="PERIODO_OBSERVACIONES") & Q(dias_observacion__isnull=True))
                ),
                name="ck_servicio_politica_entrega_coherente",
            ),
        ),
    ]
