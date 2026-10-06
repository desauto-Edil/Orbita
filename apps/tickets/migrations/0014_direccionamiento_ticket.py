import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q

EVENTOS = [
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
    ("PRORROGA_SOLICITADA", "Prórroga solicitada"),
    ("PRORROGA_APROBADA", "Prórroga aprobada"),
    ("PRORROGA_RECHAZADA", "Prórroga rechazada"),
    ("PRORROGA_CANCELADA", "Prórroga cancelada"),
    ("DIRECCIONADO", "Direccionado"),
    ("ATENCION_INICIADA", "Atención iniciada"),
]


class Migration(migrations.Migration):
    """4.C2 — direccionamiento del Ticket General. Aditiva: los tickets existentes
    no tienen direccionamiento (solo los generales radicados desde ahora lo
    tendrán) y no se recalcula ni se inventa nada."""

    dependencies = [
        ("catalogo", "0014_destino_ticket_general"),
        ("tickets", "0013_prorroga_ticket"),
    ]

    operations = [
        migrations.AlterField(
            model_name="historialticket",
            name="tipo_evento",
            field=models.CharField(choices=EVENTOS, max_length=30),
        ),
        migrations.CreateModel(
            name="DireccionamientoTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("tipo", models.CharField(blank=True, default="", editable=False, max_length=10)),
                ("referencia_id", models.PositiveIntegerField(blank=True, editable=False, null=True)),
                ("etiqueta", models.CharField(blank=True, default="", editable=False, max_length=200)),
                ("es_predeterminado", models.BooleanField(default=False, editable=False)),
                ("fijado_en", models.DateTimeField(blank=True, editable=False, null=True)),
                (
                    "destino",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="direccionamientos", to="catalogo.destinoticketgeneral",
                    ),
                ),
                (
                    "ticket",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="direccionamiento",
                        to="tickets.ticket",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="direccionamientoticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(fijado_en__isnull=True, tipo="", etiqueta="", referencia_id__isnull=True, es_predeterminado=False)
                    | Q(
                        fijado_en__isnull=False, destino__isnull=False, referencia_id__isnull=False,
                        tipo__in=["AREA", "EQUIPO", "USUARIO"],
                    )
                    & ~Q(etiqueta="")
                ),
                name="ck_direccionamiento_fijado_coherente",
            ),
        ),
    ]
