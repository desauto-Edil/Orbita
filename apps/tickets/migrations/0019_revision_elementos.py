import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    """4.G2C1 — revisión individual de elementos por un entregable de conjunto: columnas revisadas y
    revisores congelados por Ticket, y las decisiones (`RevisionElemento`, append-only). Aditiva: ningún
    Ticket existente tiene entregables de este tipo y no se crea ningún dato."""

    dependencies = [
        ("catalogo", "0022_entregable_revision_elementos"),
        ("core", "0005_seed_permiso_auditoria_consultar"),
        ("tickets", "0018_parrilla_ticket"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="entregableticket",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("TEXTO", "Texto"), ("ARCHIVO", "Archivo"), ("ENLACE", "Enlace"),
                    ("CONFIRMACION", "Confirmación"), ("ELEMENTOS", "Revisión de elementos"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="resultadoentregaticket",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("TEXTO", "Texto"), ("ARCHIVO", "Archivo"), ("ENLACE", "Enlace"),
                    ("CONFIRMACION", "Confirmación"), ("ELEMENTOS", "Revisión de elementos"),
                ],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="ColumnaRevisadaEntregable",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "campo",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalogo.campo"
                    ),
                ),
                (
                    "entregable",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="columnas_revisadas",
                        to="tickets.entregableticket",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RevisorEntregable",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "entregable",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="revisores",
                        to="tickets.entregableticket",
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
        ),
        migrations.CreateModel(
            name="RevisionElemento",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                (
                    "decision",
                    models.CharField(choices=[("APROBADA", "Aprobada"), ("DEVUELTA", "Devuelta")], max_length=10),
                ),
                ("observacion", models.TextField(blank=True)),
                ("decidido_en", models.DateTimeField()),
                ("huella", models.CharField(max_length=64)),
                ("contenido", models.TextField()),
                (
                    "elemento",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="revisiones",
                        to="tickets.elementoparrilla",
                    ),
                ),
                (
                    "entregable",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="revisiones",
                        to="tickets.entregableticket",
                    ),
                ),
                (
                    "revisor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
            options={"ordering": ["pk"]},
        ),
        migrations.AddIndex(
            model_name="revisionelemento",
            index=models.Index(fields=["entregable", "elemento"], name="ix_revisionelemento_ent_elem"),
        ),
        migrations.AddConstraint(
            model_name="columnarevisadaentregable",
            constraint=models.UniqueConstraint(fields=("entregable", "campo"), name="uq_columnarevisadaentregable_campo"),
        ),
        migrations.AddConstraint(
            model_name="revisorentregable",
            constraint=models.CheckConstraint(
                condition=Q(usuario__isnull=False, equipo__isnull=True) | Q(usuario__isnull=True, equipo__isnull=False),
                name="ck_revisorentregable_usuario_o_equipo",
            ),
        ),
        migrations.AddConstraint(
            model_name="revisorentregable",
            constraint=models.UniqueConstraint(
                condition=Q(usuario__isnull=False), fields=("entregable", "usuario"), name="uq_revisorentregable_usuario"
            ),
        ),
        migrations.AddConstraint(
            model_name="revisorentregable",
            constraint=models.UniqueConstraint(
                condition=Q(equipo__isnull=False), fields=("entregable", "equipo"), name="uq_revisorentregable_equipo"
            ),
        ),
        migrations.AddConstraint(
            model_name="revisionelemento",
            constraint=models.CheckConstraint(
                condition=Q(decision="APROBADA") | ~Q(observacion=""), name="ck_revisionelemento_devolucion_observada"
            ),
        ),
    ]
