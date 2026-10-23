"""Fase 1 (modelo configurable de procesos) — varias colecciones de ítems por Proceso.

`DefinicionParrilla.servicio` pasa de 1:1 a FK, con `clave` estable (única por Proceso) y `orden`.
Cada definición existente recibe una clave derivada de su nombre de elemento y `orden=1`: sigue siendo
la colección por defecto de su Proceso. No cambia ningún otro dato."""

import django.core.validators  # noqa: F401
import django.db.models.deletion
from django.db import migrations, models

import apps.catalogo.claves


def asignar_claves(apps, schema_editor):
    from apps.catalogo.claves import clave_desde_texto

    DefinicionParrilla = apps.get_model("catalogo", "DefinicionParrilla")
    for definicion in DefinicionParrilla.objects.all().iterator():
        definicion.clave = clave_desde_texto(definicion.nombre_elemento, por_defecto="elementos")
        definicion.orden = 1
        definicion.save(update_fields=["clave", "orden"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalogo", "0024_bloque_tareas_elementos"),
    ]

    operations = [
        migrations.AlterField(
            model_name="definicionparrilla",
            name="servicio",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="definiciones_parrilla",
                to="catalogo.servicio",
            ),
        ),
        migrations.AddField(
            model_name="definicionparrilla",
            name="clave",
            field=models.CharField(
                default="elementos", max_length=60, validators=[apps.catalogo.claves.validar_clave]
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="definicionparrilla",
            name="orden",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(asignar_claves, migrations.RunPython.noop),
        migrations.AlterModelOptions(name="definicionparrilla", options={"ordering": ["orden", "pk"]}),
        migrations.AddConstraint(
            model_name="definicionparrilla",
            constraint=models.UniqueConstraint(
                fields=("servicio", "clave"), name="uq_definicion_parrilla_servicio_clave"
            ),
        ),
    ]
