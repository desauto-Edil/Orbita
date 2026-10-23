import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 2 del modelo configurable — subinstancias (recorridos por ítem y actividades en paralelo) y sus
    motivos de espera. Solo agrega columnas NULL y restricciones que las instancias existentes ya cumplen."""

    dependencies = [
        ("workflows", "0014_motivo_espera_elementos"),
        ("catalogo", "0027_bloques_contenedores"),
        ("tickets", "0025_datos_inicio_y_equipo"),
    ]

    operations = [
        migrations.AlterField(
            model_name="instanciaetapa",
            name="motivo_espera",
            field=models.CharField(
                blank=True,
                choices=[
                    ("TEMPORAL", "Espera temporal"),
                    ("TAREA", "Tarea"),
                    ("APROBACION", "Aprobación"),
                    ("ENTREGABLE", "Entregable"),
                    ("ELEMENTOS", "Tareas por elemento"),
                    ("RECORRIDO", "Recorridos por ítem"),
                    ("GRUPO", "Actividades en paralelo"),
                ],
                max_length=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="instanciaworkflow",
            name="instancia_padre",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="subinstancias", to="workflows.instanciaworkflow",
            ),
        ),
        migrations.AddField(
            model_name="instanciaworkflow",
            name="ejecucion_padre",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="subinstancias", to="workflows.instanciaetapa",
            ),
        ),
        migrations.AddField(
            model_name="instanciaworkflow",
            name="elemento",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="recorridos", to="tickets.elementoparrilla",
            ),
        ),
        migrations.AddField(
            model_name="instanciaworkflow",
            name="bloque_rama",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="ramas_ejecutadas", to="catalogo.bloqueoperativo",
            ),
        ),
        migrations.AddConstraint(
            model_name="instanciaworkflow",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("bloque_rama__isnull", True), ("ejecucion_padre__isnull", True),
                        ("elemento__isnull", True), ("instancia_padre__isnull", True),
                    ),
                    models.Q(("ejecucion_padre__isnull", False), ("instancia_padre__isnull", False)),
                    _connector="OR",
                ),
                name="ck_instanciawf_subinstancia_coherente",
            ),
        ),
        migrations.AddConstraint(
            model_name="instanciaworkflow",
            constraint=models.UniqueConstraint(
                condition=models.Q(("elemento__isnull", False)), fields=("ejecucion_padre", "elemento"),
                name="uq_instanciawf_recorrido_elemento",
            ),
        ),
        migrations.AddConstraint(
            model_name="instanciaworkflow",
            constraint=models.UniqueConstraint(
                condition=models.Q(("bloque_rama__isnull", False)), fields=("ejecucion_padre", "bloque_rama"),
                name="uq_instanciawf_rama_general",
            ),
        ),
    ]
