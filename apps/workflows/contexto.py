"""Contexto de ejecución de una `InstanciaWorkflow` — incremento 3.2
(CU-020 "Ejecutar workflow", RQF-066).

Estructura JSON controlada con 3 namespaces fijos (propuesta aprobada,
sección F):

    datos_iniciales    — lo que llega a `iniciar_workflow(datos_iniciales=...)`.
    resultados_etapas   — `{"<etapa_id>": [datos1, datos2, ...]}`, una lista
                          por etapa (no un valor único): un ciclo puede
                          ejecutar la misma `Etapa` varias veces dentro de
                          una instancia y ninguna ejecución anterior debe
                          perderse silenciosamente (corrección aprobada).
                          El historial completo con metadatos (estado,
                          tiempos, `orden`) sigue viviendo en
                          `InstanciaEtapa` — este namespace solo existe
                          porque una Strategy únicamente puede leer del
                          contexto, nunca de la base de datos directamente.
    variables          — el único namespace que `TransicionEtapa.variable`
                          (CONDICION) resuelve. Sin búsqueda implícita en
                          cascada sobre los otros dos namespaces (decisión
                          aprobada): quien necesite promover un dato a
                          variable evaluable lo hace explícitamente vía
                          `ResultadoEjecucionEtapa.variables_actualizadas`.

Solo JSON puro: nunca objetos ORM, nunca `datetime` sin serializar (se
guardan como texto ISO) — mismo criterio que `apps/core/auditoria.py`
guarda las FK como `<campo>_id`, nunca el objeto relacionado.

4.B0 agrega un cuarto namespace, `resultados_bloques`, con el resultado
publicado por bloques concretos:

    {"aprobaciones": {"<clave_del_bloque>": {"resultado": "APROBADA"}}}

Se identifica por la CLAVE ESTABLE del bloque (`BloqueOperativo.clave`), nunca
por pk ni por nombre. Se escribe SOLO con `publicar_resultado_bloque` y lo
consulta `apps.workflows.variables` (`aprobaciones.<clave>.resultado`,
`entregables.<clave>.satisfecho`). Las instancias creadas antes de 4.B0 no tienen
la clave: se tolera su ausencia.

Variable INEXISTENTE ≠ valor NULL (4.B0). `INEXISTENTE` es un centinela interno
que nunca se guarda en JSON: ninguna condición se cumple frente a una variable
inexistente (ni `DISTINTO_DE`, `NO_CONTIENE` o `ESTA_VACIO`), de modo que la
DECISION sigue con otras rutas y puede terminar en su fallback. Una variable
que existe con valor `None` sí participa (`ESTA_VACIO` es verdadero).

Una `Strategy` (`apps/workflows/estrategias.py`) nunca escribe estas
estructuras directamente: solo el motor (`apps/workflows/motor.py`) las
lee/escribe, a través de las funciones de este módulo.
"""

from __future__ import annotations

import operator as _op
from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime


class _Inexistente:
    """Tipo del centinela `INEXISTENTE` (singleton, falso, nunca va a JSON)."""

    _unica = None

    def __new__(cls):
        if cls._unica is None:
            cls._unica = super().__new__(cls)
        return cls._unica

    def __bool__(self):
        return False

    def __repr__(self):
        return "INEXISTENTE"


INEXISTENTE = _Inexistente()

# Ámbitos con resultados publicados por bloques (`resultados_bloques`). Sumar un
# ámbito aquí basta: el resolutor y la API de publicación no necesitan otro cambio.
AMBITOS_BLOQUE = frozenset({"aprobaciones", "entregables"})


def construir_contexto_inicial(datos_iniciales):
    return {
        "datos_iniciales": datos_iniciales or {},
        "resultados_etapas": {},
        "resultados_bloques": {},
        "variables": {},
    }


