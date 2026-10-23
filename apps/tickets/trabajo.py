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
from apps.tickets.autorizacion import puede_editar_parrilla, puede_escribir_entregables_finales
from apps.tickets.models import Ticket
from apps.workflows.models import (
    EsquemaAprobacionWorkflow,
    InstanciaEtapa,
    InstanciaWorkflow,
    TareaWorkflow,
)

TIPOS_BLOQUE = {
    "ACTIVIDAD": "Actividad", "ENTREGABLE": "Entregable", "APROBACION": "Aprobación", "DECISION": "Decisión",
    "ESPERA": "Espera", "TAREAS_ELEMENTOS": "Tareas por elemento",
    "GRUPO_ACTIVIDADES": "Actividades en paralelo", "RECORRIDO_ITEMS": "Recorrido por ítem", "GACETA": "Gaceta",
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
    if instancia.estado == InstanciaWorkflow.Estado.CANCELADA:
        # 4.G1.5: el Ticket se canceló; sus etapas quedan como historia, no como trabajo por hacer.
        return {"trabajo_actual": None, "trabajo_interno_en_curso": False}
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
        elif motivo == InstanciaEtapa.MotivoEspera.ELEMENTOS:
            from apps.tickets import tareas_elemento

            correccion = tareas_elemento.correccion_pendiente(ultima)
            if correccion:
                # 4.G2E: no es un error técnico: falta información por corregir y un reintento explícito.
                trabajo["correccion"] = correccion
                trabajo["pendiente"] = "Hay información por corregir antes de generar las tareas por elemento."
            else:
                progreso = tareas_elemento.progreso_de(ticket)
                trabajo["pendiente"] = (
                    f"Van {progreso.completadas} de {progreso.total} tareas por elemento completadas."
                )
        elif motivo in (InstanciaEtapa.MotivoEspera.RECORRIDO, InstanciaEtapa.MotivoEspera.GRUPO):
            # Fase 2: la instancia principal espera a sus subinstancias (detalle en «Recorridos»).
            from apps.workflows import recorridos

            avance = recorridos.progreso(ultima)
            partes = [f"{avance.en_curso} en curso"]
            if avance.con_error:
                partes.append(f"{avance.con_error} detenido(s)")
            if avance.pendiente_alcance:
                partes.append("esperando el cierre del alcance")
            trabajo["pendiente"] = f"{avance.total - avance.en_curso - avance.con_error} de {avance.total} terminados · " + ", ".join(partes) + "."
        elif motivo == InstanciaEtapa.MotivoEspera.GACETA:
            # Fase 3: el flujo espera la resolución de una gaceta (ver «Gacetas»).
            datos = ultima.resultado or {}
            trabajo["pendiente"] = (
                f"Esperando la resolución de la gaceta: {datos.get('aprobados', 0)} aprobado(s), "
                f"{datos.get('en_revision', 0)} en revisión, {datos.get('en_correccion', 0)} en corrección."
            )
        else:
            trabajo["pendiente"] = "En espera programada."
    return {"trabajo_actual": trabajo, "trabajo_interno_en_curso": en_curso}


# --- Fase 2: recorridos por ítem / actividades en paralelo y datos de inicio --------------------------------

ESTADO_RECORRIDO = {
    "EN_EJECUCION": "En curso", "EN_ESPERA": "En curso", "ERROR": "Detenido",
    "COMPLETADO": "Completado", "EXCLUIDO": "Excluido", "RECHAZADO": "Rechazado", "RETIRADO": "Retirado",
    "CANCELADO": "Cancelado",
}


def _responsable_de(tarea=None, participaciones=()):
    if tarea is not None:
        if tarea.usuario_responsable_id:
            return tarea.usuario_responsable.get_full_name().strip() or tarea.usuario_responsable.get_username()
        return tarea.equipo_responsable.nombre if tarea.equipo_responsable_id else "Sin asignar"
    nombres = []
    for aprobacion in participaciones:
        if aprobacion.estado != "PENDIENTE":
            continue
        if aprobacion.aprobador_usuario_id:
            nombres.append(aprobacion.aprobador_usuario.get_full_name().strip() or aprobacion.aprobador_usuario.get_username())
        elif aprobacion.aprobador_equipo_id:
            nombres.append(aprobacion.aprobador_equipo.nombre)
    return ", ".join(nombres)


def _fila_de_recorrido(usuario, hija, puede_reintentar):
    from apps.aprobaciones.autorizacion import puede_consultar_aprobacion
    from apps.tareas.autorizacion import puede_consultar_tarea
    from apps.workflows import recorridos

    ultima = hija.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    resultado = recorridos.resultado_de(hija)
    fila = {
        "instancia": hija,
        "elemento": hija.elemento,
        "etiqueta": (
            f"{hija.elemento.parrilla.nombre_elemento or 'Ítem'} #{hija.elemento.numero}" if hija.elemento_id
            else (hija.bloque_rama.nombre if hija.bloque_rama_id else f"#{hija.pk}")
        ),
        "estado": ESTADO_RECORRIDO.get(resultado, resultado),
        "en_curso": hija.estado in recorridos.ESTADOS_EN_CURSO,
        "con_error": hija.estado == InstanciaWorkflow.Estado.ERROR,
        "paso": ultima.bloque_operativo.nombre if ultima is not None and ultima.bloque_operativo_id else "",
        "error": (ultima.error or {}).get("mensaje", "") if ultima is not None and ultima.error else "",
        "responsable": "",
        "tarea": None,
        "aprobacion": None,
        "puede_reintentar": puede_reintentar and hija.estado == InstanciaWorkflow.Estado.ERROR,
    }
    if ultima is not None and ultima.estado == InstanciaEtapa.Estado.EN_ESPERA:
        vinculo = TareaWorkflow.objects.filter(instancia_etapa=ultima).select_related(
            "tarea__usuario_responsable", "tarea__equipo_responsable"
        ).first()
        if vinculo is not None:
            fila["responsable"] = _responsable_de(tarea=vinculo.tarea)
            fila["tarea"] = vinculo.tarea if puede_consultar_tarea(usuario, vinculo.tarea) else None
        esquema = EsquemaAprobacionWorkflow.objects.filter(instancia_etapa=ultima).select_related("esquema").first()
        if esquema is not None:
            participaciones = list(
                esquema.esquema.participaciones.select_related("aprobador_usuario", "aprobador_equipo").order_by("orden")
            )
            fila["responsable"] = _responsable_de(participaciones=participaciones)
            fila["aprobacion"] = next((a for a in participaciones if puede_consultar_aprobacion(usuario, a)), None)
    return fila


def recorridos_del_ticket(usuario, ticket):
    """Fase 2 — cada contenedor (recorrido por ítem / actividades en paralelo) que el flujo del Ticket ejecutó,
    del más reciente al más antiguo, con una fila por subinstancia: qué ítem o actividad es, en qué paso va,
    su estado o resultado, quién la tiene, el acceso a su tarea o aprobación (si `usuario` puede verlas) y si
    se puede reintentar (solo un recorrido detenido y solo quien edita los elementos). Solo LEE."""
    if ticket.instancia_workflow_id is None:
        return []
    from apps.workflows import recorridos

    ejecuciones = (
        InstanciaEtapa.objects.filter(
            instancia_workflow_id=ticket.instancia_workflow_id,
            bloque_operativo__tipo__in=(recorridos.TIPO_GRUPO, recorridos.TIPO_RECORRIDO),
        ).select_related("bloque_operativo").order_by("-orden")
    )
    puede_reintentar = puede_editar_parrilla(usuario, ticket)
    resultado = []
    for ejecucion in ejecuciones:
        filas = [_fila_de_recorrido(usuario, hija, puede_reintentar) for hija in recorridos.subinstancias_de(ejecucion)]
        resultado.append({
            "ejecucion": ejecucion,
            "bloque": ejecucion.bloque_operativo,
            "tipo": TIPOS_BLOQUE.get(ejecucion.bloque_operativo.tipo, ""),
            "vigente": ejecucion.estado == InstanciaEtapa.Estado.EN_ESPERA,
            "progreso": recorridos.progreso(ejecucion),
            "filas": filas,
        })
    return resultado


def datos_inicio_del_ticket(usuario, ticket, *, errores=None, valores_envio=None):
    """Fase 2 — la sección «Datos de inicio» de un ticket cuyos datos se diligencian en la ejecución: si ya
    están completos (cuándo y quién), qué falta, si `usuario` puede diligenciarlos y los campos del MISMO
    formulario congelado (`solicitud.construir_items`). `None` si el ticket completó sus datos al iniciar."""
    from apps.tickets import datos_inicio, solicitud
    from apps.tickets.autorizacion import puede_diligenciar_datos_inicio

    if not datos_inicio.requiere_diligenciamiento(ticket):
        return None
    respuesta = ticket.respuesta_formulario
    puede = puede_diligenciar_datos_inicio(usuario, ticket)
    return {
        "completo": respuesta.completado_en is not None,
        "completado_en": respuesta.completado_en,
        "completado_por": respuesta.completado_por,
        "puede_diligenciar": puede,
        "pendientes": datos_inicio.pendientes_de(ticket) if respuesta.completado_en is None else [],
        "items": solicitud.construir_items(ticket, errores=errores, valores_envio=valores_envio) if puede else [],
    }


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


def entregables_anotados(ticket):
    """Los entregables del Ticket con lo que necesitan las pantallas (`_entregable_item.html`): archivos
    vigentes y si el flujo ya permite escribirlos. Compartido por el detalle y el espacio de trabajo."""
    entregables = list(ticket.entregables.prefetch_related("archivos"))
    for entregable in entregables:
        entregable.archivos_vigentes = [a for a in entregable.archivos.all() if a.retirado_en is None]
        entregable.operable_ahora = entregable_operable_ahora(ticket, entregable)
    return entregables


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
        if situacion == ACTUAL and bloque.tipo == "TAREAS_ELEMENTOS":
            from apps.tickets import tareas_elemento

            detalle = (
                "Información por corregir" if tareas_elemento.correccion_pendiente(ejecucion)
                else "Esperando que se completen las tareas"
            )
        elif situacion == COMPLETADO and bloque.tipo == "DECISION":
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
    # 4.G2D2: tareas generadas por los elementos del Ticket (totales por estado y acceso a cada una).
    from apps.tickets import tareas_elemento

    contexto["tareas_elemento"] = tareas_elemento.resumen_para_ticket(usuario, ticket)
    contexto["es_proceso"] = False
    if ticket.tipo == Ticket.Tipo.PROCESO:
        _contexto_proceso(usuario, ticket, contexto)
    # Fase 2: recorridos por ítem / actividades en paralelo y datos de inicio en la ejecución.
    contexto["recorridos"] = recorridos_del_ticket(usuario, ticket)
    contexto["datos_inicio"] = datos_inicio_del_ticket(usuario, ticket)
    contexto.update(gacetas_y_cierre(usuario, ticket))
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
    elif trabajo["tipo_codigo"] == "TAREAS_ELEMENTOS":
        # 4.G2E: si falta información corregible se explica qué y se ofrece el reintento explícito (solo a
        # quien edita los elementos); la operación vuelve a autorizar y a validar bajo lock.
        panel["correccion"] = trabajo.get("correccion")
        panel["puede_reintentar"] = bool(trabajo.get("correccion")) and puede_editar_parrilla(usuario, ticket)
    contexto["panel"] = panel
    return contexto


# --- Ticket de PROCESO (4.G2E) --------------------------------------------------------------------


def _contexto_proceso(usuario, ticket, contexto):
    """Lo que añade el espacio de trabajo de un PROCESO a la composición común: elementos con su estado de
    revisión, entregables y revisiones, y la línea de tiempo. Solo LEE: cada acción es la operación de
    dominio de siempre (editar/retirar un elemento, revisar, completar una tarea, reintentar)."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets import revision_elementos as revision_ops

    entregables = entregables_anotados(ticket)
    conjuntos = [e for e in entregables if e.tipo == "ELEMENTOS"]
    estados = {}
    for conjunto in conjuntos:
        evaluados, resumen = revision_ops.evaluar(conjunto)
        conjunto.resumen_vigente = resumen
        conjunto.motivo_no_revisable = revision_ops.motivo_no_revisable(conjunto)
        estados[conjunto.pk] = {s.elemento.pk: s for s in evaluados}

    parrilla = parrilla_ops.tabla(ticket, usuario)
    if parrilla is not None:
        for fila in parrilla["filas"]:
            revisiones = []
            for conjunto in conjuntos:
                estado = estados[conjunto.pk].get(fila["elemento"].pk)
                if estado is None:
                    continue
                devuelta = estado.estado == revision_ops.Estado.DEVUELTA and estado.decision is not None
                revisiones.append({
                    "entregable": conjunto, "estado": estado.estado, "texto": estado.estado.label,
                    "observacion": estado.decision.observacion if devuelta else "",
                    "cambio": estado.cambio_desde_la_decision,
                })
            fila["revisiones"] = revisiones
        parrilla["con_revision"] = bool(conjuntos)
    # Fase 1: las demás colecciones del Ticket (la revisión por conjunto solo conoce la por defecto).
    adicionales = [
        parrilla_ops.tabla(ticket, usuario, parrilla=otra) for otra in parrilla_ops.parrillas_de(ticket)[1:]
    ]
    todas = ([parrilla] if parrilla else []) + adicionales

    from apps.tickets import tareas_elemento as tareas_ops

    tareas = contexto.get("tareas_elemento")
    contexto.update({
        "es_proceso": True,
        "tareas_configuradas": tareas_ops.configuracion_de(ticket) is not None,
        "parrilla": parrilla,
        "colecciones_adicionales": adicionales,
        "entregables": entregables,
        "conjuntos": conjuntos,
        "puede_escribir_entregables": puede_escribir_entregables_finales(usuario, ticket),
        "historial": ticket.historial.select_related("actor").all(),
        "resumen_proceso": {
            "elementos": sum(len(t["filas"]) for t in todas),
            "incompletos": sum(t["n_incompletas"] for t in todas),
            "retirados": sum(t["n_retirados"] for t in todas),
            "tiene_elementos": bool(todas),
            "tareas": tareas["progreso"] if tareas else None,
        },
    })
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


def gacetas_y_cierre(usuario, ticket):
    """Fase 3 — gacetas del ticket (resumen y acceso) y, en un Proceso, sus condiciones de cierre evaluadas
    (`apps.tickets.finalizacion`). Solo lectura."""
    from apps.gacetas.consultas import del_ticket
    from apps.tickets import finalizacion

    return {
        "gacetas_ticket": del_ticket(usuario, ticket),
        "condiciones_cierre": (
            finalizacion.evaluar(ticket)
            if ticket.tipo == Ticket.Tipo.PROCESO and ticket.estado in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)
            else []
        ),
    }
