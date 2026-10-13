import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2D1 — configuración OPCIONAL de las tareas por elemento de un Proceso
    (`ConfiguracionTareasElemento`). Aditiva: ningún Proceso existente la tiene y no se crea ningún dato."""

    dependencies = [
        ("catalogo", "0022_entregable_revision_elementos"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfiguracionTareasElemento",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("activa", models.BooleanField(default=True)),
                ("clave_titulo", models.SlugField(max_length=60)),
                ("clave_descripcion", models.SlugField(blank=True, max_length=60)),
                ("clave_responsable", models.SlugField(max_length=60)),
                ("clave_fecha_limite", models.SlugField(max_length=60)),
                ("requiere_evidencia", models.BooleanField(default=False)),
                (
                    "definicion",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="configuracion_tareas",
                        to="catalogo.definicionparrilla",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
    ]
