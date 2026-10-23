import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Fase 2 del modelo configurable — bloques contenedores (GRUPO_ACTIVIDADES, RECORRIDO_ITEMS) y bloques
    contenidos (`contenedor` + `rama`), resultado EXCLUIDA en rutas de aprobación, columna EQUIPO y datos de
    inicio en la ejecución. Solo agrega columnas con valor por defecto que los registros existentes ya
    cumplen (sin contenedor, sin rama, datos al iniciar)."""

    dependencies = [
        ("catalogo", "0026_alcance_y_versiones"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bloqueoperativo",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("ACTIVIDAD", "Actividad"),
                    ("APROBACION", "Aprobacion"),
                    ("ESPERA", "Espera"),
                    ("DECISION", "Decision"),
                    ("ENTREGABLE", "Entregable"),
                    ("TAREAS_ELEMENTOS", "Tareas por elemento"),
                    ("GRUPO_ACTIVIDADES", "Actividades en paralelo"),
                    ("RECORRIDO_ITEMS", "Recorrido por ítem"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="bloqueoperativo",
            name="contenedor",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="contenidos", to="catalogo.bloqueoperativo",
            ),
        ),
        migrations.AddField(
            model_name="bloqueoperativo",
            name="rama",
            field=models.CharField(
                blank=True,
                choices=[("", "Flujo principal"), ("ITEM", "Para cada ítem"), ("GENERAL", "General, en paralelo")],
                default="",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="bloqueoperativo",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("contenedor__isnull", True), ("rama", "")),
                    models.Q(("contenedor__isnull", False), ("rama__in", ["ITEM", "GENERAL"])),
                    _connector="OR",
                ),
                name="ck_bloqueop_contenedor_rama",
            ),
        ),
        migrations.AlterField(
            model_name="transicionbloqueoperativo",
            name="resultado_aprobacion",
            field=models.CharField(
                blank=True,
                choices=[
                    ("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"), ("DEVUELTA", "Devuelta"),
                    ("EXCLUIDA", "Excluida"),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="campo",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("TEXTO", "Texto"), ("TEXTO_LARGO", "Texto largo"), ("NUMERO", "Número"), ("FECHA", "Fecha"),
                    ("FECHA_HORA", "Fecha y hora"), ("LISTA", "Lista"), ("MULTILISTA", "Multilista"),
                    ("BOOLEANO", "Booleano"), ("ARCHIVO", "Archivo"), ("USUARIO", "Usuario"), ("AREA", "Área"),
                    ("UNIDAD", "Unidad de negocio"), ("CORREO", "Correo electrónico"), ("URL", "URL"),
                    ("EQUIPO", "Equipo"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="servicio",
            name="datos_inicio_en_ejecucion",
            field=models.BooleanField(default=False),
        ),
    ]
