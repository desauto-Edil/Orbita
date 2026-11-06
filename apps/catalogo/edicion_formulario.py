"""Edición del BORRADOR de un formulario: campos, opciones de lista y reglas condicionales — Bloque 5.6.

Única operación de dominio para crear o modificar estas piezas. La usan el Studio (formulario de inicio y
columnas de una colección; sus vistas solo interpretan el formulario HTML) y el comando de la demo
(`preparar_demo_orbita`), así ninguno de los dos salta una validación:

- permiso: `catalogo.administrar` y `formulario.administrar` (los mismos que exigen las vistas);
- la versión debe seguir en BORRADOR (`exigir_editable`, que ya hacen los `save()` de los modelos);
- un campo de tipo EQUIPO solo existe en las columnas de una colección;
- el resto lo validan los modelos (`full_clean`: tipo, clave única, fecha requerida, Strategy del tipo,
  integridad y composición de las reglas).

`configuracion` es el diccionario que admite la Strategy del tipo (`apps.catalogo.campos`); la vista lo arma
con `CampoForm.configuracion_desde_tipo`. Como antes, estas ediciones del borrador no se auditan una a una:
lo auditado es la activación de la versión.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import Campo, OpcionCampo, ReglaCondicional
from apps.core.autorizacion import usuario_tiene_permiso

# Tipos de columna que el formulario de inicio no admite y tipos que una colección no admite.
TIPOS_SOLO_EN_ELEMENTOS = frozenset({"EQUIPO"})
TIPOS_NO_ADMITIDOS_EN_ELEMENTOS = frozenset()

MENSAJE_EQUIPO_SOLO_EN_ELEMENTOS = "Un campo de tipo equipo solo existe en las columnas de los elementos del proceso."


def _exigir_permisos(actor):
    if not (usuario_tiene_permiso(actor, "catalogo.administrar") and usuario_tiene_permiso(actor, "formulario.administrar")):
        raise PermissionDenied("No tiene autorización para editar formularios.")


@transaction.atomic
def guardar_campo(
    version, actor, *, tipo, etiqueta, clave="", ayuda="", obligatorio=False, orden=0, es_fecha_requerida=False,
    configuracion=None, en_elementos=False, campo=None,
):
    """Crea (sin `campo`) o edita un campo del borrador `version`. `en_elementos=True` = es una columna de una
    colección de ítems. Una clave vacía se genera desde la etiqueta al crear y se conserva al editar."""
    _exigir_permisos(actor)
    if not en_elementos and tipo in TIPOS_SOLO_EN_ELEMENTOS:
        raise ValidationError(MENSAJE_EQUIPO_SOLO_EN_ELEMENTOS)
    if en_elementos and tipo in TIPOS_NO_ADMITIDOS_EN_ELEMENTOS:
        raise ValidationError("Ese tipo de campo no se admite en las columnas de una colección.")
    if campo is None:
        campo = Campo(version=version)
    elif campo.version_id != version.pk:
        raise ValidationError("El campo no pertenece a este borrador.")
    clave = (clave or "").strip().lower().replace("-", "_")
    campo.tipo = tipo
    campo.etiqueta = etiqueta
    campo.clave = clave or (campo.clave if campo.pk else "")
    campo.ayuda = ayuda or ""
    campo.obligatorio = bool(obligatorio)
    campo.orden = orden or 0
    campo.es_fecha_requerida = bool(es_fecha_requerida)
    campo.configuracion = dict(configuracion or {})
    campo.full_clean()
    campo.save()
    return campo


@transaction.atomic
def guardar_opcion(campo, actor, *, valor, etiqueta, orden=0, opcion=None):
    """Crea (sin `opcion`) o edita una opción de un campo de lista del borrador."""
    _exigir_permisos(actor)
    if opcion is None:
        opcion = OpcionCampo(campo=campo)
    elif opcion.campo_id != campo.pk:
        raise ValidationError("La opción no pertenece a este campo.")
    opcion.valor = valor
    opcion.etiqueta = etiqueta
    opcion.orden = orden or 0
    opcion.full_clean()
    opcion.save()
    return opcion


@transaction.atomic
def guardar_regla(version, actor, *, campo_origen, operador, valor="", campo_objetivo, efecto, regla=None):
    """Crea (sin `regla`) o edita una regla condicional entre dos campos del MISMO borrador."""
    _exigir_permisos(actor)
    if campo_origen.version_id != version.pk or campo_objetivo.version_id != version.pk:
        raise ValidationError("La regla solo puede relacionar campos de este formulario.")
    if regla is None:
        regla = ReglaCondicional()
    elif regla.campo_origen.version_id != version.pk:
        raise ValidationError("La regla no pertenece a este formulario.")
    regla.campo_origen = campo_origen
    regla.operador = operador
    regla.valor = valor or ""
    regla.campo_objetivo = campo_objetivo
    regla.efecto = efecto
    regla.full_clean()
    regla.save()
    return regla
