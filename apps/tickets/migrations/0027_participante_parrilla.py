import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """Bloque 3 — participantes congelados por colección de un Ticket. Tabla nueva: los tickets existentes no tienen
    participantes (su comportamiento no cambia)."""

    dependencies = [
        ("core", "0007_seed_permisos_configuracion"),
        ("tickets", "0026_finalizacion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ParticipanteParrilla",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "equipo",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.equipo",
                    ),
                ),
                (
                    "parrilla",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="participantes",
                        to="tickets.parrillaticket",
                    ),
                ),
                (
                    "usuario",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(usuario__isnull=False, equipo__isnull=True) | Q(usuario__isnull=True, equipo__isnull=False),
                        name="ck_participanteparrilla_usuario_o_equipo",
                    ),
                    models.UniqueConstraint(
                        condition=Q(usuario__isnull=False), fields=("parrilla", "usuario"),
                        name="uq_participanteparrilla_usuario",
                    ),
                    models.UniqueConstraint(
                        condition=Q(equipo__isnull=False), fields=("parrilla", "equipo"),
                        name="uq_participanteparrilla_equipo",
                    ),
                ],
            },
        ),
    ]
