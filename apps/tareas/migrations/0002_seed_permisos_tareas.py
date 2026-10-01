from django.db import migrations

PERMISOS = [
    ("tareas.consultar", "Consultar tareas (visibilidad amplia por alcance)"),
    ("tareas.gestionar", "Gestionar tareas (asignar, reasignar, delegar a un tercero)"),
]


def sembrar_permisos(apps, schema_editor):
    """Único dato sembrado por 3.3 (W.16, corrección aprobada): solo 2
    permisos, no 4 — "tomar/iniciar/completar/comentar" nunca exigen un
    permiso nuevo, dependen de la relación operacional con la Tarea (ver
    `apps/tareas/autorizacion.py`). Alcance GLOBAL únicamente (ver esa
    misma nota: Tarea no tiene Área/Unidad propia ni heredada documentada).

    Idempotente (`get_or_create`), mismo patrón que
    `apps/workflows/migrations/0002_seed_permisos_workflows.py`. No crea
    `RolFuncional` ni `AsignacionRol` — a quién se le otorga es 100%
    administrable desde el Admin.
    """
    Permiso = apps.get_model("core", "Permiso")
    for codigo, nombre in PERMISOS:
        Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre})


def revertir_permisos(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo__in=[codigo for codigo, _ in PERMISOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tareas", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(sembrar_permisos, revertir_permisos),
    ]
