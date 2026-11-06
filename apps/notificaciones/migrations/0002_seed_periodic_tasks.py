from django.db import migrations

TAREAS = (
    # (nombre, ruta, cada, periodo)
    ("notificaciones.evaluar_alertas", "apps.notificaciones.tasks.evaluar_alertas", 1, "hours"),
    ("notificaciones.reintentar_correos", "apps.notificaciones.tasks.reintentar_correos", 15, "minutes"),
)


def crear_tareas_periodicas(apps, schema_editor):
    """Sprint 7 (adelanto): el evaluador de alertas y recordatorios (cada hora, UNA tarea para todo) y el
    reintento de correos que no se pudieron encolar. Mismo patrón que `tickets.0017`. Idempotente."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for nombre, ruta, cada, periodo in TAREAS:
        intervalo, _ = IntervalSchedule.objects.get_or_create(every=cada, period=periodo)
        PeriodicTask.objects.get_or_create(name=nombre, defaults={"task": ruta, "interval": intervalo})


def eliminar_tareas_periodicas(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[t[0] for t in TAREAS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notificaciones", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(crear_tareas_periodicas, eliminar_tareas_periodicas)]
