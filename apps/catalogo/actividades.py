"""Configuración ampliada del bloque ACTIVIDAD (Fase 1 del modelo configurable de procesos).

Única autoridad del contrato JSON que un `BloqueOperativo` ACTIVIDAD guarda además del responsable
(`tipo_actor`/`usuario_id`/`equipo_id`/`permite_subtareas`, que no cambian):

- `requiere_evidencia` (bool): la Tarea solo se completa con al menos una evidencia adjunta.
- `plazo_previsto` / `plazo_limite` (opcionales): `{"cantidad": int, "unidad": "DIAS"|"HORAS"}`,
  RELATIVOS al momento en que el flujo llega al bloque. Se calculan con `apps.tickets.tiempos`
  (única aritmética de fechas) y se copian a `Tarea.fecha_prevista` / `Tarea.fecha_limite`.
- `plazo_habiles` (bool): ambos plazos cuentan solo días/horas hábiles.
- `verificacion`: lista de `{"texto": str, "obligatorio": bool}` que se copia a la Tarea.

Fase 2 (todas opcionales):

- `fecha_prevista_campo` / `fecha_limite_campo`: referencia a un dato con fecha del que sale esa fecha, en
  lugar del plazo relativo: `formulario.<clave>` (campo FECHA/FECHA_HORA del formulario de inicio) o
  `item.<clave>` (columna FECHA/FECHA_HORA del ítem, solo en un recorrido por ítem). Una FECHA se toma al
  final de ese día (hora local); una FECHA_HORA, tal cual. Si el dato está vacío, la Tarea no recibe esa
  fecha (no se inventa). Una misma fecha es relativa O tomada de un dato, nunca las dos.
- `exige_datos_inicio` (bool): la Tarea solo se completa cuando los datos de inicio del ticket ya se
  completaron (`apps.tickets.datos_inicio`); lo hace cumplir la integración Tarea ↔ Workflow.

Bloque 3.1 (opcional):

- `columnas_editables`: `[clave, …]` de columnas del ítem que el responsable de la Tarea ABIERTA puede editar
  en ESE ítem (solo en un recorrido por ítem; las claves se validan contra la colección recorrida al publicar).
  Vacía = la actividad no concede edición. La autorización vive en `apps.tickets.edicion_por_tarea`.

La descripción operativa es la del propio bloque (`BloqueOperativo.descripcion`), que ya se copia a la
Tarea. Configuraciones anteriores sin estas claves siguen siendo válidas: todo es opcional.
"""

import re
from datetime import date, datetime, time

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.tickets import tiempos

_REFERENCIA_FECHA = re.compile(r"^(formulario|item)\.[a-z0-9_-]+$")

_CLAVE_COLUMNA = re.compile(r"^[a-z0-9_-]+$")

MAX_VERIFICACIONES = 20
MAX_TEXTO_VERIFICACION = 200

# Equivalencia SOLO para validar que lo previsto no supere lo límite con unidades distintas.
_HORAS_POR_UNIDAD = {tiempos.UNIDAD_HORAS: 1, tiempos.UNIDAD_DIAS: 24}


def _plazo(valor, etiqueta, errores):
    if valor in (None, "", {}):
        return None
    if not isinstance(valor, dict):
        errores.append(f"El {etiqueta} no es válido.")
        return None
    cantidad, unidad = valor.get("cantidad"), valor.get("unidad")
    if isinstance(cantidad, bool) or not isinstance(cantidad, int) or not 1 <= cantidad <= tiempos.MAX_CANTIDAD:
        errores.append(f"El {etiqueta} debe ser un número entero de 1 a {tiempos.MAX_CANTIDAD}.")
        return None
    if unidad not in tiempos.UNIDADES:
        errores.append(f"El {etiqueta} necesita una unidad: días u horas.")
        return None
    return {"cantidad": cantidad, "unidad": unidad}


def _verificaciones(valor, errores):
    if valor in (None, ""):
        return []
    if not isinstance(valor, list):
        errores.append("La lista de verificación no es válida.")
        return []
    if len(valor) > MAX_VERIFICACIONES:
        errores.append(f"La lista de verificación admite hasta {MAX_VERIFICACIONES} ítems.")
    items = []
    for dato in valor[:MAX_VERIFICACIONES]:
        texto = (dato.get("texto") if isinstance(dato, dict) else "") or ""
        texto = str(texto).strip()
        if not texto:
            errores.append("Cada ítem de la lista de verificación necesita un texto.")
            continue
        if len(texto) > MAX_TEXTO_VERIFICACION:
            errores.append(f"Un ítem de la lista de verificación supera {MAX_TEXTO_VERIFICACION} caracteres.")
            continue
        items.append({"texto": texto, "obligatorio": bool(dato.get("obligatorio", True))})
    return items


