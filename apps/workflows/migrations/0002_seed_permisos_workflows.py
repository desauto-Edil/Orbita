from django.db import migrations

PERMISOS = [
    ("workflows.administrar", "Administrar workflows (crear, editar, versionar, activar)"),
    ("workflows.consultar", "Consultar workflows"),
]


def sembrar_permisos(apps, schema_editor):
    """Único dato sembrado por 3.1: los dos permisos funcionales que
    autorizan administrar/consultar `Workflow` vía Django Admin (CU-020,
    X.5), en alcance GLOBAL (misma limitación ya documentada de la
    interfaz administrativa provisional — ver `apps/workflows/admin.py`).

    No crea `RolFuncional` ni `AsignacionRol` — a quién se le otorga es una
    decisión 100% administrable desde el Admin, no de esta migración.
    Idempotente: `get_or_create` no duplica los permisos si la migración se
    corre más de una vez.
    """
    Permiso = apps.get_model("core", "Permiso")
    for codigo, nombre in PERMISOS:
        Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre})


def revertir_permisos(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo__in=[codigo for codigo, _ in PERMISOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(sembrar_permisos, revertir_permisos),
    ]
