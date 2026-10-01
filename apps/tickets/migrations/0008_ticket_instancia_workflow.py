from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0007_alter_resolucionticket_options_and_more"),
        ("workflows", "0003_instanciaworkflow_instanciaetapa"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket", name="instancia_workflow",
            field=models.OneToOneField(blank=True, editable=False, null=True,
                                       on_delete=django.db.models.deletion.PROTECT,
                                       related_name="ticket", to="workflows.instanciaworkflow"),
        ),
    ]
