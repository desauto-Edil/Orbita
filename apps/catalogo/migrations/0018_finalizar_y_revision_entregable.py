import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.E2 — dos cambios aditivos del flujo por fases.

    - `BloqueOperativo.entregable_revisado`: una APROBACION puede REFERENCIAR, de forma
      explícita y opcional, el bloque ENTREGABLE que revisa. Las filas existentes quedan en
      NULL (aprobación general), que es su semántica actual.
    - `TransicionBloqueOperativo.finaliza` + `bloque_destino` nullable: una ruta puede
      FINALIZAR el flujo en lugar de apuntar a un bloque. Las filas existentes conservan su
      destino (`finaliza=False`); la restricción impide que un destino vacío sea un fin
      implícito."""

    dependencies = [
        ("catalogo", "0017_bloque_entregable"),
    ]

    operations = [
        migrations.AddField(
            model_name="bloqueoperativo",
            name="entregable_revisado",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT,
                related_name="aprobaciones_que_lo_revisan", to="catalogo.bloqueoperativo",
            ),
        ),
        migrations.AddField(
            model_name="transicionbloqueoperativo",
            name="finaliza",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="transicionbloqueoperativo",
            name="bloque_destino",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="transiciones_entrantes", to="catalogo.bloqueoperativo",
            ),
        ),
        migrations.AddConstraint(
            model_name="transicionbloqueoperativo",
            constraint=models.CheckConstraint(
                condition=(
                    Q(finaliza=True, bloque_destino__isnull=True)
                    | Q(finaliza=False, bloque_destino__isnull=False)
                ),
                name="ck_transbloque_destino_o_finaliza",
            ),
        ),
    ]
