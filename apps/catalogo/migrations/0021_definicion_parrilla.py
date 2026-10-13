import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2B1 — `DefinicionParrilla`: estructura repetible OPCIONAL de un Proceso (columnas = Campos
    de un Formulario propio). Aditiva: ningún Proceso existente la tiene (sin fila = sin parrilla) y
    no se crea ningún dato."""

    dependencies = [
        ("catalogo", "0020_programacion_proceso"),
    ]

    operations = [
        migrations.CreateModel(
            name="DefinicionParrilla",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "nombre_elemento",
                    models.CharField(
                        default="Elemento", help_text="Cómo se llama cada fila (p. ej. «Publicación»).",
                        max_length=60,
                    ),
                ),
                ("activa", models.BooleanField(default=True)),
                (
                    "formulario",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, related_name="definicion_parrilla",
                        to="catalogo.formulario",
                    ),
                ),
                (
                    "servicio",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="definicion_parrilla",
                        to="catalogo.servicio",
                    ),
                ),
            ],
        ),
    ]
