from django.db import migrations, models


class Migration(migrations.Migration):
    """Tipo de campo «Lista de evidencias» (CHECKLIST): solo cambian las opciones; sin cambios en la BD."""

    dependencies = [
        ("catalogo", "0032_recurso_demo"),
    ]

    operations = [
        migrations.AlterField(
            model_name="campo",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("TEXTO", "Texto"), ("TEXTO_LARGO", "Texto largo"), ("NUMERO", "Número"), ("FECHA", "Fecha"),
                    ("FECHA_HORA", "Fecha y hora"), ("LISTA", "Lista"), ("MULTILISTA", "Multilista"),
                    ("BOOLEANO", "Booleano"), ("ARCHIVO", "Archivo"), ("USUARIO", "Usuario"), ("AREA", "Área"),
                    ("UNIDAD", "Unidad de negocio"), ("CORREO", "Correo electrónico"), ("URL", "URL"),
                    ("EQUIPO", "Equipo"), ("CHECKLIST", "Lista de evidencias"),
                ],
                max_length=20,
            ),
        ),
    ]
