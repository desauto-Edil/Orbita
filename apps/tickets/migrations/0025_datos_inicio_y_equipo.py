import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 2 del modelo configurable — datos de inicio diligenciados dentro de la ejecución (bandera congelada
    en el Ticket y marca de completado en sus respuestas), evento de historial y columna EQUIPO. Los tickets
    existentes quedan con `datos_inicio_en_ejecucion=False`: nada cambia para ellos."""

    dependencies = [
        ("tickets", "0024_alcance_y_versiones"),
        ("core", "0007_seed_permisos_configuracion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="datos_inicio_en_ejecucion",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="respuestaformulario",
            name="completado_en",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="respuestaformulario",
            name="completado_por",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="respuestacampo",
            name="valor_equipo",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                to="core.equipo",
            ),
        ),
        migrations.AddField(
            model_name="valorelemento",
            name="valor_equipo",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                to="core.equipo",
            ),
        ),
        migrations.AlterField(
            model_name="historialticket",
            name="tipo_evento",
            field=models.CharField(
                choices=[
                    ("RADICADO", "Radicado"), ("TOMADO", "Tomado"), ("ASIGNADO", "Asignado"),
                    ("REASIGNADO", "Reasignado"), ("INFORMACION_SOLICITADA", "Información solicitada"),
                    ("INFORMACION_RESPONDIDA", "Información respondida"), ("RESUELTO", "Resuelto"),
                    ("CERRADO", "Cerrado"), ("CANCELADO", "Cancelado"), ("REABIERTO", "Reabierto"),
                    ("ENTREGADO", "Resultado entregado"), ("ENTREGA_OBSERVADA", "Entrega con observaciones"),
                    ("PRORROGA_SOLICITADA", "Prórroga solicitada"), ("PRORROGA_APROBADA", "Prórroga aprobada"),
                    ("PRORROGA_RECHAZADA", "Prórroga rechazada"), ("PRORROGA_CANCELADA", "Prórroga cancelada"),
                    ("DIRECCIONADO", "Direccionado"), ("ATENCION_INICIADA", "Atención iniciada"),
                    ("DATOS_COMPLETADOS", "Datos de inicio completados"),
                ],
                max_length=30,
            ),
        ),
    ]
