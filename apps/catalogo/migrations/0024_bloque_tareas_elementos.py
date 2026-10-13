from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2D2 — `TAREAS_ELEMENTOS` como tipo de bloque operativo (solo `choices`: sin cambio de esquema ni
    de datos)."""

    dependencies = [
        ("catalogo", "0023_configuracion_tareas_elemento"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bloqueoperativo",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("ACTIVIDAD", "Actividad"),
                    ("APROBACION", "Aprobacion"),
                    ("ESPERA", "Espera"),
                    ("DECISION", "Decision"),
                    ("ENTREGABLE", "Entregable"),
                    ("TAREAS_ELEMENTOS", "Tareas por elemento"),
                ],
                max_length=20,
            ),
        ),
    ]
