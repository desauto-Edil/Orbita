"""Recordatorio de aprobaciones a partir de UN día (decisión del usuario, 2026-11-11): quien tiene una aprobación
pendiente más de un día recibe recordatorios por correo, en cualquier tipo de ticket. Solo cambia el valor global
si sigue en el valor por defecto anterior (2); un valor elegido en Configuración › Notificaciones se respeta."""

from django.db import migrations


def a_un_dia(apps, schema_editor):
    apps.get_model("notificaciones", "ConfiguracionNotificaciones").objects.filter(pk=1, aprobacion_dias=2).update(
        aprobacion_dias=1
    )


def a_dos_dias(apps, schema_editor):
    apps.get_model("notificaciones", "ConfiguracionNotificaciones").objects.filter(pk=1, aprobacion_dias=1).update(
        aprobacion_dias=2
    )


class Migration(migrations.Migration):
    dependencies = [("notificaciones", "0003_correo_de_conversacion")]

    operations = [migrations.RunPython(a_un_dia, a_dos_dias)]
