import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0008_servicio_politica_entrega"),
        ("workflows", "0010_workflow_modo_fases"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfiguracionEjecucionVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("numero", models.PositiveIntegerField()),
                (
                    "estado",
                    models.CharField(
                        choices=[("BORRADOR", "Borrador"), ("ACTIVA", "Activa"), ("HISTORICA", "Historica")],
                        default="BORRADOR",
                        max_length=10,
                    ),
                ),
                (
                    "servicio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="configuraciones_ejecucion",
                        to="catalogo.servicio",
                    ),
                ),
                (
                    "workflow_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="configuraciones_servicio",
                        to="workflows.workflowversion",
                    ),
                ),
            ],
            options={
                "ordering": ["servicio_id", "numero"],
            },
        ),
        migrations.CreateModel(
            name="BloqueOperativo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "tipo",
                    models.CharField(
                        choices=[
                            ("ACTIVIDAD", "Actividad"),
                            ("APROBACION", "Aprobacion"),
                            ("ESPERA", "Espera"),
                            ("DECISION", "Decision"),
                        ],
                        max_length=20,
                    ),
                ),
                ("nombre", models.CharField(max_length=150)),
                ("descripcion", models.TextField(blank=True)),
                ("orden", models.PositiveIntegerField()),
                ("configuracion", models.JSONField(blank=True, default=dict)),
                (
                    "fase",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bloques_operativos",
                        to="workflows.faseworkflow",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bloques",
                        to="catalogo.configuracionejecucionversion",
                    ),
                ),
            ],
            options={
                "ordering": ["version_id", "fase_id", "orden", "id"],
            },
        ),
        migrations.AddField(
            model_name="servicio",
            name="configuracion_ejecucion_activa",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="catalogo.configuracionejecucionversion",
            ),
        ),
        migrations.AddConstraint(
            model_name="configuracionejecucionversion",
            constraint=models.UniqueConstraint(fields=("servicio", "numero"), name="uq_configejec_servicio_numero"),
        ),
        migrations.AddConstraint(
            model_name="configuracionejecucionversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("estado", "ACTIVA")),
                fields=("servicio",),
                name="uq_configejec_servicio_activa",
            ),
        ),
        migrations.AddConstraint(
            model_name="bloqueoperativo",
            constraint=models.UniqueConstraint(
                fields=("version", "fase", "orden"), name="uq_bloqueop_version_fase_orden"
            ),
        ),
    ]
