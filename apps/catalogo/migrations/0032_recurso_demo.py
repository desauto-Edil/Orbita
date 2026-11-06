import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Bloque 5.6 — identidad de los recursos de la demostración (`preparar_demo_orbita`). Tabla nueva: nada
    existente cambia."""

    dependencies = [
        ("catalogo", "0031_tareas_elemento_columnas_editables"),
        ("workflows", "0017_fase_clave"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RecursoDemo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "caso",
                    models.CharField(
                        choices=[
                            ("PARRILLA", "Parrilla mensual de publicaciones"),
                            ("WEBINAR", "Webinar"),
                            ("PIEZA_GRAFICA", "Pieza gráfica"),
                            ("PRESENTACIONES", "Creación de presentaciones comerciales"),
                        ],
                        max_length=30, unique=True,
                    ),
                ),
                ("version_contrato", models.PositiveIntegerField()),
                (
                    "categoria_creada",
                    models.BooleanField(
                        default=False,
                        help_text="La categoría la creó la demo (solo entonces la demo puede retirarla).",
                    ),
                ),
                (
                    "parametros",
                    models.JSONField(
                        blank=True, default=dict,
                        help_text="Personas, equipos y calendario elegidos al preparar (ids), que reutilizan publicar y ejemplos.",
                    ),
                ),
                ("retirado_en", models.DateTimeField(blank=True, null=True)),
                (
                    "categoria",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalogo.categoria",
                    ),
                ),
                (
                    "preparado_por",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "servicio",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, related_name="recurso_demo",
                        to="catalogo.servicio",
                    ),
                ),
                (
                    "workflow",
                    models.ForeignKey(
                        blank=True, help_text="Plantilla de fases propia del caso (los Servicios sin flujo no tienen).",
                        null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="workflows.workflow",
                    ),
                ),
            ],
            options={"ordering": ["pk"]},
        ),
    ]