def columnas_editables(valor, errores):
    """Lista canónica (sin repetidos, en orden) de claves de columna; un valor que no es una clave es un error.
    La comparten las tres modalidades que conceden edición por tarea (actividad, tareas por elemento, gaceta)."""
    if valor in (None, "", []):
        return []
    if not isinstance(valor, (list, tuple)):
        errores.append("Las columnas editables no son válidas.")
        return []
    claves = []
    for clave in valor:
        if not isinstance(clave, str) or not _CLAVE_COLUMNA.match(clave):
            errores.append("Las columnas editables deben indicarse por la clave de la columna.")
            continue
        if clave not in claves:
            claves.append(clave)
    return claves


def normalizar(configuracion):
    """Devuelve `(ampliacion, errores)`: la parte ampliada de la configuración, limpia y canónica, y la
    lista de errores (vacía si es válida). No toca las claves del responsable."""
    cfg = configuracion or {}
    errores = []
    previsto = _plazo(cfg.get("plazo_previsto"), "plazo previsto", errores)
    limite = _plazo(cfg.get("plazo_limite"), "plazo límite", errores)
    if previsto and limite:
        horas = lambda p: p["cantidad"] * _HORAS_POR_UNIDAD[p["unidad"]]  # noqa: E731
        if horas(previsto) > horas(limite):
            errores.append("La fecha prevista no puede quedar después de la fecha límite.")
    prevista_campo = _referencia_fecha(cfg.get("fecha_prevista_campo"), "fecha prevista", errores)
    limite_campo = _referencia_fecha(cfg.get("fecha_limite_campo"), "fecha límite", errores)
    if prevista_campo and previsto:
        errores.append("La fecha prevista sale de un plazo o de un dato con fecha, no de ambos.")
    if limite_campo and limite:
        errores.append("La fecha límite sale de un plazo o de un dato con fecha, no de ambos.")
    ampliacion = {
        "requiere_evidencia": bool(cfg.get("requiere_evidencia")),
        "plazo_habiles": bool(cfg.get("plazo_habiles")),
        "plazo_previsto": previsto,
        "plazo_limite": limite,
        "verificacion": _verificaciones(cfg.get("verificacion"), errores),
        "fecha_prevista_campo": prevista_campo,
        "fecha_limite_campo": limite_campo,
        "exige_datos_inicio": bool(cfg.get("exige_datos_inicio")),
        "columnas_editables": columnas_editables(cfg.get("columnas_editables"), errores),
    }
    return ampliacion, errores


def _referencia_fecha(valor, etiqueta, errores):
    if valor in (None, ""):
        return ""
    if not isinstance(valor, str) or not _REFERENCIA_FECHA.match(valor):
        errores.append(f"El dato del que sale la {etiqueta} no es válido.")
        return ""
    return valor


def fecha_desde_valor(valor):
    """Fecha-hora (aware) de un valor tipado de un campo o columna: una FECHA es el final de ese día en la
    hora local; una FECHA_HORA se usa tal cual. Cualquier otra cosa (vacío, texto) da `None`."""
    if isinstance(valor, datetime):
        return valor if timezone.is_aware(valor) else timezone.make_aware(valor)
    if isinstance(valor, date):
        return timezone.make_aware(datetime.combine(valor, time(23, 59, 59)))
    return None


def errores_de_actividad(configuracion):
    return normalizar(configuracion)[1]


def validar(configuracion):
    errores = errores_de_actividad(configuracion)
    if errores:
        raise ValidationError(errores)


def fechas_desde(configuracion, desde, resolver=None):
    """`(fecha_prevista, fecha_limite)` (aware o `None`) de una configuración YA validada: los plazos se
    cuentan desde `desde` (la llegada del flujo al bloque); las fechas tomadas de un dato se leen con
    `resolver(referencia)` (p. ej. `ResolutorVariables.resolver`). Sin `resolver`, esas quedan vacías."""
    ampliacion, _errores = normalizar(configuracion)
    habiles = ampliacion["plazo_habiles"]

    def calcular(plazo, referencia):
        if referencia:
            return fecha_desde_valor(resolver(referencia)) if resolver is not None else None
        if plazo is None:
            return None
        return tiempos.calcular_fecha_objetivo(desde, plazo["cantidad"], plazo["unidad"], habiles)

    return (
        calcular(ampliacion["plazo_previsto"], ampliacion["fecha_prevista_campo"]),
        calcular(ampliacion["plazo_limite"], ampliacion["fecha_limite_campo"]),
    )
