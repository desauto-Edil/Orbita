"""«Estoy conforme» en la gaceta (2026-11-12): cuando todos los interesados están conformes, termina antes."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("gacetas", "0004_ventana_por_item"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ConformidadGaceta",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creada_en", models.DateTimeField(auto_now_add=True)),
                ("gaceta", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conformidades", to="gacetas.gacetaticket")),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["gaceta_id", "creada_en", "pk"]},
        ),
    ]
