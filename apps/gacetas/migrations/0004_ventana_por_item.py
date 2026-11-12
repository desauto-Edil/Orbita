"""Ventana de revisión propia de un ítem en una gaceta (la duración que elige su aprobador al publicarlo a los
interesados; requerimiento nuevo, demo «Diseño de nuevas soluciones»). Vacía = rige el periodo de la gaceta."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gacetas", "0003_gacetas_sin_aprobacion")]

    operations = [
        migrations.AddField(
            model_name="participacionitemgaceta",
            name="vence_revision_en",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
