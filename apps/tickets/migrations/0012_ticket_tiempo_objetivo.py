from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.A1 — compromiso temporal del Ticket. Aditiva: los Tickets existentes
    quedan con todo en NULL (sin recalcular ni inventar fechas)."""

    dependencies = [
        ("catalogo", "0011_servicio_tiempo_objetivo"),
        ("tickets", "0011_seed_periodic_task_cerrar_entregas"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="tiempo_objetivo_cantidad",
            field=models.PositiveSmallIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="tiempo_objetivo_unidad",
            field=models.CharField(
                blank=True, choices=[("HORAS", "Horas"), ("DIAS", "Días")], default="", editable=False, max_length=10
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="tiempo_objetivo_habiles",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="ticket",
            name="fecha_objetivo_original",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="fecha_objetivo_vigente",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(tiempo_objetivo_cantidad__isnull=True, tiempo_objetivo_unidad="", tiempo_objetivo_habiles=False)
                    | Q(tiempo_objetivo_cantidad__isnull=False, tiempo_objetivo_cantidad__gte=1, tiempo_objetivo_unidad__in=["HORAS", "DIAS"])
                ),
                name="ck_ticket_tiempo_objetivo_coherente",
            ),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(fecha_objetivo_original__isnull=True, fecha_objetivo_vigente__isnull=True)
                    | Q(
                        fecha_objetivo_original__isnull=False,
                        fecha_objetivo_vigente__isnull=False,
                        tiempo_objetivo_cantidad__isnull=False,
                    )
                ),
                name="ck_ticket_fechas_objetivo_coherentes",
            ),
        ),
    ]
