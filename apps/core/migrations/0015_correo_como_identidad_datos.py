"""Versión 1 — paso 1 de 2: el correo pasa a ser la identidad de la cuenta.

Antes de retirar `username` (0016) verifica que TODA cuenta tenga un correo, que no se repita sin importar
mayúsculas y lo normaliza (minúsculas, sin espacios). Si algo no cumple, se DETIENE sin cambiar nada y lista las
cuentas a corregir: nunca se inventa un correo ni se fusionan cuentas. Va sola (sin cambios de esquema) para que la
corrección de datos y el ALTER TABLE no compartan transacción."""

from collections import defaultdict

from django.db import migrations


def verificar_y_normalizar(apps, schema_editor):
    Usuario = apps.get_model("core", "Usuario")
    sin_correo, por_correo = [], defaultdict(list)
    for usuario in Usuario.objects.order_by("pk"):
        correo = (usuario.email or "").strip().lower()
        if not correo:
            sin_correo.append(f"#{usuario.pk} «{usuario.username}»")
        else:
            por_correo[correo].append(usuario)
    repetidos = {c: us for c, us in por_correo.items() if len(us) > 1}
    if sin_correo or repetidos:
        lineas = []
        if sin_correo:
            lineas.append("Cuentas sin correo: " + ", ".join(sin_correo))
        for correo, usuarios in repetidos.items():
            lineas.append(f"Correo repetido {correo}: " + ", ".join(f"#{u.pk} «{u.username}»" for u in usuarios))
        raise RuntimeError(
            "No se puede usar el correo como identidad todavía. Corrige estas cuentas (en Configuración › Usuarios) "
            "y vuelve a correr migrate:\n- " + "\n- ".join(lineas)
        )
    for correo, (usuario,) in por_correo.items():
        if usuario.email != correo:
            Usuario.objects.filter(pk=usuario.pk).update(email=correo)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_seed_permiso_plataforma"),
    ]

    operations = [
        migrations.RunPython(verificar_y_normalizar, migrations.RunPython.noop),
    ]
