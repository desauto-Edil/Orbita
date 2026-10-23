import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 4.B — posición visual de las actividades en el lienzo del Studio (solo presentación; el motor no la
    lee). Crea una tabla nueva: nada existente cambia."""

    dependencies = [
        ("catalogo", "0028_gacetas_y_finalizacion"),
    ]

    operations = [
        migrations.CreateModel(
            name="PosicionVisualBloque",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("clave", models.CharField(max_length=70)),
                ("x", models.IntegerField(default=0)),
                ("y", models.IntegerField(default=0)),
                (
                    "servicio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="posiciones_lienzo",
                        to="catalogo.servicio",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("servicio", "clave"), name="uq_posicion_lienzo_servicio_clave")
                ],
            },
        ),
    ]
