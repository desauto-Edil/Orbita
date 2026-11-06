from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0007_seed_permisos_configuracion")]

    operations = [
        migrations.AddField(
            model_name="usuario", name="google_sub",
            field=models.CharField(blank=True, editable=False, max_length=255, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="usuario", name="google_foto_url",
            field=models.URLField(blank=True, editable=False, max_length=2048),
        ),
    ]
