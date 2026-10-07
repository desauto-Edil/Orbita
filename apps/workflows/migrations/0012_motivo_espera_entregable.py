from django.db import migrations, models


class Migration(migrations.Migration):
    """4.B1 — `ENTREGABLE` como motivo de espera externa (solo `choices`: sin cambio de
    esquema ni de datos)."""

    dependencies = [
        ("workflows", "0011_instancia_configuracion_bloques"),
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
                ],
                max_length=10,
                null=True,
            ),
        ),
    ]
