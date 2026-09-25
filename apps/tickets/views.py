"""Vistas de Tickets — incrementos 2.1 (CU-015), 2.2 (CU-014), 2.3
(CU-016/CU-017), 2.4 (CU-018) y 2.5 (CU-019). Interfaz propia desde el día
1, sin Django Admin (decisión explícita para todo el Sprint 2: Ticket no
es back-office, es la funcionalidad central de cara al usuario final)."""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, Servicio
from apps.core.models import Equipo
from apps.tickets import operaciones
from apps.tickets.autorizacion import (
    es_propietario_borrador,
    puede_asignar,
    puede_cancelar_ticket,
    puede_cerrar_ticket,
    puede_comentar_ticket,
    puede_consultar_ticket,
    puede_reabrir_ticket,
    puede_reasignar,
    puede_resolver_ticket,
    puede_solicitar_informacion,
    puede_tomar,
    puede_ver_en_cola,
)
from apps.tickets.models import Adjunto, ArchivoRespuestaCampo, SolicitudInformacion, Ticket


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required
def mis_tickets_view(request):
    """CU-014/CU-015 — lista BORRADOR y RADICADO propios (2.2). La
    estructura (badge/enlace por `ticket.estado` en la plantilla) admite
    agregar EN_ATENCION/RESUELTO/etc. en 2.3+ sumando una rama, sin
    rehacer la pantalla."""
    tickets = (
        Ticket.objects.filter(
            solicitante=request.user,
            estado__in=[Ticket.Estado.BORRADOR, Ticket.Estado.RADICADO],
        )
        .select_related("detalle_servicio__servicio")
        .order_by("-creado_en")
    )
    contexto = {"tickets": tickets, "titulo_pagina": "Mis tickets"}
    return render(request, "tickets/mis_tickets.html", contexto)


@login_required
def cola_atencion_view(request):
    """CU-017/RQF-061 — separada de `mis_tickets_view`: aquí nunca aparecen
    tickets donde el usuario es solo el solicitante. El candidato se filtra
    por estado en SQL; la autorización efectiva (`puede_ver_en_cola`, que
    combina alcance de `tickets.atender` con la relación operacional real)
    se evalúa en Python — volumen esperado bajo para una herramienta
    interna, sin necesidad de traducir la lógica de autorización a SQL."""
    candidatos = (
        Ticket.objects.filter(estado__in=[Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION])
        .select_related("detalle_servicio__servicio", "usuario_responsable", "equipo_responsable")
        .prefetch_related("contextos_atencion")
        .order_by("-radicado_en")
    )
    tickets = [t for t in candidatos if puede_ver_en_cola(request.user, t)]
    contexto = {"tickets": tickets, "titulo_pagina": "Cola de atención"}
    return render(request, "tickets/cola_atencion.html", contexto)


@login_required
def iniciar_borrador_view(request, servicio_id):
    servicio = get_object_or_404(Servicio, pk=servicio_id)
    try:
        ticket = operaciones.crear_borrador(request.user, servicio)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("catalogo:detalle", pk=servicio_id)
    return redirect("tickets:borrador", pk=ticket.pk)


def _leer_respuestas_de_request(request, version):
    respuestas = {}
    for campo in version.campos.all():
        nombre = f"campo_{campo.id}"
        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            if nombre in request.FILES:
                respuestas[campo.id] = request.FILES[nombre]
            elif request.POST.get(f"{nombre}__eliminar"):
                respuestas[campo.id] = None
        elif campo.tipo == Campo.TipoCampo.MULTILISTA:
            if nombre in request.POST:
                respuestas[campo.id] = request.POST.getlist(nombre)
        elif campo.tipo == Campo.TipoCampo.BOOLEANO:
            # Un checkbox ausente en el POST significa "no marcado", no
            # "campo no tocado" — siempre se considera parte de este envío.
            respuestas[campo.id] = nombre in request.POST
        elif nombre in request.POST:
            respuestas[campo.id] = request.POST.get(nombre)
    return respuestas


