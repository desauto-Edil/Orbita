from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 3 — `GACETA` como motivo de espera del bloque que espera la resolución de una gaceta (solo
    `choices`)."""

    dependencies = [
        ("workflows", "0015_subinstancias"),
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
                    ("RECORRIDO", "Recorridos por ítem"),
                    ("GRUPO", "Actividades en paralelo"),
                    ("GACETA", "Gaceta"),
                ],
                max_length=10,
                null=True,
            ),
        ),
    ]
