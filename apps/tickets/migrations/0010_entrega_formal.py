from django.conf import settings
from django.db import migrations, models
from django.db.models import Q, UniqueConstraint
import django.db.models.deletion

POLITICAS = [
    ("CIERRE_DIRECTO", "Cierre al entregar, sin esperar respuesta"),
    ("PERIODO_OBSERVACIONES", "Periodo de observaciones"),
]


class Migration(migrations.Migration):
    """4.5 — entrega formal al solicitante. Aditiva: los Tickets existentes
    quedan con política vacía (flujo resolver/cerrar anterior) y sin entregas;
    no se materializa ninguna entrega ficticia para históricos."""

    dependencies = [
        ("catalogo", "0008_servicio_politica_entrega"),
        ("tickets", "0009_entregables"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="entrega_politica",
            field=models.CharField(blank=True, choices=POLITICAS, default="", editable=False, max_length=30),
        ),
        migrations.AddField(
            model_name="ticket",
            name="entrega_dias_observacion",
            field=models.PositiveSmallIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="historialticket",
            name="actor",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="historialticket",
            name="tipo_evento",
            field=models.CharField(
                choices=[
                    ("RADICADO", "Radicado"),
                    ("TOMADO", "Tomado"),
                    ("ASIGNADO", "Asignado"),
                    ("REASIGNADO", "Reasignado"),
                    ("INFORMACION_SOLICITADA", "Información solicitada"),
                    ("INFORMACION_RESPONDIDA", "Información respondida"),
                    ("RESUELTO", "Resuelto"),
                    ("CERRADO", "Cerrado"),
                    ("CANCELADO", "Cancelado"),
                    ("REABIERTO", "Reabierto"),
                    ("ENTREGADO", "Resultado entregado"),
                    ("ENTREGA_OBSERVADA", "Entrega con observaciones"),
                ],
                max_length=30,
            ),
        ),
        migrations.CreateModel(
            name="EntregaTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("numero", models.PositiveIntegerField()),
                ("entregada_en", models.DateTimeField()),
                ("politica", models.CharField(choices=POLITICAS, max_length=30)),
                ("dias_observacion", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("vence_en", models.DateTimeField(blank=True, null=True)),
                (
                    "estado",
                    models.CharField(
                        choices=[
                            ("PENDIENTE", "Pendiente de respuesta"),
                            ("ACEPTADA", "Aceptada por el solicitante"),
                            ("OBSERVADA", "Con observaciones"),
                            ("CERRADA_POR_VENCIMIENTO", "Cerrada por vencimiento del plazo"),
                            ("CERRADA_SIN_RESPUESTA", "Cerrada al entregar, sin esperar respuesta"),
                        ],
                        default="PENDIENTE",
                        max_length=30,
                    ),
                ),
                ("resuelta_en", models.DateTimeField(blank=True, null=True)),
                ("observaciones", models.TextField(blank=True)),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="entregas", to="tickets.ticket"
                    ),
                ),
                (
                    "entregada_por",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "resuelta_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["ticket_id", "numero"],
                "constraints": [
                    UniqueConstraint(fields=["ticket", "numero"], name="uq_entrega_ticket_numero"),
                    UniqueConstraint(
                        fields=["ticket"], condition=Q(estado="PENDIENTE"), name="uq_entrega_pendiente_por_ticket"
                    ),
                    models.CheckConstraint(
                        condition=(
                            Q(estado="PENDIENTE", resuelta_en__isnull=True)
                            | (~Q(estado="PENDIENTE") & Q(resuelta_en__isnull=False))
                        ),
                        name="ck_entrega_resolucion_coherente",
                    ),
                    models.CheckConstraint(
                        condition=(
                            Q(politica="PERIODO_OBSERVACIONES", vence_en__isnull=False, dias_observacion__isnull=False)
                            | (~Q(politica="PERIODO_OBSERVACIONES") & Q(vence_en__isnull=True))
                        ),
                        name="ck_entrega_vencimiento_coherente",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ResultadoEntregaTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nombre", models.CharField(max_length=150)),
                (
                    "tipo",
                    models.CharField(
                        choices=[
                            ("TEXTO", "Texto"), ("ARCHIVO", "Archivo"),
                            ("ENLACE", "Enlace"), ("CONFIRMACION", "Confirmación"),
                        ],
                        max_length=20,
                    ),
                ),
                ("obligatorio", models.BooleanField(default=False)),
                ("orden", models.PositiveIntegerField(default=0)),
                ("texto", models.TextField(blank=True)),
                ("enlace", models.URLField(blank=True, max_length=2048)),
                ("confirmado_en", models.DateTimeField(blank=True, null=True)),
                (
                    "entrega",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="resultados",
                        to="tickets.entregaticket",
                    ),
                ),
                (
                    "entregable",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="tickets.entregableticket"
                    ),
                ),
                (
                    "confirmado_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "adjuntos",
                    models.ManyToManyField(blank=True, related_name="resultados_entrega", to="tickets.adjunto"),
                ),
            ],
            options={
                "ordering": ["orden", "pk"],
                "constraints": [
                    UniqueConstraint(fields=["entrega", "entregable"], name="uq_resultado_entrega_entregable"),
                ],
            },
        ),
    ]
