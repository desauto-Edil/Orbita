from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.F2 — `Campo.es_fecha_requerida`: marca semántica del campo que representa el plazo que
    pide el solicitante. Aditiva: los campos existentes quedan en falso (ninguna fecha se
    reinterpreta como plazo). Solo FECHA/FECHA_HORA y como mucho uno por versión."""

    dependencies = [
        ("catalogo", "0018_finalizar_y_revision_entregable"),
    ]

    operations = [
        migrations.AddField(
            model_name="campo",
            name="es_fecha_requerida",
            field=models.BooleanField(
                default=False,
                help_text="Órbita comparará esta fecha con el tiempo objetivo del servicio y avisará al "
                "solicitante si pide un plazo menor al establecido.",
            ),
        ),
        migrations.AddConstraint(
            model_name="campo",
            constraint=models.UniqueConstraint(
                condition=Q(es_fecha_requerida=True), fields=("version",), name="uq_campo_version_fecha_requerida"
            ),
        ),
        migrations.AddConstraint(
            model_name="campo",
            constraint=models.CheckConstraint(
                condition=Q(es_fecha_requerida=False) | Q(tipo__in=["FECHA", "FECHA_HORA"]),
                name="ck_campo_fecha_requerida_tipo",
            ),
        ),
    ]
