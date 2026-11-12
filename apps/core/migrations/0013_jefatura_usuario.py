import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Jefatura directa (jefe → colaborador) con vigencia: base de «Mi equipo»."""

    dependencies = [
        ("core", "0012_usuario_visto_en"),
    ]

    operations = [
        migrations.CreateModel(
            name="JefaturaUsuario",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("activo", models.BooleanField(default=True)),
                ("fecha_inicio", models.DateField(default=django.utils.timezone.localdate)),
                ("fecha_fin", models.DateField(blank=True, null=True)),
                ("colaborador", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="jefaturas", to=settings.AUTH_USER_MODEL)),
                ("jefe", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="colaboradores_a_cargo", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(condition=models.Q(("activo", True)), fields=("colaborador",), name="uq_jefatura_vigente_por_colaborador"),
                    models.CheckConstraint(condition=~models.Q(("jefe", models.F("colaborador"))), name="ck_jefatura_no_es_su_propio_jefe"),
                ],
            },
        ),
    ]
