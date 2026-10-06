import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q, UniqueConstraint

POLITICAS = [
    ("NO_PERMITE", "No permite prórrogas"),
    ("SIN_APROBACION", "Prórroga directa, sin aprobación"),
    ("CON_APROBACION", "Prórroga con aprobación"),
]
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
]


class Migration(migrations.Migration):
    """4.A2 — prórrogas. Aditiva: los Tickets existentes quedan sin política de
    prórroga (no permiten prórrogas) y sin solicitudes; no se inventa nada."""

    dependencies = [
        ("core", "0005_seed_permiso_auditoria_consultar"),
        ("aprobaciones", "0001_initial"),
        ("catalogo", "0012_servicio_politica_prorroga"),
        ("tickets", "0012_ticket_tiempo_objetivo"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="prorroga_politica",
            field=models.CharField(blank=True, choices=POLITICAS, default="", editable=False, max_length=30),
        ),
        migrations.AddField(
            model_name="ticket",
            name="prorroga_aprobador_usuario",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="prorroga_aprobador_equipo",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="core.equipo",
            ),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        prorroga_politica="CON_APROBACION",
                        prorroga_aprobador_usuario__isnull=False,
                        prorroga_aprobador_equipo__isnull=True,
                    )
                    | Q(
                        prorroga_politica="CON_APROBACION",
                        prorroga_aprobador_usuario__isnull=True,
                        prorroga_aprobador_equipo__isnull=False,
                    )
                    | (
                        ~Q(prorroga_politica="CON_APROBACION")
                        & Q(prorroga_aprobador_usuario__isnull=True, prorroga_aprobador_equipo__isnull=True)
                    )
                ),
                name="ck_ticket_prorroga_coherente",
            ),
        ),
        migrations.AlterField(
            model_name="historialticket",
            name="tipo_evento",
            field=models.CharField(choices=EVENTOS, max_length=30),
        ),
        migrations.CreateModel(
            name="ProrrogaTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("numero", models.PositiveIntegerField()),
                (
                    "politica",
                    models.CharField(
                        choices=[
                            ("SIN_APROBACION", "Prórroga directa, sin aprobación"),
                            ("CON_APROBACION", "Prórroga con aprobación"),
                        ],
                        max_length=30,
                    ),
                ),
                ("solicitada_en", models.DateTimeField()),
                ("fecha_objetivo_vigente_al_solicitar", models.DateTimeField()),
                ("nueva_fecha_solicitada", models.DateTimeField()),
                ("motivo", models.TextField()),
                (
                    "estado",
                    models.CharField(
                        choices=[
                            ("PENDIENTE", "Pendiente"),
                            ("APROBADA", "Aprobada"),
                            ("RECHAZADA", "Rechazada"),
                            ("CANCELADA", "Cancelada"),
                        ],
                        default="PENDIENTE",
                        max_length=10,
                    ),
                ),
                ("resuelta_en", models.DateTimeField(blank=True, null=True)),
                ("observaciones_resolucion", models.TextField(blank=True)),
                (
                    "esquema_aprobacion",
                    models.OneToOneField(
                        blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="prorroga", to="aprobaciones.esquemaaprobacion",
                    ),
                ),
                (
                    "resuelta_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "solicitada_por",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="prorrogas", to="tickets.ticket"
                    ),
                ),
            ],
            options={"ordering": ["ticket_id", "numero"]},
        ),
        migrations.AddConstraint(
            model_name="prorrogaticket",
            constraint=UniqueConstraint(fields=("ticket", "numero"), name="uq_prorroga_ticket_numero"),
        ),
        migrations.AddConstraint(
            model_name="prorrogaticket",
            constraint=UniqueConstraint(
                condition=Q(estado="PENDIENTE"), fields=("ticket",), name="uq_prorroga_pendiente_por_ticket"
            ),
        ),
        migrations.AddConstraint(
            model_name="prorrogaticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(estado="PENDIENTE", resuelta_en__isnull=True, resuelta_por__isnull=True)
                    | (~Q(estado="PENDIENTE") & Q(resuelta_en__isnull=False))
                ),
                name="ck_prorroga_resolucion_coherente",
            ),
        ),
        migrations.AddConstraint(
            model_name="prorrogaticket",
            constraint=models.CheckConstraint(
                condition=Q(nueva_fecha_solicitada__gt=F("fecha_objetivo_vigente_al_solicitar")),
                name="ck_prorroga_fecha_posterior",
            ),
        ),
        migrations.AddConstraint(
            model_name="prorrogaticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(politica="CON_APROBACION", esquema_aprobacion__isnull=False)
                    | Q(politica="SIN_APROBACION", esquema_aprobacion__isnull=True, estado="APROBADA")
                ),
                name="ck_prorroga_politica_coherente",
            ),
        ),
    ]
