import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """4.G2D1 — copia congelada de la configuración de tareas por elemento (`ConfiguracionTareasTicket`) y el
    vínculo elemento ↔ Tarea (`TareaElemento`). Aditiva: ningún Ticket existente las tiene y no se crea
    ningún dato."""

    dependencies = [
        ("catalogo", "0023_configuracion_tareas_elemento"),
        ("tareas", "0003_tarea_cancelada"),
        ("tickets", "0019_revision_elementos"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfiguracionTareasTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("requiere_evidencia", models.BooleanField(default=False)),
                (
                    "campo_descripcion",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="catalogo.campo",
                    ),
                ),
                (
                    "campo_fecha_limite",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalogo.campo"
                    ),
                ),
                (
                    "campo_responsable",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalogo.campo"
                    ),
                ),
                (
                    "campo_titulo",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalogo.campo"
                    ),
                ),
                (
                    "parrilla",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="configuracion_tareas",
                        to="tickets.parrillaticket",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="TareaElemento",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("requiere_evidencia", models.BooleanField(default=False)),
                (
                    "configuracion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="tareas_generadas",
                        to="tickets.configuraciontareasticket",
                    ),
                ),
                (
                    "elemento",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="tareas",
                        to="tickets.elementoparrilla",
                    ),
                ),
                (
                    "tarea",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, related_name="vinculo_elemento",
                        to="tareas.tarea",
                    ),
                ),
            ],
            options={"ordering": ["elemento__orden", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="tareaelemento",
            constraint=models.UniqueConstraint(
                fields=("elemento", "configuracion"), name="uq_tareaelemento_elemento_configuracion"
            ),
        ),
    ]
