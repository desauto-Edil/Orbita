from django.db import migrations

CODIGO = "formulario.administrar"
NOMBRE = "Administrar formularios configurables"


def sembrar_permiso(apps, schema_editor):
    """Único dato sembrado por 1.2: el permiso funcional que autoriza
    diseñar/versionar/activar `Formulario` vía Django Admin (CU-012/013),
    en alcance GLOBAL (misma limitación documentada de la interfaz
    administrativa provisional — ver `apps/catalogo/admin.py`).

    Deliberadamente separado de `catalogo.administrar` (0002) — el Excel usa
    actores distintos para CU-010 ("Gestor de Servicios") y CU-012/013
    ("Gestor autorizado"), y la separación entre autorización de Catálogo y
    de Form Builder fue aprobada explícitamente por el usuario.

    No crea `RolFuncional` ni `AsignacionRol` — a quién se le otorga es una
    decisión 100% administrable desde el Admin, no de esta migración.
    Idempotente: `get_or_create` no duplica el permiso si la migración se
    corre más de una vez.
    """
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.get_or_create(codigo=CODIGO, defaults={"nombre": NOMBRE})


def revertir_permiso(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo=CODIGO).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0003_formulario_servicio_formulario_formularioversion_and_more"),
    ]

    operations = [
        migrations.RunPython(sembrar_permiso, revertir_permiso),
    ]
