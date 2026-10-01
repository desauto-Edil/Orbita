from django.conf import settings
from django.db import migrations, models
from django.db.models import Q, UniqueConstraint
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("catalogo", "0007_definicionentregable"),
        ("tickets", "0008_ticket_instancia_workflow"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Históricos ya cerrados a materialización, incluso con cero entregables.
        migrations.AddField(
            model_name="ticket", name="entregables_materializados",
            field=models.BooleanField(default=True, editable=False),
        ),
        migrations.CreateModel(
            name="EntregableTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("nombre", models.CharField(max_length=150)),
                ("descripcion", models.TextField(blank=True)),
                ("tipo", models.CharField(choices=[("TEXTO", "Texto"), ("ARCHIVO", "Archivo"), ("ENLACE", "Enlace"), ("CONFIRMACION", "Confirmación")], max_length=20)),
                ("obligatorio", models.BooleanField(default=False)),
                ("orden", models.PositiveIntegerField(default=0)),
                ("texto", models.TextField(blank=True)),
                ("enlace", models.URLField(blank=True, max_length=2048)),
                ("confirmado_en", models.DateTimeField(blank=True, null=True)),
                ("ticket", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="entregables", to="tickets.ticket")),
                ("definicion", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ejecuciones", to="catalogo.definicionentregable")),
                ("confirmado_por", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("registrado_por", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["orden", "pk"],
                "constraints": [
                    UniqueConstraint(fields=["ticket", "definicion"], name="uq_entregable_ticket_definicion"),
                    models.CheckConstraint(condition=(Q(confirmado_por__isnull=True, confirmado_en__isnull=True) | Q(confirmado_por__isnull=False, confirmado_en__isnull=False)), name="ck_entregable_confirmacion_coherente"),
                ],
            },
        ),
        migrations.AddField(
            model_name="adjunto", name="entregable",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="archivos", to="tickets.entregableticket"),
        ),
        migrations.AddField(
            model_name="adjunto", name="retirado_en",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="adjunto", name="tipo_relacion",
            field=models.CharField(choices=[("TICKET", "Ticket"), ("COMENTARIO", "Comentario"), ("SOLICITUD", "Solicitud de información"), ("RESPUESTA_SOLICITUD", "Respuesta a solicitud de información"), ("RESOLUCION", "Resolución"), ("ENTREGABLE", "Entregable")], max_length=20),
        ),
        migrations.RemoveConstraint(model_name="adjunto", name="ck_adjunto_relacion_coherente"),
        migrations.AddConstraint(model_name="adjunto", constraint=models.CheckConstraint(check=Q(entregable__isnull=True, tipo_relacion='TICKET', ticket__isnull=False, comentario__isnull=True, solicitud__isnull=True, respuesta_solicitud__isnull=True, resolucion__isnull=True) | Q(entregable__isnull=True, tipo_relacion='COMENTARIO', ticket__isnull=True, comentario__isnull=False, solicitud__isnull=True, respuesta_solicitud__isnull=True, resolucion__isnull=True) | Q(entregable__isnull=True, tipo_relacion='SOLICITUD', ticket__isnull=True, comentario__isnull=True, solicitud__isnull=False, respuesta_solicitud__isnull=True, resolucion__isnull=True) | Q(entregable__isnull=True, tipo_relacion='RESPUESTA_SOLICITUD', ticket__isnull=True, comentario__isnull=True, solicitud__isnull=True, respuesta_solicitud__isnull=False, resolucion__isnull=True) | Q(entregable__isnull=True, tipo_relacion='RESOLUCION', ticket__isnull=True, comentario__isnull=True, solicitud__isnull=True, respuesta_solicitud__isnull=True, resolucion__isnull=False) | Q(tipo_relacion='ENTREGABLE', entregable__isnull=False, ticket__isnull=True, comentario__isnull=True, solicitud__isnull=True, respuesta_solicitud__isnull=True, resolucion__isnull=True), name='ck_adjunto_relacion_coherente')),
    ]
