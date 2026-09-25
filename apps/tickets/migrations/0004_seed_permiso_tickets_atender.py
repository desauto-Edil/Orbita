from django.db import migrations

CODIGO = "tickets.atender"
NOMBRE = "Atender tickets (tomar, asignar, reasignar)"


def sembrar_permiso(apps, schema_editor):
    """Único dato sembrado por 2.3: el permiso funcional que autoriza la
    cola de atención y las operaciones tomar/asignar/reasignar (CU-017).
    Igual que `catalogo.administrar`/`formulario.administrar`, no crea
    `RolFuncional` ni `AsignacionRol` — a quién se le otorga, y en qué
    alcance GLOBAL/AREA/UNIDAD, es 100% administrable desde el Admin.
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
        ("tickets", "0003_ticket_equipo_responsable_ticket_usuario_responsable_and_more"),
        ("core", "0005_seed_permiso_auditoria_consultar"),
    ]

    operations = [
        migrations.RunPython(sembrar_permiso, revertir_permiso),
    ]
