"""Contenido de cada aviso: un título, UNA frase con quién hizo qué, de 3 a 5 datos clave y, cuando hay que corregir,
un extracto corto. Nunca respuestas del formulario, adjuntos ni la conversación completa. El mismo contenido se usa
en el centro de notificaciones y en el correo (decisión del usuario: resumen moderado + un enlace)."""

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

MAX_EXTRACTO = 280


def _nombre(usuario):
    if usuario is None:
        return ""
    return usuario.nombre_visible


def _actor(aviso):
    from apps.core.models import Usuario

    if aviso.actor_id is None:
        return None
    return Usuario.objects.filter(pk=aviso.actor_id).first()


def nombre_ticket(ticket):
    if ticket is None:
        return ""
    detalle = getattr(ticket, "detalle_servicio", None)
    nombre = detalle.servicio.nombre if detalle is not None else f"Ticket {ticket.pk}"
    return f"{nombre} – {ticket.etiqueta}" if ticket.etiqueta else nombre


def fecha(valor):
    """«viernes 14 de noviembre» (con hora si es fecha-hora y no es medianoche local)."""
    if not valor:
        return ""
    if isinstance(valor, str):
        from django.utils.dateparse import parse_datetime

        valor = parse_datetime(valor) or valor
        if isinstance(valor, str):
            return valor
    if hasattr(valor, "hour"):
        local = timezone.localtime(valor) if timezone.is_aware(valor) else valor
        texto = date_format(local, r"l j \d\e F")
        if (local.hour, local.minute) not in ((0, 0), (23, 59)):
            texto += f", {local:%H:%M}"
        return texto
    return date_format(valor, r"l j \d\e F")


def recortar(texto, limite=MAX_EXTRACTO):
    texto = " ".join((texto or "").split())
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def _hechos(aviso, ticket):
    hechos = []
    if ticket is not None:
        detalle = getattr(ticket, "detalle_servicio", None)
        if detalle is not None:
            hechos.append(["Proceso" if ticket.tipo == "PROCESO" else "Servicio", detalle.servicio.nombre])
        if ticket.etiqueta:
            hechos.append(["Ejecución", ticket.etiqueta])
        try:
            from apps.tickets.seguimiento import fase_actual

            fase = fase_actual(ticket)
        except Exception:  # noqa: BLE001
            fase = ""
        if fase:
            hechos.append(["Etapa", fase])
    contexto = aviso.contexto
    if aviso.clave == "COMENTARIO" and contexto.get("canal"):
        from apps.tickets.conversacion import ETIQUETAS, HECHO_CANAL

        hechos.append([HECHO_CANAL, ETIQUETAS.get(contexto["canal"], contexto["canal"])])
    if contexto.get("tarea") and aviso.cantidad == 1:
        hechos.append(["Actividad", contexto["tarea"]])
    if contexto.get("fecha_limite"):
        hechos.append(["Fecha límite", fecha(contexto["fecha_limite"])])
    elif contexto.get("vence_en") and aviso.clave in ("ENTREGA_DISPONIBLE", "GACETA_ABIERTA", "RECORDATORIO_ENTREGA"):
        hechos.append(["Se acepta sola el" if aviso.clave != "GACETA_ABIERTA" else "Revisar antes del", fecha(contexto["vence_en"])])
    elif ticket is not None and ticket.fecha_objetivo_vigente and aviso.clave not in ("TICKET_CERRADO", "TICKET_CANCELADO"):
        hechos.append(["Fecha objetivo", fecha(ticket.fecha_objetivo_vigente)])
    if contexto.get("gaceta") and aviso.clave.startswith("GACETA"):
        hechos.append(["Gaceta", contexto["gaceta"]])
    if ticket is not None and ticket.solicitante_id and aviso.clave in ("TICKET_EN_COLA", "TICKET_ASIGNADO", "ALERTA_SIN_TOMAR"):
        hechos.append(["Solicitado por", _nombre(ticket.solicitante)])
    for etiqueta, valor in contexto.get("hechos_extra") or []:
        hechos.append([etiqueta, valor])
    return [h for h in hechos if h[1]][:5]


