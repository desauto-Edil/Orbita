import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


TIPOS_EVENTO = [
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
    ("CANCELACION_SOLICITADA", "Cancelación solicitada"),
    ("CANCELACION_RECHAZADA", "Cancelación rechazada"),
    ("CANCELACION_RETIRADA", "Solicitud de cancelación retirada"),
    ("DIRECCIONADO", "Direccionado"),
    ("ATENCION_INICIADA", "Atención iniciada"),
    ("DATOS_COMPLETADOS", "Datos de inicio completados"),
]


class Migration(migrations.Migration):
    """Cancelación con aprobación: política congelada en el Ticket y `SolicitudCancelacion`."""

    dependencies = [
        ("tickets", "0034_marca_evidencia"),
        ("catalogo", "0034_servicio_cancelacion"),
        ("aprobaciones", "0003_exclusion_y_justificacion"),
        ("core", "0013_jefatura_usuario"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="cancelacion_con_aprobacion",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="ticket",
            name="cancelacion_aprobador_usuario",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="cancelacion_aprobador_equipo",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="core.equipo",
            ),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(cancelacion_con_aprobacion=True, cancelacion_aprobador_usuario__isnull=False,
                             cancelacion_aprobador_equipo__isnull=True)
                    | models.Q(cancelacion_con_aprobacion=True, cancelacion_aprobador_usuario__isnull=True,
                               cancelacion_aprobador_equipo__isnull=False)
                    | models.Q(cancelacion_con_aprobacion=False, cancelacion_aprobador_usuario__isnull=True,
                               cancelacion_aprobador_equipo__isnull=True)
                ),
                name="ck_ticket_cancelacion_coherente",
            ),
        ),
        migrations.AlterField(
            model_name="historialticket",
            name="tipo_evento",
            field=models.CharField(choices=TIPOS_EVENTO, max_length=30),
        ),
        migrations.CreateModel(
            name="SolicitudCancelacion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("motivo", models.TextField()),
                ("estado", models.CharField(
                    choices=[("PENDIENTE", "Pendiente de aprobación"), ("APROBADA", "Aprobada"),
                             ("RECHAZADA", "Rechazada"), ("RETIRADA", "Retirada")],
                    default="PENDIENTE", max_length=10,
                )),
                ("resuelta_en", models.DateTimeField(blank=True, null=True)),
                ("observaciones_resolucion", models.TextField(blank=True)),
                ("esquema_aprobacion", models.OneToOneField(
                    editable=False, on_delete=django.db.models.deletion.PROTECT, related_name="cancelacion",
                    to="aprobaciones.esquemaaprobacion",
                )),
                ("resuelta_por", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("solicitada_por", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("ticket", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="solicitudes_cancelacion",
                    to="tickets.ticket",
                )),
            ],
            options={
                "ordering": ["ticket_id", "creado_en"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(estado="PENDIENTE"), fields=("ticket",),
                        name="uq_cancelacion_pendiente_por_ticket",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(estado="PENDIENTE", resuelta_en__isnull=True, resuelta_por__isnull=True)
                            | (~models.Q(estado="PENDIENTE") & models.Q(resuelta_en__isnull=False))
                        ),
                        name="ck_cancelacion_resolucion_coherente",
                    ),
                ],
            },
        ),
    ]
