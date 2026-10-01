"""Amplía las plantillas existentes; no modifica asignaciones ni versiones históricas."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workflows", "0007_transicionetapa_resultado_aprobacion_and_more")]

    operations = [
        migrations.RemoveConstraint(
            model_name="configuracionetapatarea", name="ck_configuracionetapatarea_tipo_coherente",
        ),
        migrations.RemoveConstraint(
            model_name="participanteetapaaprobacion", name="ck_participanteetapaaprobacion_tipo_coherente",
        ),
        migrations.AlterField(
            model_name="configuracionetapatarea", name="tipo_responsable",
            field=models.CharField(blank=True, max_length=20, choices=[
                ("USUARIO", "Usuario"), ("EQUIPO", "Equipo"),
                ("RESPONSABLE_TICKET", "Responsable individual del ticket"),
                ("SOLICITANTE", "Solicitante del ticket"),
            ]),
        ),
        migrations.AlterField(
            model_name="participanteetapaaprobacion", name="tipo_aprobador",
            field=models.CharField(max_length=20, choices=[
                ("USUARIO", "Usuario"), ("EQUIPO", "Equipo"),
                ("RESPONSABLE_TICKET", "Responsable individual del ticket"),
                ("SOLICITANTE", "Solicitante del ticket"),
            ]),
        ),
        migrations.AddConstraint(
            model_name="configuracionetapatarea",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(tipo_responsable__in=["", "RESPONSABLE_TICKET", "SOLICITANTE"], usuario_responsable__isnull=True, equipo_responsable__isnull=True)
                    | models.Q(tipo_responsable="USUARIO", usuario_responsable__isnull=False, equipo_responsable__isnull=True)
                    | models.Q(tipo_responsable="EQUIPO", equipo_responsable__isnull=False, usuario_responsable__isnull=True)
                ), name="ck_configuracionetapatarea_tipo_coherente",
            ),
        ),
        migrations.AddConstraint(
            model_name="participanteetapaaprobacion",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(tipo_aprobador="USUARIO", usuario__isnull=False, equipo__isnull=True)
                    | models.Q(tipo_aprobador="EQUIPO", equipo__isnull=False, usuario__isnull=True)
                    | models.Q(tipo_aprobador__in=["RESPONSABLE_TICKET", "SOLICITANTE"], usuario__isnull=True, equipo__isnull=True)
                ), name="ck_participanteetapaaprobacion_tipo_coherente",
            ),
        ),
    ]
