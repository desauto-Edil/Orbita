import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q


class Migration(migrations.Migration):
    """4.G1 — Ticket generado por programación: origen PROGRAMACION, `solicitante` opcional SOLO
    en ese origen (constraint en ambos sentidos), `etiqueta` de ejecución y `EjecucionProgramada`
    (identidad única por Proceso y periodo). Aditiva: los tickets existentes son MANUAL con
    solicitante y quedan intactos; no se genera ninguna ejecución retroactiva."""

    dependencies = [
        ("catalogo", "0020_programacion_proceso"),
        ("tickets", "0015_codigo_publico_ticket"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="origen",
            field=models.CharField(
                choices=[("MANUAL", "Manual"), ("SISTEMA", "Sistema"), ("PROGRAMACION", "Programación")],
                default="MANUAL",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="ticket",
            name="solicitante",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="tickets_solicitados", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="etiqueta",
            field=models.CharField(blank=True, default="", editable=False, max_length=60),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(origen="PROGRAMACION", solicitante__isnull=True)
                    | (~Q(origen="PROGRAMACION") & Q(solicitante__isnull=False))
                ),
                name="ck_ticket_solicitante_segun_origen",
                violation_error_message="Solo un ticket generado por programación puede no tener solicitante.",
            ),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=~Q(origen="PROGRAMACION") | Q(tipo="PROCESO"),
                name="ck_ticket_programacion_es_proceso",
                violation_error_message="Solo un Proceso puede generarse por programación.",
            ),
        ),
        migrations.CreateModel(
            name="EjecucionProgramada",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("frecuencia", models.CharField(max_length=20)),
                ("periodo_inicio", models.DateField()),
                ("periodo_fin", models.DateField()),
                ("etiqueta", models.CharField(max_length=60)),
                ("generada_en", models.DateTimeField()),
                ("programacion_foto", models.JSONField(blank=True, default=dict)),
                (
                    "servicio",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="ejecuciones_programadas",
                        to="catalogo.servicio",
                    ),
                ),
                (
                    "ticket",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT, related_name="ejecucion_programada",
                        to="tickets.ticket",
                    ),
                ),
            ],
            options={"ordering": ["-periodo_inicio", "-pk"]},
        ),
        migrations.AddConstraint(
            model_name="ejecucionprogramada",
            constraint=models.UniqueConstraint(
                fields=("servicio", "frecuencia", "periodo_inicio"), name="uq_ejecucionprogramada_periodo"
            ),
        ),
        migrations.AddConstraint(
            model_name="ejecucionprogramada",
            constraint=models.CheckConstraint(
                condition=Q(periodo_fin__gte=F("periodo_inicio")), name="ck_ejecucionprogramada_periodo_ordenado"
            ),
        ),
    ]
