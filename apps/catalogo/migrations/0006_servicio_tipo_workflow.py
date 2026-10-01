from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("catalogo", "0005_serviciocontextoatencion"),
        ("workflows", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicio", name="tipo",
            field=models.CharField(choices=[("SERVICIO", "Servicio"), ("PROCESO", "Proceso")], default="SERVICIO", max_length=20),
        ),
        migrations.AddField(
            model_name="servicio", name="workflow",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="servicios", to="workflows.workflow"),
        ),
    ]
