import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.G2C1 — nuevo tipo de entregable ELEMENTOS (revisión de los elementos de la estructura del
    Proceso) y su configuración: columnas revisadas (por clave estable) y revisores (persona o equipo).
    Aditiva: ninguna definición existente cambia y no se crea ningún dato."""

    dependencies = [
        ("catalogo", "0021_definicion_parrilla"),
        ("core", "0005_seed_permiso_auditoria_consultar"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="definicionentregable",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("TEXTO", "Texto"), ("ARCHIVO", "Archivo"), ("ENLACE", "Enlace"),
                    ("CONFIRMACION", "Confirmación"), ("ELEMENTOS", "Revisión de elementos"),
                ],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="ColumnaRevisadaDefinicion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("clave", models.SlugField(max_length=60)),
                (
                    "definicion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="columnas_revisadas",
                        to="catalogo.definicionentregable",
                    ),
                ),
            ],
            options={"ordering": ["pk"]},
        ),
        migrations.CreateModel(
            name="RevisorDefinicion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "definicion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="revisores",
                        to="catalogo.definicionentregable",
                    ),
                ),
                (
                    "equipo",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.equipo",
                    ),
                ),
                (
                    "usuario",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["pk"]},
        ),
        migrations.AddConstraint(
            model_name="columnarevisadadefinicion",
            constraint=models.UniqueConstraint(fields=("definicion", "clave"), name="uq_columnarevisada_definicion_clave"),
        ),
        migrations.AddConstraint(
            model_name="revisordefinicion",
            constraint=models.CheckConstraint(
                condition=Q(usuario__isnull=False, equipo__isnull=True) | Q(usuario__isnull=True, equipo__isnull=False),
                name="ck_revisordefinicion_usuario_o_equipo",
            ),
        ),
        migrations.AddConstraint(
            model_name="revisordefinicion",
            constraint=models.UniqueConstraint(
                condition=Q(usuario__isnull=False), fields=("definicion", "usuario"), name="uq_revisordefinicion_usuario"
            ),
        ),
        migrations.AddConstraint(
            model_name="revisordefinicion",
            constraint=models.UniqueConstraint(
                condition=Q(equipo__isnull=False), fields=("definicion", "equipo"), name="uq_revisordefinicion_equipo"
            ),
        ),
    ]
