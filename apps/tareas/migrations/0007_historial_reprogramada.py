from django.db import migrations, models


class Migration(migrations.Migration):
    """Bloque 5.4 — evento REPROGRAMADA del historial de una tarea (solo cambian las opciones; sin cambios en la BD)."""

    dependencies = [
        ("tareas", "0006_historial_actor_sistema"),
    ]

    operations = [
        migrations.AlterField(
            model_name="historialtarea",
            name="tipo_evento",
            field=models.CharField(
                choices=[
                    ("TOMADA", "Tomada"), ("ASIGNADA", "Asignada"), ("REASIGNADA", "Reasignada"),
                    ("DELEGADA", "Delegada"), ("INICIADA", "Iniciada"), ("COMPLETADA", "Completada"),
                    ("CANCELADA", "Cancelada"), ("VERIFICACION", "Lista de verificación"),
                    ("REPROGRAMADA", "Reprogramada"),
                ],
                max_length=15,
            ),
        ),
    ]
