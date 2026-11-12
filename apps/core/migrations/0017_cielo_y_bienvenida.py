"""Versión 1 — etapas 6 y 7.

- Cielo pasa a ser el estilo predeterminado (Lima sigue disponible) y la instalación que seguía en Lima pasa a
  Cielo (decisión del usuario, 2026-11-12). Una instalación que ya había elegido otro color lo conserva.
- Cada persona puede tener su propio color (`Usuario.tema`, vacío = el del sistema).
- `Usuario.bienvenida_completada_en`: vacío para TODOS, así quienes ya existían también ven la bienvenida."""

from django.db import migrations, models

TEMAS = [
    ("lima", "Lima"), ("menta", "Menta"), ("cielo", "Cielo"), ("ambar", "Ámbar"), ("coral", "Coral"),
    ("lavanda", "Lavanda"),
]


def lima_a_cielo(apps, schema_editor):
    apps.get_model("core", "ConfiguracionSistema").objects.filter(tema="lima").update(tema="cielo")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_usuario_sin_username_ni_contrasena"),
    ]

    operations = [
        migrations.AlterField(
            model_name="configuracionsistema",
            name="tema",
            field=models.CharField(choices=TEMAS, default="cielo", max_length=20),
        ),
        migrations.AddField(
            model_name="usuario",
            name="tema",
            field=models.CharField(blank=True, choices=TEMAS, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="usuario",
            name="bienvenida_completada_en",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        # Al final: los datos se tocan después de los cambios de esquema.
        migrations.RunPython(lima_a_cielo, migrations.RunPython.noop),
    ]
