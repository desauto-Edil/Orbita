from django.db import migrations

NOMBRE_TAREA = "workflows.reanudar_esperas_vencidas"
TASK_PATH = "apps.workflows.tasks.reanudar_esperas_vencidas"


def crear_tarea_periodica(apps, schema_editor):
    """Registra `reanudar_esperas_vencidas` en Celery Beat — 3.2.x.

    Reutiliza `django_celery_beat` (ya instalado y ya fijado como
    `CELERY_BEAT_SCHEDULER` desde Sprint 0): no se introduce un scheduler
    nuevo ni `CELERYBEAT_SCHEDULE` estático en `settings.py`, que sería una
    segunda fuente de configuración de periodicidad conviviendo con la que
    ya eligió el proyecto. Cada 1 minuto es suficiente para RQF-067 — no
    necesita precisión de segundos, y "minutes" se hardcodea (en vez de usar
    `IntervalSchedule.MINUTES`) porque un modelo histórico de `RunPython`
    solo reconstruye campos/Meta, no atributos de clase.

    Idempotente (`get_or_create`), mismo patrón que
    `0002_seed_permisos_workflows.py`/`0004_seed_permiso_workflows_ejecutar.py`.
    """
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    intervalo, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
    PeriodicTask.objects.get_or_create(
        name=NOMBRE_TAREA,
        defaults={"task": TASK_PATH, "interval": intervalo},
    )


def eliminar_tarea_periodica(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=NOMBRE_TAREA).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0004_seed_permiso_workflows_ejecutar"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(crear_tarea_periodica, eliminar_tarea_periodica),
    ]
