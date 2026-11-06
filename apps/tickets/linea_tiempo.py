"""Historial del ticket como línea de tiempo — representación compartida de LECTURA de TODO lo ocurrido.

No hay un registro nuevo ni se reconstruye nada: cada evento sale de una fila REAL que el dominio ya guarda, con
su actor (o Sistema cuando no hay persona) y su fecha. Fuentes, todas acotadas al ticket:

- `HistorialTicket` — radicación, toma, asignaciones, solicitudes de información, entregas, prórrogas, cierre…
- Ítems de sus colecciones — registro (`RegistroAuditoria` CREAR del ítem: el único lugar que guarda quién lo
  creó), retiro, versiones (`VersionElemento`), envío a su recorrido (subinstancia `InstanciaWorkflow` con su
  `iniciado_por`), decisiones de alcance (`DecisionAlcanceElemento`), revisión por conjunto (`RevisionElemento`) y
  cierre del alcance (`ParrillaTicket`).
- Flujo — cambio de fase del recorrido principal (`InstanciaEtapa.fase_workflow`) y su fin.
- Actividades — `HistorialTarea` de las tareas del flujo (`TareaWorkflow`), por ítem (`TareaElemento`) y de
  corrección de gaceta.
- Aprobaciones — cada decisión de `Aprobacion` de los esquemas del flujo (`EsquemaAprobacionWorkflow`) y de las
  rondas de gaceta, con su observación.
- Gacetas — apertura, cierre del periodo y resolución (`GacetaTicket`), ítems presentados, rondas nuevas,
  aceptaciones automáticas (`ParticipacionItemGaceta`) y observaciones con sus respuestas (`ObservacionGaceta`).

La auditoría transversal solo se lee para saber quién creó un ítem; el resto de `RegistroAuditoria` no se mezcla
(sería repetir lo mismo). Orden: del más reciente al más antiguo."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from apps.tickets.models import HistorialTicket

CAUSAS = {
    "ACEPTACION_SOLICITANTE": "Aceptado por el solicitante",
    "VENCIMIENTO_SIN_RESPUESTA": "Plazo vencido sin respuesta",
    "CIERRE_DIRECTO": "Al entregar",
    "POLITICA_SIN_APROBACION": "Automática, sin aprobación",
}

ACCIONES_TICKET = {
    "RADICADO": "radicó el ticket",
    "TOMADO": "tomó el ticket",
    "ASIGNADO": "asignó el ticket",
    "REASIGNADO": "reasignó el ticket",
    "INFORMACION_SOLICITADA": "solicitó información",
    "INFORMACION_RESPONDIDA": "respondió la solicitud de información",
    "RESUELTO": "resolvió el ticket",
    "CERRADO": "cerró el ticket",
    "CANCELADO": "canceló el ticket",
    "REABIERTO": "reabrió el ticket",
    "ENTREGADO": "entregó el resultado",
    "ENTREGA_OBSERVADA": "observó la entrega",
    "PRORROGA_SOLICITADA": "solicitó una prórroga",
    "PRORROGA_APROBADA": "aprobó la prórroga",
    "PRORROGA_RECHAZADA": "rechazó la prórroga",
    "PRORROGA_CANCELADA": "canceló la prórroga",
    "DIRECCIONADO": "direccionó el ticket",
    "ATENCION_INICIADA": "inició la atención",
    "DATOS_COMPLETADOS": "completó los datos de inicio",
}

ACCIONES_TAREA = {
    "TOMADA": "tomó la actividad",
    "ASIGNADA": "asignó la actividad",
    "REASIGNADA": "reasignó la actividad",
    "DELEGADA": "delegó la actividad",
    "INICIADA": "inició la actividad",
    "COMPLETADA": "completó la actividad",
    "CANCELADA": "canceló la actividad",
    "VERIFICACION": "marcó la verificación de",
    "REPROGRAMADA": "reprogramó la actividad",
}

ACCIONES_APROBACION = {
    "APROBADA": "aprobó",
    "RECHAZADA": "rechazó",
    "DEVUELTA": "devolvió",
    "EXCLUIDA": "excluyó",
}

DECISION_AUTOMATICA = {
    "APROBADA": "aceptó automáticamente",
    "RECHAZADA": "rechazó al resolver la gaceta",
    "EXCLUIDA": "excluyó al resolver la gaceta",
}

LARGO_DETALLE = 220

_CLAVES_PERSONA = ("usuario_id", "usuario_anterior_id")


def _nombre(usuario):
    return usuario.get_full_name() or usuario.get_username()


def _corto(texto):
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= LARGO_DETALLE else texto[: LARGO_DETALLE - 1] + "…"


def _evento(fecha, actor, accion, objeto="", detalle=()):
    return {
        "fecha": fecha,
        "actor": actor,
        "actor_nombre": _nombre(actor) if actor else "Sistema",
        "sistema": actor is None,
        "accion": accion,
        "objeto": objeto,
        "detalle": [parte for parte in detalle if parte],
    }


# --- 1. HistorialTicket -----------------------------------------------------------------------------------------

def _detalle_historial(evento, personas):
    datos = evento.datos or {}
    tipo = evento.tipo_evento
    partes = []
    if datos.get("causa") in CAUSAS:
        partes.append(CAUSAS[datos["causa"]])
    if tipo == HistorialTicket.TipoEvento.DIRECCIONADO and datos.get("destino_etiqueta"):
        texto = f"Destino: {datos['destino_etiqueta']}"
        if datos.get("responsable_etiqueta"):
            texto += f" · Responsable: {datos['responsable_etiqueta']}"
        if datos.get("es_predeterminado"):
            texto += " (destino de reserva)"
        partes.append(texto)
    if tipo == HistorialTicket.TipoEvento.RADICADO and datos.get("origen") == "PROGRAMACION":
        partes.append("Generado por programación" + (f" · {datos['etiqueta']}" if datos.get("etiqueta") else ""))
    if tipo in (HistorialTicket.TipoEvento.ASIGNADO, HistorialTicket.TipoEvento.REASIGNADO,
                HistorialTicket.TipoEvento.ATENCION_INICIADA):
        anterior = personas.get(datos.get("usuario_anterior_id"))
        nuevo = personas.get(datos.get("usuario_id"))
        if anterior and nuevo:
            partes.append(f"{anterior} → {nuevo}")
        elif nuevo:
            partes.append(f"A {nuevo}")
    if tipo in (HistorialTicket.TipoEvento.ENTREGADO, HistorialTicket.TipoEvento.ENTREGA_OBSERVADA) and datos.get("numero"):
        partes.append(f"Entrega {datos['numero']}")
    if datos.get("nueva_fecha"):
        partes.append(f"Nueva fecha: {str(datos['nueva_fecha'])[:10]}")
    if datos.get("motivo"):
        partes.append(_corto(datos["motivo"]))
    return partes


def _del_historial(ticket):
    filas = list(ticket.historial.select_related("actor"))
    ids = {
        (fila.datos or {}).get(clave)
        for fila in filas
        for clave in _CLAVES_PERSONA
        if isinstance((fila.datos or {}).get(clave), int)
    }
    personas = {u.pk: _nombre(u) for u in get_user_model().objects.filter(pk__in=ids)} if ids else {}
    return [
        _evento(
            fila.creado_en, fila.actor,
            ACCIONES_TICKET.get(fila.tipo_evento, fila.get_tipo_evento_display().lower()),
            detalle=_detalle_historial(fila, personas),
        )
        for fila in filas
    ]


# --- 2. Ítems de las colecciones ---------------------------------------------------------------------------------

def _items(ticket):
    """Ítems de todas las colecciones del ticket, con su nombre legible (primera columna de texto con valor)."""
    from apps.catalogo.models import Campo
    from apps.tickets.models import ElementoParrilla

    elementos = list(
        ElementoParrilla.objects.filter(parrilla__ticket=ticket)
        .select_related("parrilla", "retirado_por")
        .prefetch_related("valores__campo")
    )
    nombres = {}
    for elemento in elementos:
        nombre = f"{elemento.parrilla.nombre_elemento or 'Ítem'} #{elemento.numero}"
        textos = sorted(
            (v for v in elemento.valores.all()
             if v.campo.tipo in (Campo.TipoCampo.TEXTO, Campo.TipoCampo.TEXTO_LARGO) and v.valor),
            key=lambda v: (v.campo.orden, v.campo.pk),
        )
        if textos:
            texto = " ".join(str(textos[0].valor).split())
            nombre = texto if len(texto) <= 48 else texto[:45] + "…"
        nombres[elemento.pk] = nombre
    return elementos, nombres


def _de_items(ticket, elementos, nombres):
    from apps.core.models import RegistroAuditoria
    from apps.tickets.models import (
        DecisionAlcanceElemento,
        ElementoParrilla,
        ParrillaTicket,
        RevisionElemento,
        VersionElemento,
    )

    eventos = []
    ids = [e.pk for e in elementos]
    if ids:
        tipo = ContentType.objects.get_for_model(ElementoParrilla)
        creaciones = RegistroAuditoria.objects.filter(
            content_type=tipo, object_id__in=ids, accion=RegistroAuditoria.Accion.CREAR
        ).select_related("usuario")
        for registro in creaciones:
            eventos.append(_evento(registro.creado_en, registro.usuario, "registró", nombres.get(registro.object_id, "")))
        for elemento in elementos:
            if elemento.retirado_en:
                eventos.append(_evento(elemento.retirado_en, elemento.retirado_por, "retiró", nombres[elemento.pk]))
        # Las versiones que guarda el sistema (al presentar a una gaceta o abrir una ronda) ya aparecen como ese
        # evento; aquí solo las que guardó una persona.
        for version in VersionElemento.objects.filter(elemento_id__in=ids, creada_por__isnull=False).select_related("creada_por"):
            eventos.append(_evento(
                version.creada_en, version.creada_por, f"guardó la versión {version.numero} de",
                nombres.get(version.elemento_id, ""), [_corto(version.motivo)],
            ))
        for decision in DecisionAlcanceElemento.objects.filter(elemento_id__in=ids).select_related("decidido_por"):
            eventos.append(_evento(
                decision.decidido_en, decision.decidido_por,
                f"registró «{decision.get_tipo_display().lower()}» para", nombres.get(decision.elemento_id, ""),
                [_corto(decision.justificacion)],
            ))
        for revision in RevisionElemento.objects.filter(elemento_id__in=ids).select_related("revisor"):
            accion = "aprobó en la revisión del conjunto" if revision.decision == "APROBADA" else "devolvió en la revisión del conjunto"
            eventos.append(_evento(revision.decidido_en, revision.revisor, accion, nombres.get(revision.elemento_id, ""),
                                   [_corto(revision.observacion)]))
    for parrilla in ParrillaTicket.objects.filter(ticket=ticket, alcance_cerrado_en__isnull=False).select_related("alcance_cerrado_por"):
        eventos.append(_evento(parrilla.alcance_cerrado_en, parrilla.alcance_cerrado_por,
                               f"cerró el alcance de {parrilla.nombre_elemento or 'la colección'}"))
    return eventos


# --- 3. Flujo, actividades y aprobaciones -------------------------------------------------------------------------

def _instancias(ticket):
    from apps.workflows.models import InstanciaWorkflow

    principal = ticket.instancia_workflow
    if principal is None:
        return None, []
    hijas = list(
        InstanciaWorkflow.objects.filter(instancia_padre=principal).select_related(
            "iniciado_por", "bloque_rama", "ejecucion_padre__bloque_operativo"
        )
    )
    return principal, hijas


def _del_flujo(ticket, principal, hijas, nombres):
    from apps.workflows.models import InstanciaEtapa

    eventos = []
    if principal is None:
        return eventos
    fase_anterior = None
    for ejecucion in (
        InstanciaEtapa.objects.filter(instancia_workflow=principal, fase_workflow__isnull=False, iniciada_en__isnull=False)
        .select_related("fase_workflow")
        .order_by("iniciada_en", "pk")
    ):
        if ejecucion.fase_workflow_id != fase_anterior:
            eventos.append(_evento(ejecucion.iniciada_en, None, "movió el proceso a la fase", ejecucion.fase_workflow.nombre))
            fase_anterior = ejecucion.fase_workflow_id
    if principal.finalizada_en:
        eventos.append(_evento(principal.finalizada_en, None, "completó el flujo del proceso"))
    for hija in hijas:
        if hija.elemento_id is None:
            continue
        bloque = hija.bloque_rama or (hija.ejecucion_padre.bloque_operativo if hija.ejecucion_padre else None)
        eventos.append(_evento(
            hija.creado_en, hija.iniciado_por, "envió a su recorrido", nombres.get(hija.elemento_id, ""),
            [f"Paso: {bloque.nombre}" if bloque is not None else ""],
        ))
    return eventos


def _de_actividades(ticket, instancias_ids, elementos_ids, rondas):
    from apps.tareas.models import HistorialTarea
    from apps.tickets.models import TareaElemento
    from apps.workflows.models import TareaWorkflow

    tareas = set(TareaWorkflow.objects.filter(instancia_etapa__instancia_workflow_id__in=instancias_ids).values_list("tarea_id", flat=True))
    tareas |= set(TareaElemento.objects.filter(elemento_id__in=elementos_ids).values_list("tarea_id", flat=True))
    tareas |= {ronda.tarea_correccion_id for ronda in rondas if ronda.tarea_correccion_id}
    if not tareas:
        return []
    from apps.tareas.models import Tarea

    eventos = []
    for tarea in Tarea.objects.filter(pk__in=tareas).select_related("creada_por", "usuario_responsable", "equipo_responsable"):
        para = tarea.usuario_responsable or tarea.equipo_responsable
        eventos.append(_evento(tarea.creado_en, tarea.creada_por, "creó la actividad", tarea.titulo,
                               [f"Para: {_nombre(para) if hasattr(para, 'get_username') else para}" if para else ""]))
    for fila in HistorialTarea.objects.filter(tarea_id__in=tareas).select_related("actor", "tarea"):
        accion = ACCIONES_TAREA.get(fila.tipo_evento, fila.get_tipo_evento_display().lower())
        eventos.append(_evento(fila.creado_en, fila.actor, accion, fila.tarea.titulo,
                               [_corto((fila.datos or {}).get("motivo"))]))
    return eventos


def _de_aprobaciones(instancias_ids, nombres, rondas, gacetas_nombre):
    from apps.aprobaciones.models import Aprobacion
    from apps.workflows.models import EsquemaAprobacionWorkflow

    contexto = {}
    for vinculo in EsquemaAprobacionWorkflow.objects.filter(
        instancia_etapa__instancia_workflow_id__in=instancias_ids
    ).select_related("instancia_etapa__bloque_operativo", "instancia_etapa__instancia_workflow"):
        etapa = vinculo.instancia_etapa
        elemento_id = etapa.instancia_workflow.elemento_id
        objeto = nombres.get(elemento_id) if elemento_id else ""
        paso = etapa.bloque_operativo.nombre if etapa.bloque_operativo_id else ""
        contexto[vinculo.esquema_id] = (objeto or paso, f"Paso: {paso}" if objeto and paso else "")
    for ronda in rondas:
        if ronda.esquema_id:
            participacion = ronda.participacion
            contexto[ronda.esquema_id] = (
                nombres.get(participacion.elemento_id, ""),
                f"Gaceta: {gacetas_nombre.get(participacion.gaceta_id, '')} · ronda {ronda.numero}",
            )
    if not contexto:
        return []
    eventos = []
    for aprobacion in Aprobacion.objects.filter(
        esquema_id__in=contexto, estado__in=list(ACCIONES_APROBACION), decidida_en__isnull=False
    ).select_related("decidido_por"):
        objeto, donde = contexto[aprobacion.esquema_id]
        eventos.append(_evento(aprobacion.decidida_en, aprobacion.decidido_por, ACCIONES_APROBACION[aprobacion.estado],
                               objeto, [donde, _corto(aprobacion.observacion)]))
    return eventos


# --- 4. Gacetas -------------------------------------------------------------------------------------------------

def _de_gacetas(gacetas, participaciones, rondas, nombres):
    from apps.gacetas.models import ObservacionGaceta

    eventos = []
    for gaceta in gacetas:
        if gaceta.abierta_en:
            eventos.append(_evento(gaceta.abierta_en, None, "abrió la gaceta", gaceta.nombre))
        if gaceta.periodo_cerrado_en:
            eventos.append(_evento(gaceta.periodo_cerrado_en, None, "cerró el periodo de revisión de la gaceta", gaceta.nombre))
        if gaceta.resuelta_en:
            eventos.append(_evento(gaceta.resuelta_en, gaceta.resuelta_por, "resolvió la gaceta", gaceta.nombre))
    nombre_gaceta = {g.pk: g.nombre for g in gacetas}
    for participacion in participaciones:
        objeto = nombres.get(participacion.elemento_id, "")
        eventos.append(_evento(participacion.presentada_en, participacion.presentada_por, "presentó en la gaceta", objeto,
                               [f"Gaceta: {nombre_gaceta.get(participacion.gaceta_id, '')}"]))
        # Lo que decide una persona ya aparece como su aprobación; aquí solo lo que decidió el sistema.
        if participacion.decidida_en and participacion.decidida_por_id is None and participacion.estado in ("APROBADA", "RECHAZADA", "EXCLUIDA"):
            eventos.append(_evento(participacion.decidida_en, None,
                                   DECISION_AUTOMATICA[participacion.estado], objeto,
                                   [f"Gaceta: {nombre_gaceta.get(participacion.gaceta_id, '')}"]))
    for ronda in rondas:
        if ronda.numero > 1:
            eventos.append(_evento(ronda.abierta_en, None, f"abrió la ronda {ronda.numero} de revisión de",
                                   nombres.get(ronda.participacion.elemento_id, "")))
    for observacion in ObservacionGaceta.objects.filter(participacion__in=participaciones).select_related(
        "autor", "respondida_por", "participacion"
    ):
        objeto = nombres.get(observacion.participacion.elemento_id, "")
        eventos.append(_evento(observacion.creada_en, observacion.autor, "observó", objeto, [_corto(observacion.texto)]))
        if observacion.respondida_en:
            eventos.append(_evento(observacion.respondida_en, observacion.respondida_por, "respondió la observación sobre",
                                   objeto, [_corto(observacion.respuesta)]))
    return eventos


# --- Composición --------------------------------------------------------------------------------------------------

def eventos(ticket):
    """La línea de tiempo completa del ticket, del más reciente al más antiguo."""
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta, RondaRevisionGaceta

    elementos, nombres = _items(ticket)
    principal, hijas = _instancias(ticket)
    instancias_ids = ([principal.pk] if principal else []) + [h.pk for h in hijas]
    gacetas = list(GacetaTicket.objects.filter(ticket=ticket).select_related("resuelta_por"))
    participaciones = list(
        ParticipacionItemGaceta.objects.filter(gaceta__ticket=ticket).select_related("presentada_por")
    )
    rondas = list(RondaRevisionGaceta.objects.filter(participacion__gaceta__ticket=ticket).select_related("participacion"))

    todos = _del_historial(ticket)
    todos += _de_items(ticket, elementos, nombres)
    todos += _del_flujo(ticket, principal, hijas, nombres)
    todos += _de_actividades(ticket, instancias_ids, [e.pk for e in elementos], rondas)
    todos += _de_aprobaciones(instancias_ids, nombres, rondas, {g.pk: g.nombre for g in gacetas})
    todos += _de_gacetas(gacetas, participaciones, rondas, nombres)
    todos.sort(key=lambda e: e["fecha"], reverse=True)
    return todos
