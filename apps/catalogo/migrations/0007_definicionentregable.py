from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("catalogo", "0006_servicio_tipo_workflow")]

    operations = [
        migrations.CreateModel(
            name="DefinicionEntregable",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("nombre", models.CharField(max_length=150)),
                ("descripcion", models.TextField(blank=True)),
                ("tipo", models.CharField(choices=[("TEXTO", "Texto"), ("ARCHIVO", "Archivo"), ("ENLACE", "Enlace"), ("CONFIRMACION", "Confirmación")], max_length=20)),
                ("obligatorio", models.BooleanField(default=False)),
                ("orden", models.PositiveIntegerField(default=0)),
                ("activo", models.BooleanField(default=True)),
                ("servicio", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="definiciones_entregables", to="catalogo.servicio")),
            ],
            options={"ordering": ["orden", "pk"]},
        ),
    ]
