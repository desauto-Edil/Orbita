from django.db import migrations

PERMISOS = [
    ("usuarios.administrar", "Administrar usuarios"),
    ("organizacion.administrar", "Administrar áreas y unidades de negocio"),
    ("permisos.administrar", "Administrar roles, permisos y asignaciones"),
    ("sistema.configurar", "Configurar la identidad del sistema"),
]


def sembrar_permisos(apps, schema_editor):
    """Permisos funcionales que gobiernan cada sección de Configuración
    (`apps/core/configuracion.py`), en alcance GLOBAL. Categorías reutiliza
    `catalogo.administrar`, que ya existe.

    No crea `RolFuncional` ni `AsignacionRol`: a quién se le otorgan es una
    decisión administrable, no de esta migración. Idempotente.
    """
    Permiso = apps.get_model("core", "Permiso")
    for codigo, nombre in PERMISOS:
        Permiso.objects.get_or_create(codigo=codigo, defaults={"nombre": nombre})


def revertir_permisos(apps, schema_editor):
    Permiso = apps.get_model("core", "Permiso")
    Permiso.objects.filter(codigo__in=[codigo for codigo, _ in PERMISOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_configuracion_sistema"),
    ]

    operations = [
        migrations.RunPython(sembrar_permisos, revertir_permisos),
    ]