def _construir_campos_formulario(respuesta_formulario, version):
    """Estructura reutilizada por `borrador_formulario_view` (editable),
    `radicar_view` (para re-render con errores) y `detalle_view` (2.2,
    solo lectura)."""
    existentes = {
        rc.campo_id: rc
        for rc in respuesta_formulario.respuestas_campo.select_related("archivo", "campo").all()
    }
    campos_formulario = []
    for campo in version.campos.prefetch_related("opciones").all():
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        campos_formulario.append(
            {
                "campo": campo,
                "widget": estrategia.widget,
                "opciones": campo.opciones.all(),
                "opciones_referencia": estrategia.queryset(campo) if hasattr(estrategia, "queryset") else None,
                "respuesta": existentes.get(campo.id),
                "errores": None,
            }
        )
    return campos_formulario


@login_required
def borrador_formulario_view(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket) or ticket.estado != Ticket.Estado.BORRADOR:
        raise PermissionDenied

    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version

    if request.method == "POST":
        respuestas_crudas = _leer_respuestas_de_request(request, version)
        try:
            operaciones.guardar_respuestas_borrador(ticket, request.user, respuestas_crudas)
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Borrador guardado.")
            return redirect("tickets:borrador", pk=ticket.pk)

    contexto = {
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "campos_formulario": _construir_campos_formulario(respuesta_formulario, version),
        "titulo_pagina": f"Nuevo ticket — {ticket.detalle_servicio.servicio.nombre}",
    }
    return render(request, "tickets/borrador_formulario.html", contexto)


