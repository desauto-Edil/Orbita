from django.db import migrations

CODIGO = "catalogo.administrar"
NOMBRE = "Administrar catálogo de servicios"


def sembrar_permiso(apps, schema_editor):
    """Único dato sembrado por 1.1: el permiso funcional que autoriza
    gestionar Categoria/Servicio vía Django Admin (CU-010), en alcance
    GLOBAL (limitación documentada de la interfaz administrativa
    provisional — ver `apps/catalogo/admin.py`).

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
        ("catalogo", "0001_initial"),
        ("core", "0005_seed_permiso_auditoria_consultar"),
    ]

    operations = [
        migrations.RunPython(sembrar_permiso, revertir_permiso),
    ]
