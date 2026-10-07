import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.G1 — `ProgramacionProceso`: cuándo se genera automáticamente una ejecución de un
    Proceso. Aditiva: ningún Proceso existente queda programado (sin fila = inicio manual)."""

    dependencies = [
        ("catalogo", "0019_campo_fecha_requerida"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProgramacionProceso",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("activa", models.BooleanField(default=True)),
                (
                    "frecuencia",
                    models.CharField(choices=[("MENSUAL", "Mensual")], default="MENSUAL", max_length=20),
                ),
                ("dia_creacion", models.PositiveSmallIntegerField()),
                (
                    "periodo",
                    models.CharField(
                        choices=[("MES_ACTUAL", "Mes actual"), ("MES_SIGUIENTE", "Mes siguiente")],
                        default="MES_SIGUIENTE",
                        max_length=20,
                    ),
                ),
                ("activada_desde", models.DateField(blank=True, null=True)),
                ("ultimo_intento_en", models.DateTimeField(blank=True, editable=False, null=True)),
                ("ultimo_error", models.TextField(blank=True, default="", editable=False)),
                (
                    "responsable_inicial",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="catalogo.servicioresponsable",
                    ),
                ),
                (
                    "servicio",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="programacion",
                        to="catalogo.servicio",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="programacionproceso",
            constraint=models.CheckConstraint(
                condition=Q(dia_creacion__gte=1, dia_creacion__lte=28),
                name="ck_programacion_dia_1_a_28",
                violation_error_message="El día de creación debe estar entre 1 y 28.",
            ),
        ),
        migrations.AddConstraint(
            model_name="programacionproceso",
            constraint=models.CheckConstraint(
                condition=Q(frecuencia="MENSUAL"),
                name="ck_programacion_frecuencia_v1",
                violation_error_message="Por ahora solo existe la frecuencia mensual.",
            ),
        ),
        migrations.AddConstraint(
            model_name="programacionproceso",
            constraint=models.CheckConstraint(
                condition=Q(periodo__in=["MES_ACTUAL", "MES_SIGUIENTE"]),
                name="ck_programacion_periodo_valido",
            ),
        ),
        migrations.AddConstraint(
            model_name="programacionproceso",
            constraint=models.CheckConstraint(
                condition=Q(activa=False) | Q(activada_desde__isnull=False, responsable_inicial__isnull=False),
                name="ck_programacion_activa_completa",
                violation_error_message="Una programación activa necesita responsable inicial y fecha de activación.",
            ),
        ),
    ]
