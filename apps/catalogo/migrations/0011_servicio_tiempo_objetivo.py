from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.A1 — tiempo objetivo de atención del Servicio/Proceso. Aditiva: los
    registros existentes quedan sin compromiso temporal (cantidad nula)."""

    dependencies = [("catalogo", "0010_transicion_bloque_operativo")]

    operations = [
        migrations.AddField(
            model_name="servicio",
            name="tiempo_objetivo_cantidad",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="servicio",
            name="tiempo_objetivo_unidad",
            field=models.CharField(
                blank=True, choices=[("HORAS", "Horas"), ("DIAS", "Días")], default="", max_length=10
            ),
        ),
        migrations.AddField(
            model_name="servicio",
            name="tiempo_objetivo_habiles",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="servicio",
            constraint=models.CheckConstraint(
                condition=(
                    Q(tiempo_objetivo_cantidad__isnull=True, tiempo_objetivo_unidad="", tiempo_objetivo_habiles=False)
                    | Q(tiempo_objetivo_cantidad__isnull=False, tiempo_objetivo_cantidad__gte=1, tiempo_objetivo_unidad__in=["HORAS", "DIAS"])
                ),
                name="ck_servicio_tiempo_objetivo_coherente",
                violation_error_message="El tiempo objetivo necesita cantidad (1 o más) y unidad, o ninguno de los dos.",
            ),
        ),
    ]
