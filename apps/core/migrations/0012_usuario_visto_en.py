from django.db import migrations, models


class Migration(migrations.Migration):
    """Presencia (Conversación del ticket): la última vez que la persona tenía Órbita abierta (`apps.core.presencia`)."""

    dependencies = [
        ("core", "0011_configuracion_sistema_logo_oscuro"),
    ]

    operations = [
        migrations.AddField(
            model_name="usuario",
            name="visto_en",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
