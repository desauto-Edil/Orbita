"""Experiencia operativa de TRABAJO (4.F3) — composición de lectura.

Nada de aquí cambia estado ni decide avance: el motor (`apps.workflows.motor`) es la única autoridad
de en qué fase y bloque va un Ticket, y cada acción (completar una Tarea, escribir un entregable,
decidir una aprobación) sigue siendo la operación de dominio de siempre, que revalida permisos y
estado. Este módulo solo LEE `InstanciaWorkflow` / `InstanciaEtapa` / `FaseWorkflow` /
`BloqueOperativo` y las traduce a lo que necesita la pantalla; no existe un «estado de frontend»
paralelo. La disponibilidad de una acción sale de la autorización existente del dominio
(`apps.tareas.autorizacion`, `puede_escribir_entregables_finales`, `apps.aprobaciones.autorizacion`).

Protección de fases futuras: un bloque que el motor aún no alcanzó no tiene Tarea ni Aprobación (se
crean al llegar), así que no hay nada que operar; el único recurso que existe desde el borrador es el
`EntregableTicket`, y `entregable_operable_ahora` impide escribirlo antes de que el flujo llegue a su
bloque (la regla vive aquí para que vistas y pruebas la compartan).
"""

from apps.tickets import seguimiento
from apps.tickets.autorizacion import puede_escribir_entregables_finales
from apps.tickets.models import Ticket
from apps.workflows.models import (
    EsquemaAprobacionWorkflow,
    InstanciaEtapa,
    InstanciaWorkflow,
    TareaWorkflow,
)

TIPOS_BLOQUE = {
    "ACTIVIDAD": "Actividad", "ENTREGABLE": "Entregable", "APROBACION": "Aprobación", "DECISION": "Decisión",
    "ESPERA": "Espera",
}

# Estado de un bloque dentro de la fase actual (derivado de las ejecuciones reales).
COMPLETADO = "completado"
ACTUAL = "actual"
PENDIENTE = "pendiente"
CON_ERROR = "con_error"


def tickets_a_cargo(usuario):
    """Tickets cuya atención está bajo responsabilidad INDIVIDUAL de `usuario`. «Mi trabajo» es esto;
    haber solicitado un ticket no lo convierte en trabajo."""
    return (
        Ticket.objects.filter(usuario_responsable=usuario, estado=Ticket.Estado.EN_ATENCION)
        .select_related("detalle_servicio__servicio", "instancia_workflow__workflow_version", "solicitante")
        .order_by("fecha_objetivo_vigente", "radicado_en", "pk")
    )


def contexto_trabajo_actual(usuario, ticket):
    """4.E2 — qué trabajo tiene el Ticket ahora y dónde actuar: el bloque vigente de su ejecución,
    su situación y los enlaces a la Tarea / Aprobación / Entregable que corresponda (solo a quien
    puede consultarlos). `trabajo_interno_en_curso` es la misma regla que protege resolver/entregar."""
    if ticket.instancia_workflow_id is None:
        return {"trabajo_actual": None, "trabajo_interno_en_curso": False}
    from apps.aprobaciones.autorizacion import puede_aprobar, puede_consultar_aprobacion
    from apps.tareas.autorizacion import puede_consultar_tarea

    instancia = ticket.instancia_workflow
    ultima = (
        instancia.ejecuciones_etapa.select_related("bloque_operativo", "etapa", "fase_workflow")
        .order_by("-orden")
        .first()
    )
    en_curso = instancia.estado in (InstanciaWorkflow.Estado.EN_EJECUCION, InstanciaWorkflow.Estado.EN_ESPERA)
    if ultima is None:
        return {"trabajo_actual": None, "trabajo_interno_en_curso": en_curso}

    bloque = ultima.bloque_operativo
    definicion = bloque if bloque is not None else ultima.etapa
    trabajo = {
        "fase": ultima.fase_workflow.nombre if ultima.fase_workflow_id else "",
        "nombre": definicion.nombre,
        "tipo": TIPOS_BLOQUE.get(bloque.tipo, bloque.get_tipo_display())
        if bloque is not None
        else definicion.get_tipo_display(),
        "tipo_codigo": bloque.tipo if bloque is not None else "",
        "descripcion": getattr(definicion, "descripcion", "") or "",
        "terminado": instancia.estado == InstanciaWorkflow.Estado.COMPLETADA,
        "con_error": instancia.estado == InstanciaWorkflow.Estado.ERROR,
        "pendiente": "",
        "tarea": None,
        "aprobacion": None,
        "entregable": None,
    }
    if ultima.estado == InstanciaEtapa.Estado.EN_ESPERA:
        motivo = ultima.motivo_espera
        if motivo == InstanciaEtapa.MotivoEspera.TAREA:
            trabajo["pendiente"] = "Hay una actividad por completar."
            vinculo = TareaWorkflow.objects.filter(instancia_etapa=ultima).select_related("tarea").first()
            if vinculo is not None:
                trabajo["tarea_vinculada"] = vinculo.tarea
                if puede_consultar_tarea(usuario, vinculo.tarea):
                    trabajo["tarea"] = vinculo.tarea
        elif motivo == InstanciaEtapa.MotivoEspera.APROBACION:
            trabajo["pendiente"] = "Está pendiente de aprobación."
            vinculo = (
                EsquemaAprobacionWorkflow.objects.filter(instancia_etapa=ultima).select_related("esquema").first()
            )
            if vinculo is not None:
                participaciones = list(vinculo.esquema.participaciones.select_related("aprobador_usuario", "aprobador_equipo"))
                trabajo["participaciones"] = participaciones
                propias = [a for a in participaciones if puede_consultar_aprobacion(usuario, a)]
                propias.sort(key=lambda a: (not puede_aprobar(usuario, a), a.orden))
                trabajo["aprobacion"] = propias[0] if propias else None
        elif motivo == InstanciaEtapa.MotivoEspera.ENTREGABLE:
            entregable_id = (ultima.resultado or {}).get("entregable_id")
            entregable = ticket.entregables.filter(pk=entregable_id).first() if entregable_id else None
            trabajo["entregable"] = entregable
            trabajo["pendiente"] = (
                f"Falta entregar «{entregable.nombre}»." if entregable is not None else "Falta un entregable."
            )
        else:
            trabajo["pendiente"] = "En espera programada."
    return {"trabajo_actual": trabajo, "trabajo_interno_en_curso": en_curso}