def componer(aviso):
    """`{"titulo", "resumen", "hechos", "extracto"}` del aviso."""
    ticket = aviso.ticket
    contexto = aviso.contexto
    actor = _actor(aviso)
    quien = _nombre(actor) if actor else ""
    nombre = nombre_ticket(ticket)
    codigo = ticket.codigo if ticket is not None and ticket.consecutivo else ""
    t = f"«{nombre}»" if nombre else "el ticket"
    titulo, resumen = _texto(aviso.clave, contexto, quien, t, codigo, aviso.cantidad)
    if contexto.get("titulo"):
        titulo = contexto["titulo"]
    if contexto.get("resumen"):
        resumen = contexto["resumen"]
    return {
        "titulo": titulo,
        "resumen": resumen,
        "hechos": _hechos(aviso, ticket),
        "extracto": recortar(contexto.get("extracto", "")),
    }


def _por(quien, accion_con_actor, sin_actor):
    return f"{quien} {accion_con_actor}" if quien else sin_actor


def _texto(clave, c, quien, t, codigo, cantidad):
    tarea = f"«{c.get('tarea')}»" if c.get("tarea") else "una actividad"
    gaceta = f"«{c.get('gaceta')}»" if c.get("gaceta") else "la gaceta"
    item = f"«{c.get('item')}»" if c.get("item") else "una pieza"
    textos = {
        "TICKET_RADICADO": ("Tu ticket fue radicado", f"Radicaste {t}." + (f" Su código es {codigo}." if codigo else "")),
        "TICKET_EN_ATENCION": ("Tu ticket está en atención", _por(quien, f"empezó a atender {t}.", f"{t} ya está en atención.")),
        "INFORMACION_SOLICITADA": ("Te piden información", _por(quien, f"necesita información para continuar con {t}.",
                                                               f"Se necesita información para continuar con {t}.")),
        "COMENTARIO": ("Nuevo comentario", _por(quien, f"comentó en {t}.", f"Hay un comentario nuevo en {t}.")),
        "FECHA_OBJETIVO_CAMBIADA": ("Cambió la fecha objetivo", f"Se aprobó una prórroga para {t}: su fecha objetivo cambió."),
        "ENTREGA_DISPONIBLE": ("Tienes una entrega por revisar", _por(quien, f"entregó el resultado de {t}. Revísalo y acéptalo u obsérvalo.",
                                                                     f"Ya está el resultado de {t}. Revísalo y acéptalo u obsérvalo.")),
        "TICKET_RESUELTO": ("Tu ticket fue resuelto", _por(quien, f"resolvió {t}.", f"{t} fue resuelto.")),
        "TICKET_CERRADO": ("Ticket cerrado", _por(quien, f"cerró {t}.", f"{t} se cerró.")),
        "TICKET_CANCELADO": ("Ticket cancelado", _por(quien, f"canceló {t}.", f"{t} fue cancelado.")),
        "TICKET_REABIERTO": ("Ticket reabierto", _por(quien, f"reabrió {t}.", f"{t} se reabrió.")),
        "TICKET_EN_COLA": ("Nuevo ticket en la Cola", f"{t} llegó a la Cola y nadie lo ha tomado. Puedes tomarlo o asignarlo."),
        "TICKET_ASIGNADO": ("Te asignaron un ticket",
                            f"{t} se dirigió a ti." if c.get("dirigido") else _por(quien, f"te asignó {t}.", f"{t} quedó a tu cargo.")),
        "TICKET_REASIGNADO_FUERA": ("Ya no tienes este ticket", _por(quien, f"reasignó {t} a otra persona.", f"{t} se reasignó.")),
        "TICKET_GENERADO": ("Se generó una ejecución", f"Se generó {t} y está a tu cargo."),
        "INFORMACION_RESPONDIDA": ("Respondieron la información", _por(quien, f"respondió la información pedida en {t}.",
                                                                       f"Llegó la información pedida en {t}.")),
        "ENTREGA_ACEPTADA": ("Entrega aceptada", _por(quien, f"aceptó la entrega de {t}.", f"Se aceptó la entrega de {t}.")),
        "ENTREGA_OBSERVADA": ("La entrega tiene observaciones", _por(quien, f"observó la entrega de {t}.", f"La entrega de {t} tiene observaciones.")),
        "PRORROGA_RECHAZADA": ("Prórroga rechazada", _por(quien, f"rechazó la prórroga de {t}.", f"La prórroga de {t} fue rechazada.")),
        "CANCELACION_SOLICITADA": ("Se pidió cancelar el ticket", _por(
            quien, f"pidió cancelar {t}. Queda detenido hasta que {c.get('aprobador') or 'su aprobador'} decida.",
            f"Se pidió cancelar {t}. Queda detenido hasta que {c.get('aprobador') or 'su aprobador'} decida.")),
        "CANCELACION_RECHAZADA": ("Cancelación rechazada", _por(quien, f"rechazó cancelar {t}: el trabajo continúa.",
                                                                f"No se aceptó cancelar {t}: el trabajo continúa.")),
        "APROBACION_TURNO": ("Tienes una aprobación pendiente", f"Es tu turno de aprobar {c.get('que') or 'una aprobación'} en {t}."),
        "TRABAJO_DEVUELTO": (
            "Rechazaron el trabajo" if c.get("rechazo") else "Devolvieron el trabajo",
            _por(quien, f"{'rechazó' if c.get('rechazo') else 'devolvió'} {c.get('que') or 'el trabajo'} en {t}.",
                 f"{c.get('que') or 'El trabajo'} fue {'rechazado' if c.get('rechazo') else 'devuelto'} en {t}."),
        ),
        "GACETA_ABIERTA": ("Hay piezas para revisar", f"Se abrió la gaceta {gaceta} de {t}."),
        "GACETA_OBSERVACION": ("Tienes una corrección", _por(quien, f"observó {item} en la gaceta {gaceta}.", f"{item} tiene observaciones en la gaceta {gaceta}.")),
        "GACETA_NUEVA_RONDA": ("Llegó una versión corregida", f"{item} volvió a revisión en la gaceta {gaceta}."),
        "GACETA_PERIODO_CERRADO": ("Se cerró el periodo de revisión", f"Terminó el periodo de observaciones de la gaceta {gaceta}."),
        "GACETA_RESUELTA": ("Gaceta resuelta", _por(quien, f"resolvió la gaceta {gaceta}.", f"La gaceta {gaceta} se resolvió.")),
    }
    if clave == "APROBACION_TURNO" and cantidad > 1:
        paso = f"«{c['paso']}»" if c.get("paso") else "aprobación"
        return (f"Tienes {cantidad} aprobaciones pendientes",
                f"Llegaron {cantidad} ítems a {paso} en {t}: es tu turno de aprobarlos.")
    if clave == "RECORRIDO_COMPLETADO":
        paso = f"«{c['paso']}»" if c.get("paso") else "el recorrido"
        completados, fuera = c.get("completados") or 0, c.get("excluidos") or 0
        detalle = f"{completados} ítem{'s' if completados != 1 else ''} completo{'s' if completados != 1 else ''}"
        if fuera:
            detalle += f" y {fuera} fuera del alcance"
        return (f"Se completó {paso}", f"Todos los ítems de {paso} terminaron en {t}: {detalle}.")
    if clave == "TAREA_ASIGNADA":
        if cantidad > 1:
            return f"Tienes {cantidad} actividades nuevas", f"Tienes {cantidad} actividades nuevas en {t}."
        return "Te asignaron una actividad", _por(quien, f"te asignó {tarea}" + (f" en {t}." if c.get("tarea") and t != "el ticket" else "."),
                                                 f"Tienes una nueva actividad: {tarea}.")
    if clave == "REGLA_ETAPA":
        objetivo = f"«{c.get('objetivo')}»" if c.get("objetivo") else "una etapa"
        frases = {
            "FASE_ALCANZADA": (f"{t} llegó a {objetivo}", f"{t} llegó a la fase {objetivo}."),
            "ACTIVIDAD_INICIADA": (f"Empezó {objetivo}", f"Empezó la actividad {objetivo} en {t}."),
            "ACTIVIDAD_COMPLETADA": (f"Se completó {objetivo}", f"Se completó la actividad {objetivo} en {t}."),
            "APROBACION_DEVUELTA": (f"{objetivo} fue devuelta o rechazada", f"La aprobación {objetivo} devolvió o rechazó el trabajo en {t}."),
            "GACETA_ABIERTA": (f"Se abrió {objetivo}", f"Se abrió la gaceta {objetivo} de {t}."),
            "GACETA_RESUELTA": (f"Se resolvió {objetivo}", f"Se resolvió la gaceta {objetivo} de {t}."),
        }
        return frases.get(c.get("momento"), ("Aviso del proceso", f"Hay novedades en {t}."))
    return textos.get(clave, ("Aviso", f"Hay novedades en {t}."))


