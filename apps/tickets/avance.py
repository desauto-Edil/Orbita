"""UX-4 — «Guardar y avanzar» en UNA transacción: lo que la persona escribió en el ítem de su actividad y el avance
de la actividad (completarla, que el flujo envía a aprobación o a revisión) ocurren juntos o no ocurren.

No hay regla nueva: compone operaciones que ya existen y que siguen autorizando y validando cada una lo suyo
(`parrilla.actualizar_valores` / `adjuntar_archivo_elemento` con la edición por tarea de 3.1;
`workflows.integracion.completar_tarea_workflow`, que exige las columnas requeridas de la actividad, la evidencia
y la lista de verificación; `gacetas.operaciones.reenviar_por_tarea` para una corrección de gaceta). «Guardar
borrador» sigue siendo solo `actualizar_valores` (no avanza).

Orden de locks: instancia principal (la de la ejecución del Ticket) → Ticket → colección → ítem → Tarea →
gaceta, el del proyecto: la instancia se bloquea ANTES de escribir el ítem, y cada operación vuelve a tomar sus
locks dentro de la misma transacción.
"""

from django.db import transaction

from apps.tareas.operaciones import completar_tarea


def _instancia_de(tarea):
    from apps.gacetas.models import RondaRevisionGaceta
    from apps.workflows.integracion import _instancia_id_de_tarea

    instancia_id = _instancia_id_de_tarea(tarea)
    ronda = RondaRevisionGaceta.objects.filter(tarea_correccion=tarea).select_related("participacion__gaceta__ticket").first()
    if instancia_id is None and ronda is not None:
        instancia_id = ronda.participacion.gaceta.ticket.instancia_workflow_id
    return instancia_id, ronda


@transaction.atomic
def guardar_y_avanzar(tarea, actor, *, elemento=None, valores=None, archivos=()):
    """Guarda `valores` (`{campo_id: valor}`) y `archivos` (`[(campo, archivo_subido)]`) en `elemento` y completa
    `tarea` por la vía que le corresponde. Si algo falla (un dato inválido, una columna exigida vacía, la
    evidencia o la verificación pendientes, falta de permiso), no queda nada guardado. Devuelve la Tarea."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.workflows.integracion import bloquear_instancia, completar_tarea_workflow

    instancia_id, ronda = _instancia_de(tarea)
    if instancia_id is not None:
        bloquear_instancia(instancia_id)
    if elemento is not None:
        if valores:
            parrilla_ops.actualizar_valores(elemento, actor, valores)
        for campo, archivo in archivos or ():
            parrilla_ops.adjuntar_archivo_elemento(elemento, campo, actor, archivo)
    if hasattr(tarea, "vinculo_workflow") or hasattr(tarea, "vinculo_elemento"):
        return completar_tarea_workflow(tarea, actor)
    if ronda is not None:
        from apps.gacetas.operaciones import reenviar_por_tarea

        reenviar_por_tarea(tarea, actor)
        tarea.refresh_from_db()
        return tarea
    return completar_tarea(tarea, actor)
