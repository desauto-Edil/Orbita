from django.db import migrations

CODIGO = "plataforma.administracion_avanzada"
NOMBRE = "Entrar a la administración avanzada"


def sembrar(apps, schema_editor):
    """Versión 1: reemplaza a `is_staff`/`is_superuser`. Solo el permiso (alcance GLOBAL): a quién se le otorga lo
    decide `manage.py otorgar_administrador` (el primero) y luego Configuración. Idempotente."""
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.get_or_create(codigo=CODIGO, defaults={"nombre": NOMBRE})


def revertir(apps, schema_editor):
    apps.get_model("core", "Permiso").objects.filter(codigo=CODIGO).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_jefatura_usuario"),
    ]

    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
