"""Efectos configurados al LLEGAR a un bloque — Fase 2 del modelo configurable de procesos.

El motor (`apps.workflows.motor._ejecutar_etapa`) llama a `aplicar_al_entrar` una sola vez por ejecución de
un bloque, ANTES de su Strategy y bajo el lock que ya tomó. El motor no sabe qué hace cada efecto: solo
delega aquí, igual que delega el comportamiento de cada tipo en `ESTRATEGIAS_POR_TIPO` (RN-021).

Efectos disponibles (claves de `BloqueOperativo.configuracion`, cualquier tipo de bloque):

- `cierra_alcance`: lista de claves de colección del Ticket. Al llegar al bloque, el alcance de esas
  colecciones se cierra si seguía abierto (`apps.tickets.parrilla.cerrar_alcance_por_flujo`, actor
  Sistema). Una colección ya cerrada o que el Ticket no tiene se ignora: el efecto es idempotente.

Un error inesperado aquí lo registra el motor como error técnico de la ejecución, igual que una Strategy.
"""

from apps.workflows.variables import ResolutorVariables


def claves_de_alcance(configuracion):
    """Las claves de colección cuyo alcance cierra el bloque (lista sin repetidos, en orden)."""
    claves = (configuracion or {}).get("cierra_alcance") or []
    if not isinstance(claves, list):
        return []
    vistas = []
    for clave in claves:
        if isinstance(clave, str) and clave and clave not in vistas:
            vistas.append(clave)
    return vistas


def aplicar_al_entrar(instancia, instancia_etapa):
    bloque = instancia_etapa.bloque_operativo
    if bloque is None:
        return
    claves = claves_de_alcance(bloque.configuracion)
    if not claves:
        return
    ticket = ResolutorVariables(instancia).ticket
    if ticket is None:
        return
    from apps.tickets.parrilla import cerrar_alcance_por_flujo

    cerrar_alcance_por_flujo(ticket, claves, bloque=bloque)


def al_terminar_subinstancia(instancia):
    """Fase 3 — un recorrido de ítem terminó COMPLETADO (no excluido ni rechazado): el ítem ingresa a las gacetas
    que reciben ítems «al terminar su recorrido». Idempotente y silencioso si no cumple sus requisitos."""
    if instancia.elemento_id is None or (instancia.contexto or {}).get("resultado_recorrido"):
        return
    from apps.gacetas.operaciones import ingresar_automaticamente

    ingresar_automaticamente(instancia.elemento, None, ingreso="AL_TERMINAR_RECORRIDO")
