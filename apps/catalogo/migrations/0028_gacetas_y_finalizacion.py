import apps.catalogo.claves
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 3 del modelo configurable — definiciones de gaceta (N por Proceso), reglas de finalización (1:1,
    opcional) y el bloque GACETA. Solo crea tablas y amplía `choices`: nada existente cambia."""

    dependencies = [
        ("catalogo", "0027_bloques_contenedores"),
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
                    ("GRUPO_ACTIVIDADES", "Actividades en paralelo"),
                    ("RECORRIDO_ITEMS", "Recorrido por ítem"),
                    ("GACETA", "Gaceta"),
                ],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="DefinicionGaceta",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("clave", models.SlugField(max_length=60, validators=[apps.catalogo.claves.validar_clave])),
                ("nombre", models.CharField(max_length=150)),
                ("descripcion", models.TextField(blank=True)),
                ("activa", models.BooleanField(default=True)),
                ("orden", models.PositiveIntegerField(default=0)),
                ("version", models.PositiveIntegerField(default=1)),
                ("configuracion", models.JSONField(blank=True, default=dict)),
                (
                    "servicio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="definiciones_gaceta",
                        to="catalogo.servicio",
                    ),
                ),
            ],
            options={
                "ordering": ["servicio_id", "orden", "pk"],
                "constraints": [
                    models.UniqueConstraint(fields=("servicio", "clave"), name="uq_definiciongaceta_servicio_clave"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReglaFinalizacion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("configuracion", models.JSONField(blank=True, default=dict)),
                (
                    "servicio",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="regla_finalizacion",
                        to="catalogo.servicio",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
