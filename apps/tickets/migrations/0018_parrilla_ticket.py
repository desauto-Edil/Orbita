import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.G2B1 — estructura de elementos de un Ticket de Proceso: `ParrillaTicket` (congela la versión
    de columnas), `ElementoParrilla` (filas, retiro lógico) y `ValorElemento` (celdas tipadas).
    Aditiva: ningún Ticket existente tiene parrilla y no se crea ningún dato."""

    dependencies = [
        ("catalogo", "0021_definicion_parrilla"),
        ("core", "0005_seed_permiso_auditoria_consultar"),
        ("tickets", "0017_seed_periodic_task_generar_ejecuciones"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ParrillaTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("nombre_elemento", models.CharField(max_length=60)),
                (
                    "formulario_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="parrillas_ticket",
                        to="catalogo.formularioversion",
                    ),
                ),
                (
                    "ticket",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="parrilla", to="tickets.ticket"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ElementoParrilla",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("numero", models.PositiveIntegerField()),
                ("orden", models.PositiveIntegerField()),
                ("retirado_en", models.DateTimeField(blank=True, editable=False, null=True)),
                (
                    "parrilla",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="elementos",
                        to="tickets.parrillaticket",
                    ),
                ),
                (
                    "retirado_por",
                    models.ForeignKey(
                        blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["orden", "pk"]},
        ),
        migrations.CreateModel(
            name="ValorElemento",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("valor_texto", models.TextField(blank=True, default="")),
                ("valor_numero", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True)),
                ("valor_fecha", models.DateField(blank=True, null=True)),
                ("valor_fecha_hora", models.DateTimeField(blank=True, null=True)),
                ("valor_booleano", models.BooleanField(blank=True, null=True)),
                ("valor_json", models.JSONField(blank=True, null=True)),
                (
                    "campo",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="valores_elemento",
                        to="catalogo.campo",
                    ),
                ),
                (
                    "elemento",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="valores",
                        to="tickets.elementoparrilla",
                    ),
                ),
                (
                    "valor_area",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.area",
                    ),
                ),
                (
                    "valor_unidad",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to="core.unidadnegocio",
                    ),
                ),
                (
                    "valor_usuario",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="elementoparrilla",
            constraint=models.UniqueConstraint(fields=("parrilla", "numero"), name="uq_elementoparrilla_numero"),
        ),
        migrations.AddConstraint(
            model_name="elementoparrilla",
            constraint=models.UniqueConstraint(
                condition=Q(retirado_en__isnull=True), fields=("parrilla", "orden"),
                name="uq_elementoparrilla_orden_vigente",
            ),
        ),
        migrations.AddConstraint(
            model_name="elementoparrilla",
            constraint=models.CheckConstraint(
                condition=Q(retirado_en__isnull=False) | Q(retirado_por__isnull=True),
                name="ck_elementoparrilla_retiro_coherente",
            ),
        ),
        migrations.AddConstraint(
            model_name="valorelemento",
            constraint=models.UniqueConstraint(fields=("elemento", "campo"), name="uq_valorelemento_campo"),
        ),
    ]