@login_required
def radicar_view(request, pk):
    """CU-014/RQF-053/054 — incremento 2.2.

    Ejecuta `guardar_respuestas_borrador` con el envío actual del
    formulario y, si eso tiene éxito, `radicar_ticket`. Son dos
    operaciones atómicas independientes, no una única transacción
    envolvente: si `radicar_ticket` falla (campos obligatorios pendientes,
    servicio desactivado, etc.), las respuestas recién guardadas
    permanecen persistidas — el usuario no pierde lo que acaba de escribir
    y puede corregir solo lo que falta, en vez de perder todo el envío.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket) or ticket.estado != Ticket.Estado.BORRADOR:
        raise PermissionDenied

    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    respuestas_crudas = _leer_respuestas_de_request(request, version)

    try:
        operaciones.guardar_respuestas_borrador(ticket, request.user, respuestas_crudas)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("tickets:borrador", pk=ticket.pk)

    try:
        operaciones.radicar_ticket(ticket, request.user)
    except ValidationError as exc:
        errores_por_campo = exc.message_dict if hasattr(exc, "error_dict") else None
        if errores_por_campo:
            campos_formulario = _construir_campos_formulario(respuesta_formulario, version)
            for item in campos_formulario:
                lista = errores_por_campo.get(item["campo"].id)
                if lista:
                    item["errores"] = lista
            messages.error(request, "Revisa los campos señalados antes de radicar.")
            contexto = {
                "ticket": ticket,
                "servicio": ticket.detalle_servicio.servicio,
                "campos_formulario": campos_formulario,
                "titulo_pagina": f"Nuevo ticket — {ticket.detalle_servicio.servicio.nombre}",
            }
            return render(request, "tickets/borrador_formulario.html", contexto)
        messages.error(request, _mensaje_error(exc))
        return redirect("tickets:borrador", pk=ticket.pk)

    messages.success(request, "Ticket radicado correctamente.")
    return redirect("tickets:detalle", pk=ticket.pk)


@login_required
def detalle_view(request, pk):
    """Incremento 2.2 (radicado, fecha/hora, servicio, estado, respuestas y
    archivos) + 2.3 (responsable/equipo, acciones Tomar/Asignar/Reasignar
    según autorización, línea de tiempo) + 2.4 (comunicación, adjuntos
    operativos, solicitudes de información). Visible para el solicitante
    (`es_propietario_borrador`) o para quien tenga autorización de
    atención sobre el ticket (`puede_ver_en_cola`) — un ticket en
    BORRADOR nunca satisface `puede_ver_en_cola` (ver `autorizacion.py`),
    así que solo el solicitante llega al `redirect` de abajo."""
    ticket = get_object_or_404(Ticket, pk=pk)
    es_solicitante = es_propietario_borrador(request.user, ticket)
    if not (es_solicitante or puede_ver_en_cola(request.user, ticket)):
        raise PermissionDenied
    if ticket.estado == Ticket.Estado.BORRADOR:
        return redirect("tickets:borrador", pk=ticket.pk)

    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    usuario_puede_asignar = puede_asignar(request.user, ticket)
    usuario_puede_reasignar = puede_reasignar(request.user, ticket)
    resoluciones = list(
        ticket.resoluciones.select_related("resuelto_por")
        .prefetch_related("adjuntos")
        .order_by("-resuelto_en")
    )
    contexto = {
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "campos_formulario": _construir_campos_formulario(respuesta_formulario, version),
        "historial": ticket.historial.select_related("actor").all(),
        "puede_tomar": puede_tomar(request.user, ticket),
        "puede_asignar": usuario_puede_asignar,
        "puede_reasignar": usuario_puede_reasignar,
        "usuarios_disponibles": get_user_model().objects.filter(is_active=True).order_by("username")
        if (usuario_puede_asignar or usuario_puede_reasignar)
        else None,
        "equipos_disponibles": Equipo.objects.filter(activo=True).order_by("nombre")
        if (usuario_puede_asignar or usuario_puede_reasignar)
        else None,
        "comentarios": ticket.comentarios.select_related("autor").prefetch_related("adjuntos").all(),
        "adjuntos_ticket": ticket.adjuntos.select_related("subido_por").all(),
        "solicitudes": ticket.solicitudes_informacion.select_related(
            "solicitada_por", "destinatario", "respuesta__respondida_por"
        ).prefetch_related("adjuntos", "respuesta__adjuntos"),
        "puede_comentar": puede_comentar_ticket(request.user, ticket),
        "puede_solicitar_informacion": puede_solicitar_informacion(request.user, ticket),
        # Solo el destinatario de una SolicitudInformacion puede responderla, y en
        # 2.4 el destinatario es siempre el solicitante (R.1) — reutilizar
        # `es_solicitante` evita evaluar `puede_responder_solicitud` fila por fila
        # en la plantilla.
        "usuario_es_destinatario_solicitudes": es_solicitante,
        # 2.5/2.C — resolución/cierre/cancelación/reapertura. `resoluciones`
        # ordenadas más reciente primero: la actual es la [0] (si existe);
        # el resto (tras un ciclo RESOLVER→REABRIR→RESOLVER) son historia.
        "resolucion_actual": resoluciones[0] if resoluciones else None,
        "resoluciones_anteriores": resoluciones[1:],
        "puede_resolver": puede_resolver_ticket(request.user, ticket),
        "puede_cerrar": puede_cerrar_ticket(request.user, ticket),
        "puede_cancelar": puede_cancelar_ticket(request.user, ticket),
        "puede_reabrir": puede_reabrir_ticket(request.user, ticket),
        "titulo_pagina": f"Ticket {ticket.radicado} — {ticket.detalle_servicio.servicio.nombre}",
    }
    return render(request, "tickets/detalle.html", contexto)


@login_required
def tomar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    try:
        operaciones.tomar_ticket(ticket, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket tomado.")
    return redirect("tickets:detalle", pk=pk)


def _usuario_y_equipo_de_request(request):
    Usuario = get_user_model()
    usuario_id = request.POST.get("usuario_id") or None
    equipo_id = request.POST.get("equipo_id") or None
    usuario = get_object_or_404(Usuario, pk=usuario_id) if usuario_id else None
    equipo = get_object_or_404(Equipo, pk=equipo_id) if equipo_id else None
    return usuario, equipo


@login_required
def asignar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    usuario, equipo = _usuario_y_equipo_de_request(request)
    try:
        operaciones.asignar_ticket(ticket, request.user, usuario=usuario, equipo=equipo)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket asignado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def reasignar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    usuario, equipo = _usuario_y_equipo_de_request(request)
    try:
        operaciones.reasignar_ticket(ticket, request.user, usuario=usuario, equipo=equipo)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket reasignado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def resolver_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    descripcion = request.POST.get("descripcion", "")
    archivo = request.FILES.get("archivo")
    try:
        operaciones.resolver_ticket(
            ticket, request.user, descripcion, archivos=[archivo] if archivo else None
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket resuelto.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def cerrar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    try:
        operaciones.cerrar_ticket(ticket, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket cerrado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def cancelar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    motivo = request.POST.get("motivo", "")
    try:
        operaciones.cancelar_ticket(ticket, request.user, motivo)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket cancelado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def reabrir_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    motivo = request.POST.get("motivo", "")
    try:
        operaciones.reabrir_ticket(ticket, request.user, motivo)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket reabierto.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def comentar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    contenido = request.POST.get("contenido", "")
    archivo = request.FILES.get("archivo")
    try:
        operaciones.comentar_ticket(ticket, request.user, contenido, archivos=[archivo] if archivo else None)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Comentario agregado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def adjuntar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    archivo = request.FILES.get("archivo")
    try:
        if not archivo:
            raise ValidationError("Debe seleccionar un archivo.")
        operaciones.adjuntar_archivo_ticket(ticket, request.user, archivo)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Archivo adjuntado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def solicitar_informacion_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    mensaje = request.POST.get("mensaje", "")
    archivo = request.FILES.get("archivo")
    try:
        operaciones.solicitar_informacion(
            ticket, request.user, mensaje, archivos=[archivo] if archivo else None
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Solicitud de información enviada.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def responder_solicitud_view(request, pk, solicitud_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    solicitud = get_object_or_404(SolicitudInformacion, pk=solicitud_id, ticket=ticket)
    contenido = request.POST.get("contenido", "")
    archivo = request.FILES.get("archivo")
    try:
        operaciones.responder_solicitud(
            solicitud, request.user, contenido, archivos=[archivo] if archivo else None
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Respuesta enviada.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def eliminar_borrador_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket):
        raise PermissionDenied
    try:
        operaciones.eliminar_borrador(ticket, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("tickets:borrador", pk=pk)
    messages.success(request, "Borrador eliminado.")
    return redirect("tickets:mis_tickets")


@login_required
def descargar_archivo_respuesta_view(request, archivo_id):
    """RQF-006 (corregido en 2.4): la autorización real es "puede consultar
    el Ticket relacionado", no "es el solicitante" — un gestor/responsable
    ya autorizado a ver el detalle del ticket (y, por tanto, esta misma
    respuesta) también puede descargar su archivo. Antes de 2.4 esta vista
    solo aceptaba `es_propietario_borrador`, más estricto que lo que RQF-006
    exige y distinto de lo que ya permitía el propio detalle del ticket."""
    archivo = get_object_or_404(
        ArchivoRespuestaCampo.objects.select_related("respuesta_campo__respuesta_formulario__ticket"),
        pk=archivo_id,
    )
    ticket = archivo.respuesta_campo.respuesta_formulario.ticket
    if not puede_consultar_ticket(request.user, ticket):
        raise PermissionDenied
    return FileResponse(archivo.archivo.open("rb"), as_attachment=True, filename=archivo.nombre_original)


@login_required
def descargar_adjunto_view(request, adjunto_id):
    """RQF-006/RQ-NFN-04 — misma autorización que `descargar_archivo_respuesta_view`,
    centralizada en `puede_consultar_ticket`: nunca se expone el adjunto
    solo por conocer su URL de MEDIA."""
    adjunto = get_object_or_404(
        Adjunto.objects.select_related(
            "ticket",
            "comentario__ticket",
            "solicitud__ticket",
            "respuesta_solicitud__solicitud__ticket",
        ),
        pk=adjunto_id,
    )
    ticket = adjunto.ticket_relacionado
    if not puede_consultar_ticket(request.user, ticket):
        raise PermissionDenied
    return FileResponse(adjunto.archivo.open("rb"), as_attachment=True, filename=adjunto.nombre_original)
