from django.db import migrations

NOMBRE_TAREA = "tickets.cerrar_entregas_vencidas"
TASK_PATH = "apps.tickets.tasks.cerrar_entregas_vencidas"


def crear_tarea_periodica(apps, schema_editor):
    """Registra el cierre automático por vencimiento (4.5) en Celery Beat.

    Mismo patrón que `workflows/0005_seed_periodic_task_reanudar_esperas`:
    reutiliza `django_celery_beat` (ya fijado como scheduler) sin crear un
    scheduler nuevo. Idempotente (`get_or_create`)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    intervalo, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
    PeriodicTask.objects.get_or_create(name=NOMBRE_TAREA, defaults={"task": TASK_PATH, "interval": intervalo})


def eliminar_tarea_periodica(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=NOMBRE_TAREA).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0010_entrega_formal"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(crear_tarea_periodica, eliminar_tarea_periodica)]
