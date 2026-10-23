from django.db import migrations

NOMBRE_TAREA = "gacetas.reconciliar_vencimientos"
TASK_PATH = "apps.gacetas.tasks.reconciliar_vencimientos"


def crear_tarea_periodica(apps, schema_editor):
    """Registra el reconciliador de vencimientos de gacetas (Fase 3) en Celery Beat: UNA tarea para todas las
    gacetas, cada 15 minutos. Mismo patrón que `tickets.0017_seed_periodic_task_generar_ejecuciones`.
    Idempotente (`get_or_create`)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    intervalo, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")
    PeriodicTask.objects.get_or_create(name=NOMBRE_TAREA, defaults={"task": TASK_PATH, "interval": intervalo})


def eliminar_tarea_periodica(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=NOMBRE_TAREA).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("gacetas", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(crear_tarea_periodica, eliminar_tarea_periodica)]
