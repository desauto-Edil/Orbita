from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 2 del modelo configurable — decisión EXCLUIDA (solo en esquemas que la permiten) y justificación
    obligatoria al aprobar. Los esquemas existentes no la permiten ni la exigen: nada cambia para ellos."""

    dependencies = [
        ("aprobaciones", "0002_seed_permisos_aprobaciones"),
    ]

    operations = [
        migrations.AddField(
            model_name="esquemaaprobacion",
            name="permite_exclusion",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="esquemaaprobacion",
            name="justificacion_al_aprobar",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="esquemaaprobacion",
            name="resultado",
            field=models.CharField(
                blank=True,
                choices=[
                    ("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"), ("DEVUELTA", "Devuelta"),
                    ("EXCLUIDA", "Excluida"),
                ],
                max_length=10,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="aprobacion",
            name="estado",
            field=models.CharField(
                choices=[
                    ("PENDIENTE", "Pendiente"), ("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"),
                    ("DEVUELTA", "Devuelta"), ("EXCLUIDA", "Excluida"), ("NO_REQUERIDA", "No requerida"),
                ],
                default="PENDIENTE",
                max_length=15,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="aprobacion",
            name="ck_aprobacion_decision_coherente",
        ),
        migrations.AddConstraint(
            model_name="aprobacion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("decidida_en__isnull", True), ("decidido_por__isnull", True),
                        ("estado__in", ["PENDIENTE", "NO_REQUERIDA"]),
                    ),
                    models.Q(
                        ("decidida_en__isnull", False), ("decidido_por__isnull", False),
                        ("estado__in", ["APROBADA", "RECHAZADA", "DEVUELTA", "EXCLUIDA"]),
                    ),
                    _connector="OR",
                ),
                name="ck_aprobacion_decision_coherente",
            ),
        ),
    ]
