from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G1.5 — estado CANCELADA de la ejecución de un Ticket cancelado. Solo amplía las opciones del
    estado: ninguna instancia existente cambia y no se crea ningún dato."""

    dependencies = [
        ("workflows", "0012_motivo_espera_entregable"),
    ]

    operations = [
        migrations.AlterField(
            model_name="instanciaworkflow",
            name="estado",
            field=models.CharField(
                choices=[
                    ("EN_EJECUCION", "En ejecución"), ("EN_ESPERA", "En espera"), ("COMPLETADA", "Completada"),
                    ("ERROR", "Error"), ("CANCELADA", "Cancelada"),
                ],
                default="EN_EJECUCION", max_length=15,
            ),
        ),
    ]
