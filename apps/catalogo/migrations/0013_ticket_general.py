from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.C1 — Ticket General: marca del Servicio interno (única) y configuración
    global. Aditiva: ningún Servicio existente queda marcado ni se convierte; el
    Ticket General nace deshabilitado."""

    dependencies = [
        ("core", "0005_seed_permiso_auditoria_consultar"),
        ("catalogo", "0012_servicio_politica_prorroga"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicio",
            name="es_ticket_general",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="servicio",
            constraint=models.UniqueConstraint(
                condition=Q(es_ticket_general=True),
                fields=("es_ticket_general",),
                name="uq_servicio_ticket_general_unico",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicio",
            constraint=models.CheckConstraint(
                condition=Q(es_ticket_general=False) | Q(tipo="SERVICIO"),
                name="ck_servicio_ticket_general_es_servicio",
                violation_error_message="El ticket general debe ser un Servicio, no un Proceso.",
            ),
        ),
        migrations.CreateModel(
            name="ConfiguracionTicketGeneral",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("habilitado", models.BooleanField(default=False)),
            ],
            options={
                "verbose_name": "configuración del ticket general",
                "verbose_name_plural": "configuración del ticket general",
            },
        ),
    ]