def resolver_variable(contexto, nombre):
    """Variable PLANA de una CONDICION (contrato anterior a 4.B0, sin cambios
    de sintaxis): solo consulta `variables`. Devuelve el valor — que puede ser
    `None` — o `INEXISTENTE` si no existe. Las rutas punteadas (`ticket.estado`,
    `formulario.<clave>`, `aprobaciones.<clave>.resultado`) las resuelve
    `apps.workflows.variables`, que cae a esta función para lo plano."""
    variables = contexto.get("variables") or {}
    if nombre in variables:
        return variables[nombre]
    return INEXISTENTE


def publicar_resultado_bloque(contexto, ambito, clave, datos):
    """Fija el resultado de un bloque concreto (`ambito`/`clave`). Única vía de
    escritura de `resultados_bloques`. Lo invoca el motor bajo el lock de la
    `InstanciaWorkflow` y mientras la ejecución del bloque sigue activa (EN_ESPERA):
    una ejecución COMPLETADA no puede volver a publicar. Si un ciclo ejecuta el bloque
    otra vez, el resultado actual reemplaza al anterior (el historial completo
    sigue en `InstanciaEtapa`/auditoría — sin event sourcing)."""
    if ambito not in AMBITOS_BLOQUE:
        raise ValueError(f"Ámbito de resultados desconocido: {ambito!r}.")
    if not clave:
        raise ValueError("El bloque no tiene clave: no se puede publicar su resultado.")
    contexto.setdefault("resultados_bloques", {}).setdefault(ambito, {})[clave] = dict(datos or {})


def leer_resultado_bloque(contexto, ambito, clave, campo):
    """Valor de `campo` del resultado publicado por el bloque `clave`, o
    `INEXISTENTE` si el bloque aún no publicó nada o no publicó ese campo."""
    resultado = ((contexto.get("resultados_bloques") or {}).get(ambito) or {}).get(clave)
    if not isinstance(resultado, dict) or campo not in resultado:
        return INEXISTENTE
    return resultado[campo]


def registrar_resultado_etapa(contexto, etapa_id, datos):
    """Agrega `datos` a la lista de ejecuciones de `etapa_id` — nunca
    sobrescribe la entrada existente (ver docstring del módulo)."""
    clave = str(etapa_id)
    contexto.setdefault("resultados_etapas", {}).setdefault(clave, []).append(datos or {})


def aplicar_variables(contexto, variables_actualizadas):
    if variables_actualizadas:
        contexto.setdefault("variables", {}).update(variables_actualizadas)


def _contiene(a, b):
    if a is None:
        return False
    return b in a


def _no_contiene(a, b):
    return not _contiene(a, b)


def _esta_vacio(a, _b):
    return a is None or a == "" or a == []


def _no_esta_vacio(a, b):
    return not _esta_vacio(a, b)


OPERADORES = {
    "IGUAL_A": _op.eq,
    "DISTINTO_DE": _op.ne,
    "CONTIENE": _contiene,
    "NO_CONTIENE": _no_contiene,
    "MAYOR_QUE": _op.gt,
    "MENOR_QUE": _op.lt,
    "ESTA_VACIO": _esta_vacio,
    "NO_ESTA_VACIO": _no_esta_vacio,
}
"""Misma forma que `apps.catalogo.reglas.OPERADORES` (8 operadores, mismos
códigos) pero no se reutiliza literalmente: esa versión está acoplada a
`EstrategiaCampo.normalizar()`, que tipifica según un `Campo` real — aquí
no hay ningún `Campo`, solo JSON crudo del contexto. Reimplementar estas
~10 líneas es más simple que introducir esa dependencia cruzada."""


class _NoConvertible:
    """El texto de la transición no puede interpretarse con el tipo de la variable."""


_NO_CONVERTIBLE = _NoConvertible()
_VERDADEROS = frozenset({"true", "1", "si", "sí", "verdadero", "yes"})
_FALSOS = frozenset({"false", "0", "no", "falso"})


