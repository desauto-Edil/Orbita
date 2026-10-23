import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 3 del modelo configurable — gacetas en ejecución: la gaceta de cada ticket, la participación de cada
    ítem, sus rondas de revisión (con la versión presentada, el esquema de aprobación y la tarea de corrección) y
    las observaciones."""

    initial = True

    dependencies = [
        ("aprobaciones", "0003_exclusion_y_justificacion"),
        ("catalogo", "0028_gacetas_y_finalizacion"),
        ("tareas", "0006_historial_actor_sistema"),
        ("tickets", "0026_finalizacion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="GacetaTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("clave", models.CharField(max_length=60)),
                ("nombre", models.CharField(max_length=150)),
                ("descripcion", models.TextField(blank=True)),
                ("version_definicion", models.PositiveIntegerField(default=1)),
                ("configuracion", models.JSONField(blank=True, default=dict)),
                (
                    "estado",
                    models.CharField(
                        choices=[
                            ("PENDIENTE", "Sin abrir"), ("ABIERTA", "Abierta"),
                            ("PERIODO_CERRADO", "Periodo de revisión cerrado"), ("RESUELTA", "Resuelta"),
                            ("CANCELADA", "Cancelada"),
                        ],
                        default="PENDIENTE",
                        max_length=16,
                    ),
                ),
                ("abierta_en", models.DateTimeField(blank=True, null=True)),
                ("vence_revision_en", models.DateTimeField(blank=True, null=True)),
                ("periodo_cerrado_en", models.DateTimeField(blank=True, null=True)),
                ("resuelta_en", models.DateTimeField(blank=True, null=True)),
                ("resolucion", models.JSONField(blank=True, null=True)),
                (
                    "definicion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="gacetas_ticket",
                        to="catalogo.definiciongaceta",
                    ),
                ),
                (
                    "resuelta_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="gacetas", to="tickets.ticket"
                    ),
                ),
            ],
            options={
                "ordering": ["ticket_id", "pk"],
                "constraints": [
                    models.UniqueConstraint(fields=("ticket", "clave"), name="uq_gacetaticket_ticket_clave"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ParticipacionItemGaceta",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "estado",
                    models.CharField(
                        choices=[
                            ("EN_REVISION", "En revisión"), ("EN_CORRECCION", "En corrección"), ("APROBADA", "Aprobada"),
                            ("RECHAZADA", "Rechazada"), ("EXCLUIDA", "Excluida"),
                        ],
                        default="EN_REVISION",
                        max_length=14,
                    ),
                ),
                ("ronda", models.PositiveIntegerField(default=1)),
                (
                    "origen",
                    models.CharField(
                        choices=[
                            ("MANUAL", "Presentado por una persona"), ("AUTOMATICO", "Ingreso automático"),
                            ("FLUJO", "Presentado por el flujo"),
                        ],
                        default="MANUAL",
                        max_length=10,
                    ),
                ),
                ("presentada_en", models.DateTimeField(auto_now_add=True)),
                ("vence_correccion_en", models.DateTimeField(blank=True, null=True)),
                ("correccion_vencida_en", models.DateTimeField(blank=True, null=True)),
                ("decidida_en", models.DateTimeField(blank=True, null=True)),
                ("aceptada_automaticamente", models.BooleanField(default=False)),
                ("justificacion", models.TextField(blank=True)),
                (
                    "decidida_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "elemento",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="participaciones_gaceta",
                        to="tickets.elementoparrilla",
                    ),
                ),
                (
                    "gaceta",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="participaciones",
                        to="gacetas.gacetaticket",
                    ),
                ),
                (
                    "presentada_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["gaceta_id", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("gaceta", "elemento"), name="uq_participaciongaceta_gaceta_elemento"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RondaRevisionGaceta",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("numero", models.PositiveIntegerField()),
                (
                    "resultado",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("APROBADA", "Aprobada"), ("DEVUELTA", "Devuelta"), ("RECHAZADA", "Rechazada"),
                            ("EXCLUIDA", "Excluida"), ("OBSERVADA", "Observada"),
                            ("ACEPTADA_AUTO", "Aceptada automáticamente"), ("RESUELTA", "Definida al resolver la gaceta"),
                            ("CANCELADA", "Cancelada"),
                        ],
                        max_length=14,
                    ),
                ),
                ("abierta_en", models.DateTimeField(auto_now_add=True)),
                ("cerrada_en", models.DateTimeField(blank=True, null=True)),
                (
                    "esquema",
                    models.OneToOneField(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="ronda_gaceta",
                        to="aprobaciones.esquemaaprobacion",
                    ),
                ),
                (
                    "participacion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="rondas",
                        to="gacetas.participacionitemgaceta",
                    ),
                ),
                (
                    "tarea_correccion",
                    models.OneToOneField(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="ronda_gaceta",
                        to="tareas.tarea",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="rondas_gaceta", to="tickets.versionelemento",
                    ),
                ),
            ],
            options={
                "ordering": ["participacion_id", "numero"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("participacion", "numero"), name="uq_rondagaceta_participacion_numero"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ObservacionGaceta",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "origen",
                    models.CharField(
                        choices=[("REVISION", "Observación de revisión"), ("DECISION", "Al devolver")],
                        default="REVISION",
                        max_length=10,
                    ),
                ),
                ("texto", models.TextField()),
                ("creada_en", models.DateTimeField(auto_now_add=True)),
                ("respuesta", models.TextField(blank=True)),
                ("respondida_en", models.DateTimeField(blank=True, null=True)),
                ("atendida_en", models.DateTimeField(blank=True, null=True)),
                (
                    "autor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "participacion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="observaciones",
                        to="gacetas.participacionitemgaceta",
                    ),
                ),
                (
                    "respondida_por",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ronda",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="observaciones",
                        to="gacetas.rondarevisiongaceta",
                    ),
                ),
            ],
            options={
                "ordering": ["participacion_id", "creada_en", "pk"],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("texto", ""), _negated=True), name="ck_observaciongaceta_con_texto"),
                ],
            },
        ),
    ]
