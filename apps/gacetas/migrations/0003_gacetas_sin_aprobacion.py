"""Regla de oro: en una gaceta NADIE APRUEBA (es un espacio de comentarios).

- Nuevo resultado de ronda ACEPTADA_CORRECCION (la versión corregida que llega con el periodo ya cerrado) y el
  estado APROBADA de un ítem se muestra como «Aceptada» (el valor guardado no cambia).
- Las rondas de antes de la regla conservan su esquema como historia; sus aprobaciones que seguían PENDIENTE pasan a
  NO_REQUERIDA (lo mismo que hace `aprobaciones.operaciones.anular_esquema_aprobacion`), así ya nadie las ve como
  pendientes en Aprobaciones ni en Mi trabajo. Las decisiones ya tomadas no se tocan. El ítem sigue EN_REVISION y
  queda aceptado al cerrar el periodo si no tiene comentarios pendientes.
"""

from django.db import migrations, models
from django.utils import timezone


def retirar_aprobaciones_pendientes(apps, schema_editor):
    Aprobacion = apps.get_model("aprobaciones", "Aprobacion")
    Aprobacion.objects.filter(
        estado="PENDIENTE", esquema__resultado__isnull=True, esquema__ronda_gaceta__isnull=False,
    ).update(estado="NO_REQUERIDA", actualizado_en=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("gacetas", "0002_seed_periodic_task_vencimientos"),
        ("aprobaciones", "0003_exclusion_y_justificacion"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rondarevisiongaceta",
            name="resultado",
            field=models.CharField(
                blank=True,
                choices=[
                    ("APROBADA", "Aprobada"),
                    ("DEVUELTA", "Devuelta"),
                    ("RECHAZADA", "Rechazada"),
                    ("EXCLUIDA", "Excluida"),
                    ("OBSERVADA", "Observada"),
                    ("ACEPTADA_AUTO", "Aceptada al cerrar el periodo"),
                    ("ACEPTADA_CORR", "Aceptada al reenviar la corrección"),
                    ("RESUELTA", "Definida al resolver la gaceta"),
                    ("CANCELADA", "Cancelada"),
                ],
                max_length=14,
            ),
        ),
        migrations.AlterField(
            model_name="participacionitemgaceta",
            name="estado",
            field=models.CharField(
                choices=[
                    ("EN_REVISION", "En revisión"),
                    ("EN_CORRECCION", "En corrección"),
                    ("APROBADA", "Aceptada"),
                    ("RECHAZADA", "Rechazada"),
                    ("EXCLUIDA", "Excluida"),
                ],
                default="EN_REVISION",
                max_length=14,
            ),
        ),
        migrations.RunPython(retirar_aprobaciones_pendientes, migrations.RunPython.noop),
    ]
