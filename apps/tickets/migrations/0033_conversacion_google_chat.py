import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

TAREAS = (
    # (nombre, ruta, cada, periodo)
    ("tickets.importar_google_chat", "apps.tickets.tasks.importar_google_chat", 1, "minutes"),
    ("tickets.reconciliar_google_chat", "apps.tickets.tasks.reconciliar_google_chat", 15, "minutes"),
)


def crear_tareas_periodicas(apps, schema_editor):
    """Conversación en Google Chat: recoger lo escrito en Chat (cada minuto) y reconciliar espacios, miembros,
    suscripciones y la bandeja de salida (cada 15 minutos). Sin `GOOGLE_CHAT_ACTIVO` no hacen nada. Idempotente."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for nombre, ruta, cada, periodo in TAREAS:
        intervalo, _ = IntervalSchedule.objects.get_or_create(every=cada, period=periodo)
        PeriodicTask.objects.get_or_create(name=nombre, defaults={"task": ruta, "interval": intervalo})


def eliminar_tareas_periodicas(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[t[0] for t in TAREAS]).delete()


class Migration(migrations.Migration):
    """Conversación del ticket en Google Chat (plan `.claude/plans/conversacion_google_chat.md`)."""

    dependencies = [
        ("tickets", "0032_canales_conversacion"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="comentarioticket",
            name="origen",
            field=models.CharField(choices=[("ORBITA", "Órbita"), ("GOOGLE_CHAT", "Google Chat")], default="ORBITA", max_length=12),
        ),
        migrations.CreateModel(
            name="EspacioChatTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("canal", models.CharField(choices=[("EQUIPO", "Equipo"), ("INTERESADOS", "Interesados")], max_length=12)),
                ("nombre", models.CharField(blank=True, default="", max_length=120)),
                ("uri", models.URLField(blank=True, default="", max_length=300)),
                ("estado", models.CharField(choices=[("PENDIENTE", "Creándose"), ("ACTIVO", "Activo"), ("SOLO_LECTURA", "Solo lectura")], default="PENDIENTE", max_length=14)),
                ("suscripcion", models.CharField(blank=True, default="", max_length=200)),
                ("suscripcion_vence", models.DateTimeField(blank=True, null=True)),
                ("ultimo_importado_en", models.DateTimeField(blank=True, null=True)),
                ("sincronizado_en", models.DateTimeField(blank=True, null=True)),
                ("error", models.CharField(blank=True, default="", max_length=300)),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("ticket", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="espacios_chat", to="tickets.ticket")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("ticket", "canal"), name="uq_espacio_chat_ticket_canal")],
            },
        ),
        migrations.CreateModel(
            name="MiembroEspacioChat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("membresia", models.CharField(blank=True, default="", max_length=200)),
                ("usuario_chat", models.CharField(blank=True, default="", max_length=120)),
                ("activo", models.BooleanField(default=False)),
                ("error", models.CharField(blank=True, default="", max_length=300)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("espacio", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="miembros", to="tickets.espaciochatticket")),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["espacio", "usuario_chat"], name="ix_miembro_chat_usuario")],
                "constraints": [models.UniqueConstraint(fields=("espacio", "usuario"), name="uq_miembro_espacio_chat")],
            },
        ),
        migrations.CreateModel(
            name="MensajeChat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nombre", models.CharField(blank=True, default="", max_length=200)),
                ("direccion", models.CharField(choices=[("SALIDA", "De Órbita a Chat"), ("ENTRADA", "De Chat a Órbita")], max_length=8)),
                ("estado", models.CharField(choices=[("PENDIENTE", "Pendiente"), ("ENVIADO", "Enviado"), ("FALLIDO", "Falló"), ("IMPORTADO", "Importado"), ("RECHAZADO", "Rechazado")], default="PENDIENTE", max_length=10)),
                ("intentos", models.PositiveSmallIntegerField(default=0)),
                ("error", models.CharField(blank=True, default="", max_length=300)),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("enviado_en", models.DateTimeField(blank=True, null=True)),
                ("comentario", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="mensaje_chat", to="tickets.comentarioticket")),
                ("espacio", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="mensajes", to="tickets.espaciochatticket")),
            ],
            options={
                "indexes": [models.Index(fields=["direccion", "estado", "creado_en"], name="ix_mensaje_chat_estado")],
                "constraints": [models.UniqueConstraint(condition=models.Q(("nombre", ""), _negated=True), fields=("nombre",), name="uq_mensaje_chat_nombre")],
            },
        ),
        migrations.RunPython(crear_tareas_periodicas, eliminar_tareas_periodicas),
    ]
