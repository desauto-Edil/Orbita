import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Lista de evidencias (CHECKLIST): una marca por punto con su prueba (archivo o enlace), retiro lógico."""

    dependencies = [
        ("tickets", "0033_conversacion_google_chat"),
        ("catalogo", "0033_campo_checklist"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MarcaEvidencia",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("punto", models.CharField(max_length=100)),
                ("enlace", models.URLField(blank=True, default="", max_length=2048)),
                ("archivo", models.FileField(blank=True, default="", upload_to="tickets/evidencias/%Y/%m/")),
                ("nombre_original", models.CharField(blank=True, default="", max_length=255)),
                ("tipo_mime", models.CharField(blank=True, default="", max_length=255)),
                ("tamano_bytes", models.PositiveIntegerField(default=0)),
                ("retirada_en", models.DateTimeField(blank=True, null=True)),
                ("campo", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="marcas_evidencia", to="catalogo.campo")),
                ("elemento", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="marcas_evidencia", to="tickets.elementoparrilla")),
                ("marcada_por", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("retirada_por", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("ticket", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="marcas_evidencia", to="tickets.ticket")),
            ],
            options={
                "ordering": ["creado_en", "pk"],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            (models.Q(("enlace", "")) & ~models.Q(("archivo", "")))
                            | (~models.Q(("enlace", "")) & models.Q(("archivo", "")))
                        ),
                        name="ck_marca_evidencia_una_prueba",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("elemento__isnull", False), ("retirada_en__isnull", True)),
                        fields=("elemento", "campo", "punto"), name="uq_marca_evidencia_item_vigente",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("elemento__isnull", True), ("retirada_en__isnull", True)),
                        fields=("ticket", "campo", "punto"), name="uq_marca_evidencia_ticket_vigente",
                    ),
                ],
            },
        ),
    ]