def entregable_operable_ahora(ticket, entregable):
    """¿Se puede escribir ESTE entregable ahora? Un entregable que un bloque ENTREGABLE del flujo
    referencia solo se completa cuando el motor llegó a ese bloque (no se adelanta trabajo de una fase
    futura ni se salta el flujo manipulando una URL). Los entregables que ningún bloque referencia
    (resultados libres para la entrega formal), los tickets sin flujo por fases y el flujo ya terminado
    no tienen esa restricción."""
    if ticket.instancia_workflow_id is None:
        return True
    instancia = ticket.instancia_workflow
    if instancia.estado == InstanciaWorkflow.Estado.COMPLETADA:
        return True
    configuracion = instancia.configuracion_ejecucion_version
    if configuracion is None:
        return True
    if not configuracion.bloques.filter(tipo="ENTREGABLE", definicion_entregable_id=entregable.definicion_id).exists():
        return True
    ultima = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    return bool(
        ultima is not None
        and ultima.estado == InstanciaEtapa.Estado.EN_ESPERA
        and ultima.motivo_espera == InstanciaEtapa.MotivoEspera.ENTREGABLE
        and ultima.bloque_operativo is not None
        and ultima.bloque_operativo.definicion_entregable_id == entregable.definicion_id
    )


def _pasada_actual(ejecuciones, fase_id):
    """Ejecuciones de la visita VIGENTE a la fase (las últimas consecutivas en esa fase): una fase que
    se vuelve a recorrer (p. ej. Revisión tras Ajustes) empieza de cero."""
    pasada = []
    for ejecucion in reversed(ejecuciones):
        if ejecucion.fase_workflow_id != fase_id:
            break
        pasada.append(ejecucion)
    pasada.reverse()
    return pasada


def _bloques_de_la_fase(instancia, ejecuciones, fase):
    configuracion = instancia.configuracion_ejecucion_version
    if configuracion is None or fase is None:
        return []
    ultima = ejecuciones[-1] if ejecuciones else None
    por_bloque = {}
    for ejecucion in _pasada_actual(ejecuciones, fase.pk):
        if ejecucion.bloque_operativo_id is not None:
            por_bloque[ejecucion.bloque_operativo_id] = ejecucion
    filas = []
    for bloque in configuracion.bloques.filter(fase=fase).order_by("orden", "pk"):
        ejecucion = por_bloque.get(bloque.pk)
        if ejecucion is None:
            situacion = PENDIENTE
        elif instancia.estado == InstanciaWorkflow.Estado.ERROR and ejecucion.pk == ultima.pk:
            situacion = CON_ERROR
        elif ejecucion.estado == InstanciaEtapa.Estado.COMPLETADA:
            situacion = COMPLETADO
        elif ejecucion.pk == ultima.pk:
            situacion = ACTUAL
        else:
            situacion = PENDIENTE
        detalle = ""
        if situacion == COMPLETADO and bloque.tipo == "DECISION":
            detalle = "Condición evaluada"
        elif situacion == COMPLETADO and bloque.tipo == "APROBACION" and ejecucion.transicion_bloque_tomada_id:
            detalle = ejecucion.transicion_bloque_tomada.get_resultado_aprobacion_display() or ""
        filas.append(
            {
                "bloque": bloque,
                "nombre": bloque.nombre,
                "tipo": TIPOS_BLOQUE.get(bloque.tipo, bloque.tipo),
                "tipo_codigo": bloque.tipo,
                "situacion": situacion,
                "detalle": detalle,
            }
        )
    return filas


