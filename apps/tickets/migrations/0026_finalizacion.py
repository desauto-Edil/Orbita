from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 3 del modelo configurable — reglas de finalización congeladas en el Ticket y foto de resultados en
    la resolución. Ambas columnas son NULL para lo existente: nada cambia para los tickets anteriores."""

    dependencies = [
        ("tickets", "0025_datos_inicio_y_equipo"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="reglas_finalizacion",
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="resolucionticket",
            name="resumen_resultados",
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
    ]
