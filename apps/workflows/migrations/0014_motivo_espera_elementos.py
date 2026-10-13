from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2D2 — `ELEMENTOS` como motivo de espera externa del bloque TAREAS_ELEMENTOS (solo `choices`: sin
    cambio de esquema ni de datos)."""

    dependencies = [
        ("workflows", "0013_instancia_cancelada"),
    ]

    operations = [
        migrations.AlterField(
            model_name="instanciaetapa",
            name="motivo_espera",
            field=models.CharField(
                blank=True,
                choices=[
                    ("TEMPORAL", "Espera temporal"),
                    ("TAREA", "Tarea"),
                    ("APROBACION", "Aprobación"),
                    ("ENTREGABLE", "Entregable"),
                    ("ELEMENTOS", "Tareas por elemento"),
                ],
                max_length=10,
                null=True,
            ),
        ),
    ]
