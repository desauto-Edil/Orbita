from django.db import migrations

NOMBRE_TAREA = "tickets.generar_ejecuciones_programadas"
TASK_PATH = "apps.tickets.tasks.generar_ejecuciones_programadas"


def crear_tarea_periodica(apps, schema_editor):
    """Registra el reconciliador de Procesos programados (4.G1) en Celery Beat: UNA sola tarea
    periódica para todos los Procesos (no una por Proceso), cada hora. Mismo patrón que
    `0011_seed_periodic_task_cerrar_entregas`. Idempotente (`get_or_create`)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    intervalo, _ = IntervalSchedule.objects.get_or_create(every=1, period="hours")
    PeriodicTask.objects.get_or_create(name=NOMBRE_TAREA, defaults={"task": TASK_PATH, "interval": intervalo})


def eliminar_tarea_periodica(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=NOMBRE_TAREA).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0016_ticket_programado"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(crear_tarea_periodica, eliminar_tarea_periodica)]
