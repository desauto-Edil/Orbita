import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0009_configuracion_ejecucion_fases"),
    ]

    operations = [
        migrations.CreateModel(
            name="TransicionBloqueOperativo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("actualizado_en", models.DateTimeField(auto_now=True)),
                ("nombre", models.CharField(blank=True, max_length=150)),
                ("prioridad", models.PositiveIntegerField(default=0)),
                ("variable", models.CharField(blank=True, max_length=150)),
                (
                    "operador",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("IGUAL_A", "Igual a"),
                            ("DISTINTO_DE", "Distinto de"),
                            ("CONTIENE", "Contiene"),
                            ("NO_CONTIENE", "No contiene"),
                            ("MAYOR_QUE", "Mayor que"),
                            ("MENOR_QUE", "Menor que"),
                            ("ESTA_VACIO", "Esta vacio"),
                            ("NO_ESTA_VACIO", "No esta vacio"),
                        ],
                        max_length=20,
                    ),
                ),
                ("valor", models.CharField(blank=True, max_length=255)),
                ("es_fallback", models.BooleanField(default=False)),
                (
                    "resultado_aprobacion",
                    models.CharField(
                        blank=True,
                        choices=[("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"), ("DEVUELTA", "Devuelta")],
                        max_length=10,
                    ),
                ),
                (
                    "bloque_destino",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transiciones_entrantes",
                        to="catalogo.bloqueoperativo",
                    ),
                ),
                (
                    "bloque_origen",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transiciones_salientes",
                        to="catalogo.bloqueoperativo",
                    ),
                ),
            ],
            options={
                "ordering": ["bloque_origen_id", "prioridad", "id"],
            },
        ),
    ]
