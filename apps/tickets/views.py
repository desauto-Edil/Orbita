"""Vistas de Tickets — incrementos 2.1 (CU-015) y 2.2 (CU-014). Interfaz
propia desde el día 1, sin Django Admin (decisión explícita para todo el
Sprint 2: Ticket no es back-office, es la funcionalidad central de cara al
usuario final)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, Servicio
from apps.tickets import operaciones
from apps.tickets.autorizacion import es_propietario_borrador
from apps.tickets.models import ArchivoRespuestaCampo, Ticket


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
    """Incremento 2.2 — vista mínima de solo lectura de un Ticket radicado
    (radicado, fecha/hora, servicio, estado, respuestas y archivos). Sin
    historial, comentarios, asignación ni acciones de atención — eso es
    2.3+."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket):
        raise PermissionDenied
    if ticket.estado == Ticket.Estado.BORRADOR:
        return redirect("tickets:borrador", pk=ticket.pk)

    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    contexto = {
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "campos_formulario": _construir_campos_formulario(respuesta_formulario, version),
        "titulo_pagina": f"Ticket {ticket.radicado} — {ticket.detalle_servicio.servicio.nombre}",
    }
    return render(request, "tickets/detalle.html", contexto)


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
    archivo = get_object_or_404(
        ArchivoRespuestaCampo.objects.select_related("respuesta_campo__respuesta_formulario__ticket"),
        pk=archivo_id,
    )
    ticket = archivo.respuesta_campo.respuesta_formulario.ticket
    if not es_propietario_borrador(request.user, ticket):
        raise PermissionDenied
    return FileResponse(archivo.archivo.open("rb"), as_attachment=True, filename=archivo.nombre_original)
