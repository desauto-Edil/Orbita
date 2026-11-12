"""Lista de verificación con conclusión (2026-11-12): cada punto puede tener una pregunta y guarda la conclusión
de quien hace la actividad."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tareas", "0008_tarea_responde_por")]

    operations = [
        migrations.AddField(
            model_name="itemverificaciontarea",
            name="pregunta",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="itemverificaciontarea",
            name="conclusion",
            field=models.TextField(blank=True, default=""),
        ),
    ]
