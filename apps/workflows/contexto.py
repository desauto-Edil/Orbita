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

Una `Strategy` (`apps/workflows/estrategias.py`) nunca escribe estas
estructuras directamente: solo el motor (`apps/workflows/motor.py`) las
lee/escribe, a través de las funciones de este módulo.
"""

from __future__ import annotations

import operator as _op


def construir_contexto_inicial(datos_iniciales):
    return {
        "datos_iniciales": datos_iniciales or {},
        "resultados_etapas": {},
        "variables": {},
    }


def resolver_variable(contexto, nombre):
    """Única forma en que una CONDICION consulta el contexto (sección F,
    aprobada) — siempre contra `variables`, nunca contra los otros
    namespaces. Variable inexistente -> `None` (mismo criterio que
    `apps.catalogo.reglas.EspecificacionRegla` para un campo sin
    respuesta todavía)."""
    return contexto.get("variables", {}).get(nombre)


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


def _coercionar(valor_actual, valor_transicion):
    """`TransicionEtapa.valor` siempre es texto (`CharField`); `valor_actual`
    viene del contexto y puede ser cualquier tipo JSON. Se intenta
    interpretar `valor_transicion` con el mismo tipo que `valor_actual`
    (si no, `5 == "5"` sería `False` en Python) — si no es convertible, se
    compara como texto."""
    if isinstance(valor_actual, bool):
        return valor_transicion.strip().lower() in ("true", "1", "si", "sí")
    if isinstance(valor_actual, int):
        try:
            return int(valor_transicion)
        except ValueError:
            return valor_transicion
    if isinstance(valor_actual, float):
        try:
            return float(valor_transicion)
        except ValueError:
            return valor_transicion
    return valor_transicion


def evaluar_operador(operador, valor_actual, valor_transicion):
    """Usado únicamente por `EstrategiaCondicion.ejecutar()`
    (`apps/workflows/estrategias.py`) — el motor nunca evalúa una
    condición por sí mismo (RN-021, corrección aprobada)."""
    funcion = OPERADORES[operador]
    try:
        return bool(funcion(valor_actual, _coercionar(valor_actual, valor_transicion)))
    except TypeError:
        # Comparación entre valores incompatibles (ej. MAYOR_QUE con
        # variable inexistente=None) — no satisface la condición, no es un
        # error del sistema (mismo criterio que
        # `apps.catalogo.reglas.EspecificacionRegla.es_satisfecha_por`).
        return False
