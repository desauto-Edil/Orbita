import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2D2 — `TareaElemento.instancia_etapa`: la ejecución del bloque TAREAS_ELEMENTOS que materializó la
    tarea. Aditiva y nula: los vínculos existentes (materializados fuera del flujo) quedan sin ejecución."""

    dependencies = [
        ("tickets", "0020_tareas_por_elemento"),
        ("workflows", "0014_motivo_espera_elementos"),
    ]

    operations = [
        migrations.AddField(
            model_name="tareaelemento",
            name="instancia_etapa",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="tareas_elementos",
                to="workflows.instanciaetapa",
            ),
        ),
    ]
