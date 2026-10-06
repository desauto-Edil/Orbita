import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.A2 — política de prórroga del Servicio/Proceso. Aditiva: los registros
    existentes quedan sin política (equivale a no permitir prórrogas)."""

    dependencies = [
        ("core", "0005_seed_permiso_auditoria_consultar"),
        ("catalogo", "0011_servicio_tiempo_objetivo"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="servicio",
            name="politica_prorroga",
            field=models.CharField(
                blank=True,
                choices=[
                    ("NO_PERMITE", "No permite prórrogas"),
                    ("SIN_APROBACION", "Prórroga directa, sin aprobación"),
                    ("CON_APROBACION", "Prórroga con aprobación"),
                ],
                default="",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="servicio",
            name="prorroga_aprobador_usuario",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="servicio",
            name="prorroga_aprobador_equipo",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="core.equipo",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicio",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        politica_prorroga="CON_APROBACION",
                        prorroga_aprobador_usuario__isnull=False,
                        prorroga_aprobador_equipo__isnull=True,
                    )
                    | Q(
                        politica_prorroga="CON_APROBACION",
                        prorroga_aprobador_usuario__isnull=True,
                        prorroga_aprobador_equipo__isnull=False,
                    )
                    | (
                        ~Q(politica_prorroga="CON_APROBACION")
                        & Q(prorroga_aprobador_usuario__isnull=True, prorroga_aprobador_equipo__isnull=True)
                    )
                ),
                name="ck_servicio_prorroga_coherente",
                violation_error_message="La prórroga con aprobación necesita exactamente un aprobador (usuario o equipo).",
            ),
        ),
    ]
