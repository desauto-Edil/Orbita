import re
import unicodedata

import apps.catalogo.claves
from django.db import migrations, models
from django.db.models import Q

LARGO_MAXIMO = 60


def _clave_desde_texto(texto, por_defecto):
    """Copia deliberada de `apps.catalogo.claves.clave_desde_texto`: una migración
    debe seguir produciendo el mismo resultado aunque el módulo cambie después."""
    descompuesto = unicodedata.normalize("NFKD", str(texto or ""))
    sin_acentos = "".join(c for c in descompuesto if not unicodedata.combining(c))
    base = re.sub(r"[\W_]+", "_", sin_acentos.casefold(), flags=re.UNICODE)
    base = re.sub(r"[^a-z0-9_]", "", base).strip("_")
    if not base:
        return por_defecto
    if not base[0].isalpha():
        base = f"{por_defecto}_{base}"
    return base[:LARGO_MAXIMO].rstrip("_") or por_defecto


def _clave_unica(base, usadas):
    if base not in usadas:
        return base
    numero = 2
    while True:
        sufijo = f"_{numero}"
        candidata = f"{base[: LARGO_MAXIMO - len(sufijo)].rstrip('_')}{sufijo}"
        if candidata not in usadas:
            return candidata
        numero += 1


def asignar_claves(apps, schema_editor):
    """Backfill determinista POR VERSIÓN: la clave sale de la etiqueta/nombre y las
    colisiones dentro de la versión reciben sufijo `_2`, `_3`… en el orden en que el
    objeto aparece (`orden, id` en campos; `fase, orden, id` en bloques).

    Limitación histórica documentada: el modelo anterior no guardaba qué campo de una
    versión es el "mismo" de otra, y no se infiere. Si una etiqueta no cambió entre
    versiones (lo normal: una versión es un clon), el mismo texto produce la misma
    clave; si cambió, el campo queda con otra clave en la versión nueva. Desde esta
    migración toda clonación copia la clave. Solo escribe `clave` (`update`): no toca
    `actualizado_en` ni dispara auditoría de datos históricos."""
    Campo = apps.get_model("catalogo", "Campo")
    FormularioVersion = apps.get_model("catalogo", "FormularioVersion")
    for version_pk in FormularioVersion.objects.order_by("pk").values_list("pk", flat=True):
        usadas = set(Campo.objects.filter(version_id=version_pk).exclude(clave="").values_list("clave", flat=True))
        for campo in Campo.objects.filter(version_id=version_pk, clave="").order_by("orden", "id"):
            clave = _clave_unica(_clave_desde_texto(campo.etiqueta, "campo"), usadas)
            usadas.add(clave)
            Campo.objects.filter(pk=campo.pk).update(clave=clave)

    Bloque = apps.get_model("catalogo", "BloqueOperativo")
    Configuracion = apps.get_model("catalogo", "ConfiguracionEjecucionVersion")
    for version_pk in Configuracion.objects.order_by("pk").values_list("pk", flat=True):
        usadas = set(Bloque.objects.filter(version_id=version_pk).exclude(clave="").values_list("clave", flat=True))
        for bloque in Bloque.objects.filter(version_id=version_pk, clave="").order_by("fase_id", "orden", "id"):
            clave = _clave_unica(_clave_desde_texto(bloque.nombre, bloque.tipo.lower() or "bloque"), usadas)
            usadas.add(clave)
            Bloque.objects.filter(pk=bloque.pk).update(clave=clave)


class Migration(migrations.Migration):
    """4.B0 — claves estables de `Campo` y `BloqueOperativo`.

    Las restricciones se crean ANTES del backfill (excluyen la clave vacía, así que
    las filas existentes las cumplen) para que ningún cambio de esquema ocurra
    después de actualizar datos dentro de la misma transacción."""

    dependencies = [
        ("catalogo", "0015_terminoservicio"),
    ]

    operations = [
        migrations.AddField(
            model_name="campo",
            name="clave",
            field=models.SlugField(
                blank=True, db_index=False, default="", max_length=60,
                help_text="Identificador estable del campo para reglas y flujos (p. ej. valor_estimado). "
                "No cambia al renombrar la etiqueta.",
                validators=[apps.catalogo.claves.validar_clave],
            ),
        ),
        migrations.AddField(
            model_name="bloqueoperativo",
            name="clave",
            field=models.SlugField(
                blank=True, db_index=False, default="", max_length=60,
                help_text="Identificador estable del bloque para referenciar sus resultados.",
                validators=[apps.catalogo.claves.validar_clave],
            ),
        ),
        migrations.AddConstraint(
            model_name="campo",
            constraint=models.UniqueConstraint(
                condition=~Q(clave=""), fields=("version", "clave"), name="uq_campo_version_clave"
            ),
        ),
        migrations.AddConstraint(
            model_name="bloqueoperativo",
            constraint=models.UniqueConstraint(
                condition=~Q(clave=""), fields=("version", "clave"), name="uq_bloqueop_version_clave"
            ),
        ),
        migrations.RunPython(asignar_claves, migrations.RunPython.noop),
    ]
