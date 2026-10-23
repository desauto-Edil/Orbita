import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """Bloque 3 — participantes de una colección de ítems (personas o equipos que crean y editan ítems sin ser
    responsables del Ticket). Tabla nueva: nada existente cambia."""

    dependencies = [
        ("catalogo", "0029_posicion_visual_bloque"),
        ("core", "0007_seed_permisos_configuracion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ParticipanteColeccion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "definicion",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="participantes",
                        to="catalogo.definicionparrilla",
                    ),
                ),
                (
                    "equipo",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.equipo",
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
                "ordering": ["pk"],
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(usuario__isnull=False, equipo__isnull=True) | Q(usuario__isnull=True, equipo__isnull=False),
                        name="ck_participantecoleccion_usuario_o_equipo",
                    ),
                    models.UniqueConstraint(
                        condition=Q(usuario__isnull=False), fields=("definicion", "usuario"),
                        name="uq_participantecoleccion_usuario",
                    ),
                    models.UniqueConstraint(
                        condition=Q(equipo__isnull=False), fields=("definicion", "equipo"),
                        name="uq_participantecoleccion_equipo",
                    ),
                ],
            },
        ),
    ]
