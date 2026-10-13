from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G1.5 — estado CANCELADA de una Tarea cuyo Ticket se canceló y su evento en el historial. Solo
    amplía opciones: ninguna Tarea existente cambia y no se crea ningún dato."""

    dependencies = [
        ("tareas", "0002_seed_permisos_tareas"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tarea",
            name="estado",
            field=models.CharField(
                choices=[
                    ("PENDIENTE", "Pendiente"), ("EN_PROGRESO", "En progreso"), ("COMPLETADA", "Completada"),
                    ("CANCELADA", "Cancelada"),
                ],
                default="PENDIENTE", max_length=15,
            ),
        ),
        migrations.AlterField(
            model_name="historialtarea",
            name="tipo_evento",
            field=models.CharField(
                choices=[
                    ("TOMADA", "Tomada"), ("ASIGNADA", "Asignada"), ("REASIGNADA", "Reasignada"),
                    ("DELEGADA", "Delegada"), ("INICIADA", "Iniciada"), ("COMPLETADA", "Completada"),
                    ("CANCELADA", "Cancelada"),
                ],
                max_length=15,
            ),
        ),
    ]
