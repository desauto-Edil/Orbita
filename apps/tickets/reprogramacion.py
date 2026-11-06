"""Bloque 5.4 — reprogramación de fechas por ítem.

Una ACTIVIDAD de un recorrido por ítem puede tomar su fecha prevista y/o límite de una columna del ítem
(`fecha_prevista_campo` / `fecha_limite_campo` = `item.<clave>`, `apps.catalogo.actividades`). La Tarea copia esa
fecha al crearse; si después cambia la columna, `sincronizar_fechas` mueve la fecha de las Tareas ABIERTAS de ESE
ítem que salieron de esa columna, con la misma regla de conversión (`actividades.fecha_desde_valor`: una FECHA es
el final del día en la hora local; una FECHA_HORA, tal cual; vacío = sin fecha).

- Qué Tareas: las de `TareaWorkflow` cuya ejecución es del recorrido de ESTE ítem y cuyo bloque (configuración
  CONGELADA de la instancia, nunca la del Studio) referencia la columna cambiada. Los plazos relativos, las
  fechas de `formulario.*`, las Tareas por elemento y los demás ítems no se tocan.
- Antes de que exista la Tarea no hay nada que mover: al llegar el flujo, la Strategy lee el valor vigente.
- Bloque 5.5: VACIAR la columna mientras una Tarea abierta toma de ella su fecha se rechaza (toda la edición, sin
  cambios parciales): la fecha se cambia, no se quita. Sin Tarea abierta (aún no existe, o ya se cerró) vaciarla
  sigue permitido.
- Se llama desde `parrilla.actualizar_valores`, que ya autorizó la edición y tiene bloqueados la instancia, el
  Ticket, la colección y el ítem; la Tarea se bloquea después (orden del proyecto: colección → tareas).
  Completar la Tarea (`workflows.integracion.completar_tarea_workflow`) también bloquea primero la instancia
  PRINCIPAL (→ hija → Tarea), así que editar el ítem y completar su Tarea se serializan en esa primera compuerta
  y nunca se esperan en sentido contrario: lo que llega segundo ve la Tarea ya cerrada (no se reprograma) o la
  fecha ya cambiada (se completa con ella).
- La escritura es `tareas.operaciones.reprogramar_tarea` (historial REPROGRAMADA y auditoría con valor anterior
  y nuevo; idempotente; nunca cambia una Tarea completada o cancelada).
"""

from django.core.exceptions import ValidationError

from apps.catalogo import actividades

REFERENCIAS = (("fecha_prevista", "fecha_prevista_campo"), ("fecha_limite", "fecha_limite_campo"))


def _tareas_abiertas_del_item(elemento):
    from apps.catalogo.models import BloqueOperativo
    from apps.tareas.models import Tarea
    from apps.workflows.models import TareaWorkflow

    return (
        TareaWorkflow.objects.filter(
            instancia_etapa__instancia_workflow__elemento_id=elemento.pk,
            instancia_etapa__bloque_operativo__tipo=BloqueOperativo.Tipo.ACTIVIDAD,
        )
        .exclude(tarea__estado__in=Tarea.ESTADOS_CERRADOS)
        .select_related("tarea", "instancia_etapa__bloque_operativo")
        .order_by("tarea_id")
    )


def sincronizar_fechas(elemento, actor, claves_cambiadas):
    """Reprograma las Tareas abiertas de `elemento` cuya fecha sale de alguna columna de `claves_cambiadas`.
    Devuelve cuántas Tareas cambiaron. Rechaza (`ValidationError`) que una de esas columnas haya quedado vacía:
    se comprueba antes de reprogramar ninguna, y quien llama deshace la edición completa."""
    from apps.tareas.operaciones import reprogramar_tarea
    from apps.tickets.parrilla import columnas_de, valores_de

    referencias = {f"item.{clave}": clave for clave in claves_cambiadas}
    if not referencias:
        return 0
    valores = None
    pendientes = []
    for vinculo in _tareas_abiertas_del_item(elemento):
        ampliacion, _errores = actividades.normalizar(vinculo.instancia_etapa.bloque_operativo.configuracion)
        fechas = {}
        for campo, llave in REFERENCIAS:
            clave = referencias.get(ampliacion[llave])
            if clave is None:
                continue
            if valores is None:
                valores = valores_de(elemento)
            if valores.get(clave) in (None, ""):
                etiqueta = next((c.etiqueta for c in columnas_de(elemento.parrilla) if c.clave == clave), clave)
                raise ValidationError(
                    f"«{etiqueta}»: la tarea «{vinculo.tarea.titulo}» del ítem #{elemento.numero} toma su fecha de "
                    "esta columna y sigue abierta. Cambia la fecha en lugar de dejarla vacía."
                )
            fechas[campo] = actividades.fecha_desde_valor(valores[clave])
        if fechas:
            pendientes.append((vinculo.tarea, fechas))
    motivo = f"Cambió la fecha del ítem #{elemento.numero}."
    return sum(1 for tarea, fechas in pendientes if reprogramar_tarea(tarea, actor, motivo=motivo, **fechas))
