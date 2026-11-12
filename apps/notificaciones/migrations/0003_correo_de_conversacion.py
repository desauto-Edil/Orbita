"""Conversación del ticket: un mensaje a alguien fuera de línea espera unos minutos su correo
(`Notificacion.correo_pendiente_desde`) y una tarea de Beat, cada minuto, envía UN correo por persona, ticket y canal
con lo que siga sin leer (`apps.notificaciones.correo_conversacion`). Idempotente."""

from django.db import migrations, models

TAREA = ("notificaciones.correos_de_conversacion", "apps.notificaciones.tasks.correos_de_conversacion", 1, "minutes")


def crear_tarea(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    nombre, ruta, cada, periodo = TAREA
    intervalo, _ = IntervalSchedule.objects.get_or_create(every=cada, period=periodo)
    PeriodicTask.objects.get_or_create(name=nombre, defaults={"task": ruta, "interval": intervalo})


def eliminar_tarea(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TAREA[0]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notificaciones", "0002_seed_periodic_tasks"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificacion",
            name="correo_pendiente_desde",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(crear_tarea, eliminar_tarea),
    ]
