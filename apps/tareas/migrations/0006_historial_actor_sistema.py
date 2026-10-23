import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 3 — el historial de una tarea admite acciones del Sistema (actor NULL), como el resto del proyecto."""

    dependencies = [
        ("tareas", "0005_fecha_prevista_verificacion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="historialtarea",
            name="actor",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
