import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """4.B1 — bloque operativo ENTREGABLE: nuevo tipo y referencia a la definición.

    Aditiva. NO elimina ni migra nada de ESPERA: el valor sigue en `choices` para leer
    y ejecutar configuraciones históricas; solo deja de poder crearse (regla de dominio)."""

    dependencies = [
        ("catalogo", "0016_claves_estables"),
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
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="bloqueoperativo",
            name="definicion_entregable",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="bloques_operativos", to="catalogo.definicionentregable",
            ),
        ),
    ]
