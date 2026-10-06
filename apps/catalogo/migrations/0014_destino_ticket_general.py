import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.C2 — destinos del Ticket General y destino de reserva. Aditiva: no crea
    ningún destino ni toca Servicios/Tickets existentes; el Ticket General sigue
    deshabilitado hasta que un administrador lo configure."""

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0005_seed_permiso_auditoria_consultar"),
        ("catalogo", "0013_ticket_general"),
    ]

    operations = [
        migrations.CreateModel(
            name="DestinoTicketGeneral",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "tipo",
                    models.CharField(
                        choices=[("AREA", "Área"), ("EQUIPO", "Equipo"), ("USUARIO", "Persona")], max_length=10
                    ),
                ),
                ("activo", models.BooleanField(default=True)),
                (
                    "area",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.area",
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
                    "responsable_equipo",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.equipo",
                    ),
                ),
                (
                    "responsable_usuario",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
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
                "verbose_name": "destino del ticket general",
                "verbose_name_plural": "destinos del ticket general",
            },
        ),
        migrations.AddConstraint(
            model_name="destinoticketgeneral",
            constraint=models.CheckConstraint(
                condition=(
                    Q(tipo="AREA", area__isnull=False, equipo__isnull=True, usuario__isnull=True)
                    | Q(tipo="EQUIPO", equipo__isnull=False, area__isnull=True, usuario__isnull=True)
                    | Q(tipo="USUARIO", usuario__isnull=False, area__isnull=True, equipo__isnull=True)
                ),
                name="ck_destinotg_tipo_coherente",
            ),
        ),
        migrations.AddConstraint(
            model_name="destinoticketgeneral",
            constraint=models.CheckConstraint(
                condition=(
                    Q(responsable_usuario__isnull=False, responsable_equipo__isnull=True)
                    | Q(responsable_usuario__isnull=True, responsable_equipo__isnull=False)
                ),
                name="ck_destinotg_un_responsable",
            ),
        ),
        migrations.AddConstraint(
            model_name="destinoticketgeneral",
            constraint=models.UniqueConstraint(
                condition=Q(tipo="AREA"), fields=("area",), name="uq_destinotg_area"
            ),
        ),
        migrations.AddConstraint(
            model_name="destinoticketgeneral",
            constraint=models.UniqueConstraint(
                condition=Q(tipo="EQUIPO"), fields=("equipo",), name="uq_destinotg_equipo"
            ),
        ),
        migrations.AddConstraint(
            model_name="destinoticketgeneral",
            constraint=models.UniqueConstraint(
                condition=Q(tipo="USUARIO"), fields=("usuario",), name="uq_destinotg_usuario"
            ),
        ),
        migrations.AddField(
            model_name="configuracionticketgeneral",
            name="destino_predeterminado",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                to="catalogo.destinoticketgeneral",
            ),
        ),
    ]
