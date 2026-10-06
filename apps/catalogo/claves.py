"""Claves estables de `Campo` y `BloqueOperativo` (Sprint 4.B0).

Una clave identifica CONCEPTUALMENTE un campo o un bloque a lo largo de las
versiones: el Workflow la usa para referirse a "la respuesta `valor_estimado`" o
a "el resultado del bloque `aprobacion_jefe`" sin depender de la etiqueta/nombre
visible (que cambia) ni del pk (que cambia en cada clonación de versión).

Reglas:

- Formato `[a-z][a-z0-9_]*`, hasta `LARGO_MAXIMO` caracteres. Sin punto: el punto
  separa los segmentos de una referencia (`formulario.<clave>`).
- Única dentro de su versión (formulario / configuración de ejecución), no
  global: dos formularios distintos pueden tener `monto`.
- Se genera UNA vez desde la etiqueta/nombre al crear el objeto, con sufijo
  determinista ante colisión (`tipo_cliente`, `tipo_cliente_2`, …). Cambiar la
  etiqueta/nombre después NO la regenera.
- Se copia siempre al clonar una versión.

Funciones puras, sin acceso a base de datos.
"""

import re

from django.core.exceptions import ValidationError

from apps.catalogo.normalizacion import normalizar

LARGO_MAXIMO = 60
PATRON_CLAVE = re.compile(r"^[a-z][a-z0-9_]*$")


def validar_clave(valor):
    """Validador de campo: rechaza lo que no tenga el formato de clave. La clave
    vacía la valida el propio campo (`blank=True`): aquí solo se mira el formato."""
    if valor and not PATRON_CLAVE.match(valor):
        raise ValidationError(
            "La clave debe empezar con una letra minúscula y usar solo minúsculas, números y guion bajo."
        )


def clave_desde_texto(texto, *, por_defecto):
    """Clave legible a partir de `texto` ("¿Valor estimado?" → `valor_estimado`).
    Si el texto no aporta ningún carácter utilizable, usa `por_defecto`."""
    base = normalizar(texto).replace(" ", "_")
    base = re.sub(r"[^a-z0-9_]", "", base).strip("_")
    if not base:
        return por_defecto
    if not base[0].isalpha():
        base = f"{por_defecto}_{base}"
    return base[:LARGO_MAXIMO].rstrip("_") or por_defecto


def clave_unica(base, existentes):
    """`base` si está libre; si no, `base_2`, `base_3`, … (el sufijo cabe siempre
    dentro de `LARGO_MAXIMO`: se recorta la base, no el sufijo)."""
    existentes = set(existentes)
    if base not in existentes:
        return base
    numero = 2
    while True:
        sufijo = f"_{numero}"
        candidata = f"{base[: LARGO_MAXIMO - len(sufijo)].rstrip('_')}{sufijo}"
        if candidata not in existentes:
            return candidata
        numero += 1


def generar_clave(texto, existentes, *, por_defecto):
    return clave_unica(clave_desde_texto(texto, por_defecto=por_defecto), existentes)
