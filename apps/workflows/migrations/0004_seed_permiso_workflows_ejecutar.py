from django.db import migrations

PERMISOS = [
    ("workflows.ejecutar", "Ejecutar workflows (iniciar, avanzar y reanudar instancias)"),
]


def sembrar_permiso(apps, schema_editor):
    """Único dato sembrado por 3.2 (W.8): el permiso que autoriza a un
    usuario a iniciar/avanzar/reanudar una `InstanciaWorkflow` a través de
    `apps.workflows.motor` cuando el origen de la operación es un usuario
    real (`RegistroAuditoria.Origen.USUARIO`) — separado de
    `workflows.administrar` (0002): administrar la definición y disparar
    ejecuciones son capacidades independientes (W.8, corrección aprobada).

    Idempotente (`get_or_create`), mismo patrón que `0002_seed_permisos_
    workflows.py`. No crea `RolFuncional` ni `AsignacionRol` — a quién se
    le otorga es 100% administrable desde el Admin.
    """
    Permiso = apps.get_model("core", "Permiso")
    for codigo, nombre in PERMISOS:
        Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre})


def revertir_permiso(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo__in=[codigo for codigo, _ in PERMISOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0003_instanciaworkflow_instanciaetapa"),
    ]

    operations = [
        migrations.RunPython(sembrar_permiso, revertir_permiso),
    ]
