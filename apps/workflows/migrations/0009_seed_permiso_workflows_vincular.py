from django.db import migrations

PERMISOS = [
    (
        "workflows.vincular",
        "Vincular un workflow publicado a un servicio o proceso (sin poder modificarlo)",
    ),
]


def sembrar_permiso(apps, schema_editor):
    """Único dato sembrado por 4.6: el permiso que autoriza a elegir, desde
    Studio, un workflow ya publicado para un Servicio/Proceso — SIN poder
    crear, copiar ni modificar workflows (eso sigue siendo
    `workflows.administrar`). Distinto de `workflows.ejecutar` (0004), que
    autoriza a disparar instancias.

    Idempotente (`get_or_create`), mismo patrón que 0002 y 0004. No crea
    `RolFuncional` ni `AsignacionRol`: a quién se le otorga es 100%
    administrable desde el Admin; esta migración no asigna nada a nadie.
    """
    Permiso = apps.get_model("core", "Permiso")
    for codigo, nombre in PERMISOS:
        Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre})


def revertir_permiso(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo__in=[codigo for codigo, _ in PERMISOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0008_actores_dinamicos"),
    ]

    operations = [
        migrations.RunPython(sembrar_permiso, revertir_permiso),
    ]
