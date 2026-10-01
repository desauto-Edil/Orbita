from django.db import migrations

PERMISOS = [
    ("aprobaciones.consultar", "Consultar aprobaciones (visibilidad amplia por alcance)"),
    ("aprobaciones.gestionar", "Gestionar aprobaciones (reasignar a un tercero)"),
]


def sembrar_permisos(apps, schema_editor):
    """Solo 2 permisos (mismo criterio W.16 ya aprobado para Tareas):
    decidir (aprobar/rechazar/devolver) nunca exige un permiso nuevo,
    depende de la relación directa con la `Aprobacion` (RN-025, ver
    `apps/aprobaciones/autorizacion.py`). Alcance GLOBAL únicamente
    (mismo hallazgo documental que Tareas).

    Idempotente (`get_or_create`), mismo patrón que
    `apps/tareas/migrations/0002_seed_permisos_tareas.py`. No crea
    `RolFuncional` ni `AsignacionRol` — a quién se le otorga es 100%
    administrable desde el Admin."""
    Permiso = apps.get_model("core", "Permiso")
    for codigo, nombre in PERMISOS:
        Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre})


def revertir_permisos(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo__in=[codigo for codigo, _ in PERMISOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("aprobaciones", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(sembrar_permisos, revertir_permisos),
    ]
