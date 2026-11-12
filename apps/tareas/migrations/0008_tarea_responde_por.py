import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Actividad de un equipo con una persona que RESPONDE (recibe los avisos si no se hace)."""

    dependencies = [
        ("tareas", "0007_historial_reprogramada"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="tarea",
            name="responde_por",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
