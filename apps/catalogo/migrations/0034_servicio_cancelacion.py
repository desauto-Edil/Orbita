import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Cancelación con aprobación: política y aprobador (usuario o equipo) del Servicio/Proceso."""

    dependencies = [
        ("catalogo", "0033_campo_checklist"),
        ("core", "0013_jefatura_usuario"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="servicio",
            name="cancelacion_con_aprobacion",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="servicio",
            name="cancelacion_aprobador_usuario",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="servicio",
            name="cancelacion_aprobador_equipo",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.equipo",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicio",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(cancelacion_con_aprobacion=True, cancelacion_aprobador_usuario__isnull=False,
                             cancelacion_aprobador_equipo__isnull=True)
                    | models.Q(cancelacion_con_aprobacion=True, cancelacion_aprobador_usuario__isnull=True,
                               cancelacion_aprobador_equipo__isnull=False)
                    | models.Q(cancelacion_con_aprobacion=False, cancelacion_aprobador_usuario__isnull=True,
                               cancelacion_aprobador_equipo__isnull=True)
                ),
                name="ck_servicio_cancelacion_coherente",
                violation_error_message="La cancelación con aprobación necesita exactamente un aprobador (usuario o equipo).",
            ),
        ),
    ]