def enlace_para(usuario, aviso):
    """Ruta interna SOLO si la persona puede abrir el objeto (al abrir se vuelve a autorizar). Quien solicitó va a
    «Ver mi ticket»; quien participa, al espacio del ticket; una tarea o aprobación sin ticket, a su detalle."""
    ticket = aviso.ticket
    if ticket is not None and ticket.estado != "BORRADOR":
        try:
            from apps.tickets import espacio

            rel = espacio.relacion(usuario, ticket)
        except Exception:  # noqa: BLE001
            rel = None
        if rel is not None and aviso.clave == "COMENTARIO" and rel.puede_abrir:
            # Un mensaje de la conversación abre el ticket con ESA conversación a la vista.
            canal = aviso.contexto.get("canal") or ""
            return reverse("tickets:espacio", args=[ticket.pk]) + f"?panel=conversacion&canal={canal}"
        if rel is not None:
            solo_solicita = rel.es_solicitante and not (rel.atiende or rel.tareas or rel.aprobaciones or rel.gacetas)
            if solo_solicita and ticket.solicitante_id == usuario.pk:
                return reverse("tickets:seguimiento", args=[ticket.pk])
            if rel.puede_abrir:
                return reverse("tickets:espacio", args=[ticket.pk])
        if aviso.clave == "TICKET_EN_COLA":
            # Quien puede tomarlo aún no consulta el ticket: el aviso lo lleva a la Cola, donde está el botón.
            return reverse("tickets:cola")
    tarea_id = aviso.contexto.get("tarea_id")
    if tarea_id:
        from apps.tareas.autorizacion import puede_consultar_tarea
        from apps.tareas.models import Tarea

        tarea = Tarea.objects.filter(pk=tarea_id).first()
        if tarea is not None and puede_consultar_tarea(usuario, tarea):
            from apps.tickets.vinculos import url_de_tarea

            return url_de_tarea(tarea)
    aprobacion_id = aviso.contexto.get("aprobacion_id")
    if aprobacion_id:
        from apps.aprobaciones.autorizacion import puede_consultar_aprobacion
        from apps.aprobaciones.models import Aprobacion

        aprobacion = Aprobacion.objects.filter(pk=aprobacion_id).select_related("esquema").first()
        if aprobacion is not None and puede_consultar_aprobacion(usuario, aprobacion):
            from apps.tickets.vinculos import url_de_aprobacion

            return url_de_aprobacion(aprobacion)
    return ""
