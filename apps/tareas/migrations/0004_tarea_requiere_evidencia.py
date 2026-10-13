from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2D2 — `Tarea.requiere_evidencia`: la Tarea exige al menos una evidencia adjunta para completarse.
    Aditiva: las Tareas existentes quedan en False (comportamiento de siempre)."""

    dependencies = [
        ("tareas", "0003_tarea_cancelada"),
    ]

    operations = [
        migrations.AddField(
            model_name="tarea",
            name="requiere_evidencia",
            field=models.BooleanField(default=False),
        ),
    ]