def _panel_actividad(usuario, trabajo, panel):
    from apps.tareas.autorizacion import (
        es_responsable_actual,
        puede_completar_tarea,
        puede_iniciar_tarea,
        puede_tomar_tarea,
    )

    tarea = trabajo.get("tarea_vinculada")
    if tarea is None:
        return
    panel["tarea"] = tarea
    panel["tarea_responsable"] = (
        tarea.usuario_responsable.get_full_name().strip() or tarea.usuario_responsable.get_username()
        if tarea.usuario_responsable_id
        else (tarea.equipo_responsable.nombre if tarea.equipo_responsable_id else "")
    )
    panel["es_mia"] = es_responsable_actual(usuario, tarea)
    panel["puede_tomar_tarea"] = puede_tomar_tarea(usuario, tarea)
    panel["puede_iniciar_tarea"] = puede_iniciar_tarea(usuario, tarea)
    panel["puede_completar_tarea"] = puede_completar_tarea(usuario, tarea)


def espacio(usuario, ticket):
    """Todo lo que pinta la experiencia operativa de un Ticket: encabezado de flujo, fases, bloques de la
    fase actual y el panel de lo que hay que hacer ahora."""
    contexto = {"hay_flujo": ticket.instancia_workflow_id is not None, "progreso": None, "bloques": [], "panel": None}
    contexto.update(contexto_trabajo_actual(usuario, ticket))
    if not contexto["hay_flujo"]:
        return contexto
    instancia = ticket.instancia_workflow
    contexto["progreso"] = seguimiento.progreso(ticket)
    ejecuciones = list(
        instancia.ejecuciones_etapa.select_related(
            "bloque_operativo", "fase_workflow", "transicion_bloque_tomada"
        ).order_by("orden")
    )
    ultima = ejecuciones[-1] if ejecuciones else None
    fase = ultima.fase_workflow if ultima is not None else None
    contexto["bloques"] = _bloques_de_la_fase(instancia, ejecuciones, fase)
    contexto["flujo_terminado"] = instancia.estado == InstanciaWorkflow.Estado.COMPLETADA
    contexto["flujo_con_error"] = instancia.estado == InstanciaWorkflow.Estado.ERROR

    trabajo = contexto["trabajo_actual"]
    if trabajo is None or trabajo["terminado"] or trabajo["con_error"]:
        return contexto
    panel = {"tipo": trabajo["tipo_codigo"], "nombre": trabajo["nombre"], "descripcion": trabajo["descripcion"]}
    if trabajo["tipo_codigo"] == "ACTIVIDAD":
        _panel_actividad(usuario, trabajo, panel)
    elif trabajo["tipo_codigo"] == "ENTREGABLE" and trabajo["entregable"] is not None:
        entregable = trabajo["entregable"]
        entregable.archivos_vigentes = [a for a in entregable.archivos.all() if a.retirado_en is None]
        entregable.operable_ahora = entregable_operable_ahora(ticket, entregable)
        panel["entregable"] = entregable
        panel["puede_escribir"] = puede_escribir_entregables_finales(usuario, ticket) and entregable_operable_ahora(
            ticket, entregable
        )
    elif trabajo["tipo_codigo"] == "APROBACION":
        panel["participaciones"] = trabajo.get("participaciones", [])
        panel["aprobacion"] = trabajo["aprobacion"]
    contexto["panel"] = panel
    return contexto


def tarjeta(usuario, ticket):
    """Resumen de un Ticket para «Mi trabajo» y el Inicio: fase, bloque actual y progreso básico."""
    datos = contexto_trabajo_actual(usuario, ticket)
    trabajo = datos["trabajo_actual"]
    progreso = seguimiento.progreso(ticket) if ticket.instancia_workflow_id else None
    completadas = total = porcentaje = 0
    if progreso:
        total = len(progreso["fases"])
        completadas = sum(1 for fila in progreso["fases"] if fila["situacion"] in ("completada", "no_aplico"))
        porcentaje = int(100 * completadas / total) if total else 0
    return {
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "fase": (progreso or {}).get("actual") or (trabajo["fase"] if trabajo else ""),
        "bloque": trabajo["nombre"] if trabajo and not trabajo["terminado"] else "",
        "bloque_tipo": trabajo["tipo"] if trabajo and not trabajo["terminado"] else "",
        "pendiente": trabajo["pendiente"] if trabajo else "",
        "terminado": bool(trabajo and trabajo["terminado"]),
        "fases_total": total,
        "fases_completadas": completadas,
        "porcentaje": porcentaje,
    }
