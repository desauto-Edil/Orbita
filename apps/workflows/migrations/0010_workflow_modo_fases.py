import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0009_seed_permiso_workflows_vincular"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflow",
            name="modo",
            field=models.CharField(
                choices=[
                    ("LEGACY_EJECUTABLE", "Legacy ejecutable"),
                    ("PLANTILLA_FASES", "Plantilla de fases"),
                ],
                default="LEGACY_EJECUTABLE",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="FaseWorkflow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("nombre", models.CharField(max_length=150)),
                ("descripcion", models.TextField(blank=True)),
                ("orden", models.PositiveIntegerField()),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fases",
                        to="workflows.workflowversion",
                    ),
                ),
            ],
            options={
                "ordering": ["version_id", "orden", "id"],
            },
        ),
        migrations.CreateModel(
            name="TransicionFaseWorkflow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("nombre", models.CharField(blank=True, max_length=150)),
                ("prioridad", models.PositiveIntegerField(default=0)),
                (
                    "fase_destino",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transiciones_entrantes",
                        to="workflows.faseworkflow",
                    ),
                ),
                (
                    "fase_origen",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transiciones_salientes",
                        to="workflows.faseworkflow",
                    ),
                ),
            ],
            options={
                "ordering": ["fase_origen_id", "prioridad", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="faseworkflow",
            constraint=models.UniqueConstraint(fields=("version", "orden"), name="uq_faseworkflow_version_orden"),
        ),
    ]
