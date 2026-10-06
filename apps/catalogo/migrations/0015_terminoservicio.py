import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """4.D — términos de búsqueda de un Servicio/Proceso (buscador "¿Qué necesitas?").
    Aditiva: no crea términos ni toca Servicios existentes; sin términos el buscador
    sigue encontrando por nombre, categoría, descripción e instrucciones."""

    dependencies = [
        ("catalogo", "0014_destino_ticket_general"),
    ]

    operations = [
        migrations.CreateModel(
            name="TerminoServicio",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("termino", models.CharField(max_length=100)),
                ("activo", models.BooleanField(default=True)),
                (
                    "servicio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="terminos_busqueda",
                        to="catalogo.servicio",
                    ),
                ),
            ],
            options={"ordering": ["termino", "pk"]},
        ),
    ]
