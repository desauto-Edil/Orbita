# Bloque 3.1 — copia congelada en el ticket de las columnas editables por la tarea de cada elemento.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0027_participante_parrilla"),
    ]

    operations = [
        migrations.AddField(
            model_name="configuraciontareasticket",
            name="columnas_editables",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
