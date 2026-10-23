# Bloque 3.1 — columnas que el responsable de una tarea por elemento abierta puede editar en su elemento.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalogo", "0030_participante_coleccion"),
    ]

    operations = [
        migrations.AddField(
            model_name="configuraciontareaselemento",
            name="columnas_editables",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
