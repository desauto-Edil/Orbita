"""Versión 1 — paso 2 de 2: la cuenta es un correo, sin usuario, contraseña, is_staff, is_superuser, grupos ni
permisos de Django (el acceso es por roles y permisos de Órbita). La sesión se firma con `clave_sesion`.

IRREVERSIBLE en la práctica: borra columnas. Haz un respaldo de la base de datos antes de aplicarla."""

import django.utils.timezone
from django.db import migrations, models
from django.db.models.functions import Lower

import apps.core.models.identidad


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_correo_como_identidad_datos"),
    ]

    operations = [
        migrations.RemoveField(model_name="usuario", name="username"),
        migrations.RemoveField(model_name="usuario", name="password"),
        migrations.RemoveField(model_name="usuario", name="is_staff"),
        migrations.RemoveField(model_name="usuario", name="is_superuser"),
        migrations.RemoveField(model_name="usuario", name="groups"),
        migrations.RemoveField(model_name="usuario", name="user_permissions"),
        migrations.AddField(
            model_name="usuario",
            name="clave_sesion",
            field=models.CharField(
                default=apps.core.models.identidad.nueva_clave_sesion, editable=False, max_length=64
            ),
        ),
        migrations.AlterField(
            model_name="usuario",
            name="email",
            field=models.EmailField(
                error_messages={"unique": "Ya existe una persona con ese correo."}, max_length=254, unique=True,
                verbose_name="correo",
            ),
        ),
        migrations.AlterField(
            model_name="usuario",
            name="first_name",
            field=models.CharField(blank=True, max_length=150, verbose_name="nombres"),
        ),
        migrations.AlterField(
            model_name="usuario",
            name="last_name",
            field=models.CharField(blank=True, max_length=150, verbose_name="apellidos"),
        ),
        migrations.AlterField(
            model_name="usuario",
            name="is_active",
            field=models.BooleanField(
                default=True,
                help_text="Una cuenta inactiva no puede ingresar; su historial se conserva. Desactivar en lugar de borrar.",
                verbose_name="cuenta activa",
            ),
        ),
        migrations.AlterField(
            model_name="usuario",
            name="date_joined",
            field=models.DateTimeField(default=django.utils.timezone.now, verbose_name="fecha de alta"),
        ),
        migrations.AddConstraint(
            model_name="usuario",
            constraint=models.UniqueConstraint(Lower("email"), name="uq_usuario_correo_sin_mayusculas"),
        ),
        migrations.AlterModelOptions(
            name="usuario",
            options={"verbose_name": "persona", "verbose_name_plural": "personas"},
        ),
        migrations.AlterModelManagers(
            name="usuario",
            managers=[("objects", apps.core.models.identidad.UsuarioManager())],
        ),
    ]