def _a_bool(texto):
    minuscula = texto.lower()
    if minuscula in _VERDADEROS:
        return True
    if minuscula in _FALSOS:
        return False
    return _NO_CONVERTIBLE


def _a_decimal(valor):
    try:
        return Decimal(str(valor).strip())
    except ArithmeticError:
        return _NO_CONVERTIBLE


def _a_fecha(texto):
    try:
        dia = parse_date(texto)
        if dia is not None:
            return dia
        momento = parse_datetime(texto)
    except ValueError:
        return _NO_CONVERTIBLE
    return momento.date() if momento is not None else _NO_CONVERTIBLE


def _a_fecha_hora(texto, referencia):
    try:
        valor = parse_datetime(texto)
        if valor is None:
            dia = parse_date(texto)
            if dia is None:
                return _NO_CONVERTIBLE
            valor = datetime.combine(dia, datetime.min.time())
    except ValueError:
        return _NO_CONVERTIBLE
    if timezone.is_aware(referencia) and timezone.is_naive(valor):
        valor = timezone.make_aware(valor)
    return valor


def _interpretar(valor_actual, texto):
    """`TransicionBloqueOperativo.valor`/`TransicionEtapa.valor` siempre es texto.
    Se interpreta con el tipo lógico de la variable (nunca al revés), para que
    `10 > 2` sea numérico y no `"10" < "2"`. Devuelve `_NO_CONVERTIBLE` si no se
    puede; texto y listas se comparan tal cual."""
    texto = "" if texto is None else str(texto).strip()
    if isinstance(valor_actual, bool):
        return _a_bool(texto)
    if isinstance(valor_actual, (int, float, Decimal)):
        return _a_decimal(texto)
    if isinstance(valor_actual, datetime):
        return _a_fecha_hora(texto, valor_actual)
    if isinstance(valor_actual, date):
        return _a_fecha(texto)
    return texto


def _numero(valor):
    """Valor numérico comparable (Decimal) o el mismo valor si no es numérico."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        return valor
    return valor if isinstance(valor, Decimal) else Decimal(str(valor))


def evaluar_operador(operador, valor_actual, valor_transicion):
    """Usado únicamente por `EstrategiaCondicion.ejecutar()`
    (`apps/workflows/estrategias.py`) — el motor nunca evalúa una
    condición por sí mismo (RN-021, corrección aprobada).

    - `INEXISTENTE`: ningún operador se cumple (ver docstring del módulo).
    - `None` (existe y es NULL): `ESTA_VACIO` verdadero, `DISTINTO_DE`/`NO_CONTIENE`
      verdaderos (no es igual ni contiene), las comparaciones no se cumplen.
    - Tipos: bool, int/float/Decimal, date, datetime, texto y listas se comparan
      según el tipo lógico de la variable; un texto de la transición que no se
      puede interpretar con ese tipo no cumple ninguna condición salvo
      `DISTINTO_DE` (no es igual).
    - Valores incompatibles (p. ej. `CONTIENE` sobre un número) no cumplen la
      condición: no son un error del sistema."""
    if valor_actual is INEXISTENTE:
        return False
    if operador in ("ESTA_VACIO", "NO_ESTA_VACIO"):
        return bool(OPERADORES[operador](valor_actual, None))
    if valor_actual is None:
        return operador in ("DISTINTO_DE", "NO_CONTIENE")

    esperado = _interpretar(valor_actual, valor_transicion)
    if esperado is _NO_CONVERTIBLE:
        return operador == "DISTINTO_DE"
    actual = _numero(valor_actual)
    esperado = _numero(esperado)
    try:
        return bool(OPERADORES[operador](actual, esperado))
    except (TypeError, ValueError, ArithmeticError):
        # Comparación entre valores incompatibles (ej. MAYOR_QUE entre fecha y
        # texto): no satisface la condición, no es un error del sistema (mismo
        # criterio que `apps.catalogo.reglas.EspecificacionRegla.es_satisfecha_por`).
        return False
