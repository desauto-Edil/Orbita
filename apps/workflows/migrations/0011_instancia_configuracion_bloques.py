import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0010_transicion_bloque_operativo"),
        ("workflows", "0010_workflow_modo_fases"),
    ]

    operations = [
        migrations.AddField(
            model_name="instanciaworkflow",
            name="configuracion_ejecucion_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="instancias_workflow",
                to="catalogo.configuracionejecucionversion",
            ),
        ),
        migrations.AlterField(
            model_name="instanciaetapa",
            name="etapa",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ejecuciones",
                to="workflows.etapa",
            ),
        ),
        migrations.AddField(
            model_name="instanciaetapa",
            name="bloque_operativo",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ejecuciones",
                to="catalogo.bloqueoperativo",
            ),
        ),
        migrations.AddField(
            model_name="instanciaetapa",
            name="fase_workflow",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ejecuciones_bloque",
                to="workflows.faseworkflow",
            ),
        ),
        migrations.AddField(
            model_name="instanciaetapa",
            name="transicion_bloque_tomada",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="catalogo.transicionbloqueoperativo",
            ),
        ),
    ]
