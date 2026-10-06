"""Vistas de Tickets — incrementos 2.1 (CU-015), 2.2 (CU-014), 2.3
(CU-016/CU-017), 2.4 (CU-018) y 2.5 (CU-019). Interfaz propia desde el día
1, sin Django Admin (decisión explícita para todo el Sprint 2: Ticket no
es back-office, es la funcionalidad central de cara al usuario final)."""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q
from django.http import FileResponse, Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo import destinos_ticket_general as destinos_ops
from apps.catalogo import ticket_general as general_ops
from apps.catalogo.models import Campo, Servicio
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.core.models import Equipo
from apps.tickets import entregables as entregables_ops
from apps.tickets import entregas as entregas_ops
from apps.tickets import direccionamiento, operaciones, prorrogas, solicitud
from apps.tickets.autorizacion import (
    es_propietario_borrador,
    es_responsable_actual,
    puede_asignar,
    aprobacion_pendiente_de_prorroga,
    motivo_no_elegible_para_prorroga,
    puede_cancelar_prorroga,
    puede_cancelar_ticket,
    puede_cerrar_ticket,
    puede_comentar_ticket,
    puede_consultar_ticket,
    puede_entregar_ticket,
    puede_escribir_entregables_finales,
    puede_iniciar_atencion,
    puede_reabrir_ticket,
    puede_consultar_prorrogas,
    puede_reasignar,
    puede_resolver_prorroga,
    puede_resolver_ticket,
    puede_responder_entrega,
    puede_solicitar_informacion,
    puede_solicitar_prorroga,
    puede_tomar,
    puede_ver_en_cola,
)
from apps.tickets.forms import CancelacionProrrogaForm, SolicitudProrrogaForm
from apps.tickets.models import (
    Adjunto,
    ArchivoRespuestaCampo,
    EntregableTicket,
    EntregaTicket,
    ProrrogaTicket,
    ResultadoEntregaTicket,
    SolicitudInformacion,
    Ticket,
)
from apps.tickets.validaciones import validar_para_radicar


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


COLA_POR_PAGINA = 30


@login_required
def cola_atencion_view(request):
    """CU-017/RQF-061 — separada de `mis_tickets_view`: aquí nunca aparecen
    tickets donde el usuario es solo el solicitante. El candidato se filtra
    por estado en SQL; la autorización efectiva (`puede_ver_en_cola`, que
    combina alcance de `tickets.atender` con la relación operacional real)
    se evalúa en Python — volumen esperado bajo para una herramienta
    interna, sin necesidad de traducir la lógica de autorización a SQL.

    Orden de llegada: el que lleva más tiempo radicado va primero, y su
    posición en la cola no cambia al filtrar. `ver` es un filtro LOCAL de
    presentación (sin tomar / en atención / míos) sobre esa misma población
    autorizada; no concede ni oculta nada por permisos."""
    candidatos = (
        Ticket.objects.filter(estado__in=[Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION])
        .select_related("detalle_servicio__servicio", "solicitante", "usuario_responsable", "equipo_responsable")
        .prefetch_related("contextos_atencion")
        .order_by("radicado_en", "pk")
    )
    visibles = [t for t in candidatos if puede_ver_en_cola(request.user, t)]
    for posicion, ticket in enumerate(visibles, start=1):
        ticket.posicion = posicion
        # "Sin tomar" = todavía no se inició la atención. Un Ticket General dirigido a
        # una persona (4.C2) está RADICADO con responsable: sigue sin iniciar.
        ticket.sin_tomar = ticket.estado == Ticket.Estado.RADICADO
        ticket.es_mio = ticket.usuario_responsable_id == request.user.pk

    filtros = {
        "todos": lambda t: True,
        "sin_tomar": lambda t: t.sin_tomar,
        "en_atencion": lambda t: not t.sin_tomar,
        "mios": lambda t: t.es_mio,
    }
    ver = request.GET.get("ver")
    if ver not in filtros:
        ver = "todos"
    conteos = {clave: sum(1 for t in visibles if criterio(t)) for clave, criterio in filtros.items()}
    pagina = Paginator([t for t in visibles if filtros[ver](t)], COLA_POR_PAGINA).get_page(request.GET.get("pagina"))
    contexto = {
        "tickets": pagina.object_list,
        "pagina": pagina,
        "ver": ver,
        "conteos": conteos,
        "segmentos": [
            {"clave": "todos", "etiqueta": "Todos"},
            {"clave": "sin_tomar", "etiqueta": "Sin tomar"},
            {"clave": "en_atencion", "etiqueta": "En atención"},
            {"clave": "mios", "etiqueta": "Míos"},
        ],
        "titulo_pagina": "Cola de atención",
    }
    for segmento in contexto["segmentos"]:
        segmento["total"] = conteos[segmento["clave"]]
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


@login_required
@require_GET
def solicitar_view(request, servicio_id):
    """V2 — entrada directa a la solicitud desde Inicio/Explorar, sin ficha
    intermedia. GET idempotente: retoma el borrador vacío del usuario o crea
    uno (`solicitud.obtener_o_crear_borrador`). El acceso por URL respeta la
    visibilidad del Servicio igual que el catálogo: 404, no un filtrado
    silencioso."""
    servicio = get_object_or_404(
        servicios_visibles_para(request.user).select_related("categoria", "formulario__version_activa"),
        pk=servicio_id,
    )
    if solicitud.version_activa_de(servicio) is None:
        return render(
            request,
            "tickets/solicitud_no_disponible.html",
            {"servicio": servicio, "titulo_pagina": servicio.nombre},
        )
    try:
        ticket = solicitud.obtener_o_crear_borrador(request.user, servicio)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("core:inicio")
    return redirect("tickets:borrador", pk=ticket.pk)


@login_required
@require_GET
def solicitar_general_view(request):
    """4.C1 — entrada explícita "Crear ticket general". GET idempotente, igual que
    `solicitar_view`: retoma el borrador vacío del usuario sobre el Servicio
    interno o crea uno, y sigue por el workspace de solicitud de siempre. Si el
    Ticket General está deshabilitado, no está configurado o no es accesible para
    el usuario, responde 404 como si no existiera: ocultar el botón no basta."""
    try:
        servicio = general_ops.servicio_para_crear_ticket(request.user)
    except (PermissionDenied, ValidationError):
        raise Http404("El ticket general no está disponible.")
    # 4.C2: sin un destino utilizable (ni de reserva) no podría radicarse.
    if solicitud.version_activa_de(servicio) is None or not destinos_ops.hay_destinos_utilizables():
        return render(
            request,
            "tickets/solicitud_no_disponible.html",
            {"servicio": servicio, "titulo_pagina": servicio.nombre},
        )
    try:
        ticket = solicitud.obtener_o_crear_borrador_general(request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("core:inicio")
    return redirect("tickets:borrador", pk=ticket.pk)


def _leer_respuestas_de_request(request, version, *, con_archivos=True):
    respuestas = {}
    for campo in version.campos.all():
        nombre = f"campo_{campo.id}"
        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            if not con_archivos:
                continue
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
    """Estructura de solo lectura que usa `detalle_view` (2.2). La
    experiencia de solicitud (V2) usa `solicitud.construir_items`."""
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


def _borrador_propio(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket) or ticket.estado != Ticket.Estado.BORRADOR:
        raise PermissionDenied
    return ticket


def _ticket_propio(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket):
        raise PermissionDenied
    return ticket


def _guardar_destino_general(request, ticket):
    """4.C2 — el selector de destino viaja con el formulario del Ticket General
    (`destino_general`). Devuelve el mensaje de error o `None`. Un campo ausente
    (otro tipo de ticket, o un envío que no lo trae) no cambia nada."""
    if "destino_general" not in request.POST or not direccionamiento.es_ticket_general(ticket):
        return None
    try:
        direccionamiento.seleccionar_destino_borrador(ticket, request.user, request.POST.get("destino_general"))
    except ValidationError as exc:
        return _mensaje_error(exc)
    return None


def _render_workspace(request, ticket, *, errores=None, valores_envio=None, error_destino=None):
    contexto = solicitud.contexto_workspace(
        ticket, errores=errores, valores_envio=valores_envio, error_destino=error_destino
    )
    contexto["titulo_pagina"] = f"Nuevo ticket — {contexto['servicio'].nombre}"
    return render(request, "tickets/solicitud.html", contexto)


def _guardar_envio(request, ticket, version):
    """Guarda el envío actual del formulario campo a campo.

    Lo que pasa la validación de su tipo se persiste con la operación de
    dominio de siempre (`guardar_respuestas_borrador`); lo que no, NO se
    guarda y se devuelve como error asociado a su campo, junto con el valor
    enviado para que el usuario no tenga que reescribirlo. Así un campo mal
    llenado no hace perder el resto del envío (ni los archivos ya subidos).

    Devuelve `(errores, valores_con_error, error_general)`.
    """
    crudas = _leer_respuestas_de_request(request, version)
    errores = solicitud.errores_de_formato(ticket, crudas)
    validas = {campo_id: valor for campo_id, valor in crudas.items() if campo_id not in errores}
    try:
        operaciones.guardar_respuestas_borrador(ticket, request.user, validas)
    except ValidationError as exc:
        return {}, {}, _mensaje_error(exc)
    return errores, {campo_id: crudas[campo_id] for campo_id in errores}, None


@login_required
def borrador_formulario_view(request, pk):
    ticket = _ticket_propio(request, pk)
    if ticket.estado != Ticket.Estado.BORRADOR:
        return redirect("tickets:detalle", pk=ticket.pk)
    version = ticket.respuesta_formulario.formulario_version

    if request.method == "POST":
        error_destino = _guardar_destino_general(request, ticket)
        errores, valores_con_error, error_general = _guardar_envio(request, ticket, version)
        if error_general:
            messages.error(request, error_general)
            return redirect("tickets:borrador", pk=ticket.pk)
        if not errores and not error_destino:
            messages.success(request, "Borrador guardado.")
            return redirect("tickets:borrador", pk=ticket.pk)
        messages.warning(
            request, "Guardamos lo que estaba correcto. Revisa los campos señalados para completarlos."
        )
        return _render_workspace(
            request, ticket, errores=errores, valores_envio=valores_con_error, error_destino=error_destino
        )

    return _render_workspace(request, ticket)


@login_required
@require_POST
def solicitud_estado_view(request, pk):
    """V2 — estado efectivo (visible/requerido) de cada campo para el envío
    actual, SIN guardar nada. El navegador solo aplica lo que responde el
    servidor (`validaciones.calcular_estados_efectivos`); no evalúa reglas."""
    ticket = _borrador_propio(request, pk)
    version = ticket.respuesta_formulario.formulario_version
    crudas = _leer_respuestas_de_request(request, version, con_archivos=False)
    estados = solicitud.estados_en_vivo(ticket, crudas)
    return JsonResponse({"campos": {str(campo_id): estado for campo_id, estado in estados.items()}})


def _errores_para_revisar(ticket):
    """Obligatoriedad y validez del estado YA guardado (`radicar_ticket`
    ejecuta exactamente esta validación)."""
    try:
        validar_para_radicar(ticket)
    except ValidationError as exc:
        return exc.message_dict if hasattr(exc, "error_dict") else {}
    return {}


@login_required
def revisar_view(request, pk):
    """V2 — "Revisa tu solicitud". POST: guarda el envío del formulario y, si
    todo está en orden, pasa a la revisión (PRG). GET: muestra lo guardado.
    Si hay campos por corregir o completar, no avanza: vuelve al formulario
    con el error junto a cada campo."""
    ticket = _ticket_propio(request, pk)
    if ticket.estado != Ticket.Estado.BORRADOR:
        return redirect("tickets:enviada", pk=ticket.pk) if ticket.radicado else redirect("tickets:detalle", pk=ticket.pk)
    version = ticket.respuesta_formulario.formulario_version

    errores_formato, valores_con_error = {}, {}
    error_destino = None
    if request.method == "POST":
        error_destino = _guardar_destino_general(request, ticket)
        errores_formato, valores_con_error, error_general = _guardar_envio(request, ticket, version)
        if error_general:
            messages.error(request, error_general)
            return redirect("tickets:borrador", pk=ticket.pk)
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    # Si un valor enviado tiene un error de formato, ese mensaje es el útil
    # (el campo quedó sin guardar, así que además figuraría como pendiente).
    errores = {**_errores_para_revisar(ticket), **errores_formato}
    # 4.C2: el destino también es parte de lo que se revisa antes de enviar.
    if direccionamiento.es_ticket_general(ticket):
        error_destino = error_destino or direccionamiento.error_de_destino(ticket)
    if errores or error_destino:
        messages.error(request, "Revisa los campos señalados antes de continuar.")
        return _render_workspace(
            request, ticket, errores=errores, valores_envio=valores_con_error, error_destino=error_destino
        )

    if request.method == "POST":
        return redirect("tickets:revisar", pk=ticket.pk)

    contexto = solicitud.contexto_workspace(ticket)
    contexto["titulo_pagina"] = f"Revisa tu solicitud — {contexto['servicio'].nombre}"
    return render(request, "tickets/solicitud_revision.html", contexto)


@login_required
@require_POST
def enviar_view(request, pk):
    """V2 — "Solicitar": la radicación REAL (`operaciones.radicar_ticket`)
    sobre lo ya guardado y revisado. Un segundo envío del mismo ticket (doble
    clic) no falla: lleva a la misma confirmación."""
    ticket = get_object_or_404(Ticket, pk=pk)
    if not es_propietario_borrador(request.user, ticket):
        raise PermissionDenied
    if ticket.estado != Ticket.Estado.BORRADOR:
        return redirect("tickets:enviada", pk=ticket.pk) if ticket.radicado else redirect("tickets:detalle", pk=ticket.pk)

    try:
        operaciones.radicar_ticket(ticket, request.user)
    except ValidationError as exc:
        errores = exc.message_dict if hasattr(exc, "error_dict") else None
        if errores:
            messages.error(request, "Revisa los campos señalados antes de solicitar.")
            return _render_workspace(request, ticket, errores=errores)
        messages.error(request, _mensaje_error(exc))
        return redirect("tickets:revisar", pk=ticket.pk)
    return redirect("tickets:enviada", pk=ticket.pk)


@login_required
@require_GET
def solicitud_enviada_view(request, pk):
    """V2 — confirmación de radicación con los datos reales del Ticket."""
    ticket = get_object_or_404(Ticket.objects.select_related("detalle_servicio__servicio__categoria"), pk=pk)
    if not es_propietario_borrador(request.user, ticket):
        raise PermissionDenied
    if ticket.estado == Ticket.Estado.BORRADOR:
        return redirect("tickets:borrador", pk=ticket.pk)
    contexto = {
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "titulo_pagina": "Solicitud enviada",
    }
    return render(request, "tickets/solicitud_enviada.html", contexto)


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

    Desde V2 la interfaz radica por el recorrido revisar → enviar
    (`revisar_view`/`enviar_view`); este endpoint directo conserva su
    contrato original.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = _borrador_propio(request, pk)

    version = ticket.respuesta_formulario.formulario_version
    respuestas_crudas = _leer_respuestas_de_request(request, version)

    error_destino = _guardar_destino_general(request, ticket)
    if error_destino:
        messages.error(request, error_destino)
        return redirect("tickets:borrador", pk=ticket.pk)
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
            messages.error(request, "Revisa los campos señalados antes de radicar.")
            return _render_workspace(request, ticket, errores=errores_por_campo)
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
        "puede_iniciar_atencion": puede_iniciar_atencion(request.user, ticket),
        "direccionamiento": direccionamiento.direccionamiento_de(ticket),
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
    contexto.update(_contexto_entrega(request.user, ticket))
    contexto.update(_contexto_prorrogas(request.user, ticket))
    return render(request, "tickets/detalle.html", contexto)


def _contexto_prorrogas(usuario, ticket):
    """4.A2 — compromiso temporal e historial de prórrogas del detalle. Solo
    presentación: qué se ofrece lo decide `autorizacion`, y cada operación vuelve
    a validarlo. La prórroga pertenece al Ticket: aquí se pide, se cancela y se
    consulta; el aprobador la resuelve en su pantalla de Aprobaciones."""
    historial_visible = puede_consultar_prorrogas(usuario, ticket)
    filas = []
    if historial_visible:
        for prorroga in ticket.prorrogas.select_related("solicitada_por", "resuelta_por"):
            aprobacion = aprobacion_pendiente_de_prorroga(prorroga) if puede_resolver_prorroga(usuario, prorroga) else None
            filas.append({
                "prorroga": prorroga,
                "puede_cancelar": puede_cancelar_prorroga(usuario, prorroga),
                "aprobacion_a_resolver": aprobacion,
            })
        filas.reverse()  # la más reciente primero
    puede_solicitar = puede_solicitar_prorroga(usuario, ticket)
    return {
        "compromiso": {
            "original": ticket.fecha_objetivo_original,
            "vigente": ticket.fecha_objetivo_vigente,
            "ampliado": ticket.fecha_objetivo_original is not None
            and ticket.fecha_objetivo_vigente != ticket.fecha_objetivo_original,
        },
        "prorrogas": filas,
        "muestra_prorrogas": ticket.fecha_objetivo_original is not None or bool(filas),
        "puede_solicitar_prorroga": puede_solicitar,
        "prorroga_form": SolicitudProrrogaForm() if puede_solicitar else None,
        "prorroga_sin_aprobacion": ticket.prorroga_politica == "SIN_APROBACION",
        "prorroga_no_disponible": motivo_no_elegible_para_prorroga(ticket)
        if es_responsable_actual(usuario, ticket) and ticket.estado == Ticket.Estado.EN_ATENCION
        else None,
    }


def _contexto_entrega(usuario, ticket):
    """4.5 — entregables del responsable, entregas históricas y respuesta del
    solicitante. Los entregables en producción solo se muestran a quien
    atiende (no al solicitante puro); las entregas realizadas, a cualquiera
    que pueda consultar el ticket."""
    puede_atender = puede_ver_en_cola(usuario, ticket)
    entregables, pendientes = [], []
    if puede_atender:
        entregables = list(ticket.entregables.prefetch_related("archivos"))
        for entregable in entregables:
            entregable.archivos_vigentes = [a for a in entregable.archivos.all() if a.retirado_en is None]
        pendientes = [e for e in entregables if e.obligatorio and not e.satisfecho]
    entregas = list(
        ticket.entregas.select_related("entregada_por", "resuelta_por")
        .prefetch_related("resultados__adjuntos")
        .order_by("-numero")
    )
    pendiente = next((e for e in entregas if e.estado == EntregaTicket.Estado.PENDIENTE), None)
    if pendiente is not None:
        pendiente.ticket = ticket
    return {
        "entregables": entregables,
        "entregables_pendientes": pendientes,
        "puede_escribir_entregables": puede_escribir_entregables_finales(usuario, ticket),
        "puede_entregar": puede_entregar_ticket(usuario, ticket),
        "entregas": entregas,
        "entrega_pendiente": pendiente,
        "puede_responder_entrega": pendiente is not None and puede_responder_entrega(usuario, pendiente),
        "tiene_entrega_formal": bool(ticket.entrega_politica),
    }


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


@login_required
def iniciar_atencion_view(request, pk):
    """4.C2 — iniciar la atención de un ticket ya dirigido a una persona."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    try:
        operaciones.iniciar_atencion_ticket(ticket, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Atención iniciada.")
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
def solicitar_prorroga_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    ticket = get_object_or_404(Ticket, pk=pk)
    form = SolicitudProrrogaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Indica una nueva fecha válida y el motivo de la prórroga.")
        return redirect("tickets:detalle", pk=pk)
    try:
        prorroga = prorrogas.solicitar_prorroga(
            ticket, request.user, nueva_fecha=form.cleaned_data["nueva_fecha"], motivo=form.cleaned_data["motivo"]
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        if prorroga.estado == ProrrogaTicket.Estado.APROBADA:
            messages.success(request, "Prórroga aplicada: la fecha objetivo vigente ya es la nueva.")
        else:
            messages.success(request, "Prórroga enviada para aprobación.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def cancelar_prorroga_view(request, pk, prorroga_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    prorroga = get_object_or_404(ProrrogaTicket, pk=prorroga_id, ticket_id=pk)
    form = CancelacionProrrogaForm(request.POST)
    try:
        prorrogas.cancelar_prorroga(
            prorroga, request.user, motivo=form.cleaned_data.get("motivo", "") if form.is_valid() else ""
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Prórroga cancelada.")
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
    # 4.5: un archivo retirado después de haber sido ENTREGADO sigue
    # disponible — forma parte de la historia de una entrega.
    en_entrega = Exists(ResultadoEntregaTicket.adjuntos.through.objects.filter(adjunto_id=OuterRef("pk")))
    adjunto = get_object_or_404(
        Adjunto.objects.filter(Q(retirado_en__isnull=True) | en_entrega).select_related(
            "ticket",
            "comentario__ticket",
            "solicitud__ticket",
            "respuesta_solicitud__solicitud__ticket",
            "entregable__ticket",
        ),
        pk=adjunto_id,
    )
    ticket = adjunto.ticket_relacionado
    if not puede_consultar_ticket(request.user, ticket):
        raise PermissionDenied
    return FileResponse(adjunto.archivo.open("rb"), as_attachment=True, filename=adjunto.nombre_original)


# --- 4.5 — Entregables del responsable, entrega formal y respuesta ---------


def _post_ticket_o_405(request, pk):
    if request.method != "POST":
        return None
    return get_object_or_404(Ticket, pk=pk)


@login_required
def entregable_resultado_view(request, pk, entregable_id):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    entregable = get_object_or_404(EntregableTicket, pk=entregable_id, ticket=ticket)
    try:
        entregables_ops.registrar_resultado_entregable(entregable, request.user, request.POST.get("valor", ""))
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Resultado guardado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def entregable_confirmar_view(request, pk, entregable_id):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    entregable = get_object_or_404(EntregableTicket, pk=entregable_id, ticket=ticket)
    try:
        entregables_ops.confirmar_entregable(entregable, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Entregable confirmado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def entregable_adjuntar_view(request, pk, entregable_id):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    entregable = get_object_or_404(EntregableTicket, pk=entregable_id, ticket=ticket)
    archivo = request.FILES.get("archivo")
    if archivo is None:
        messages.error(request, "Seleccione un archivo.")
        return redirect("tickets:detalle", pk=pk)
    try:
        entregables_ops.adjuntar_archivo_entregable(entregable, request.user, archivo)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Archivo adjuntado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def entregable_retirar_archivo_view(request, pk, adjunto_id):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    adjunto = get_object_or_404(Adjunto, pk=adjunto_id, entregable__ticket=ticket)
    try:
        entregables_ops.retirar_archivo_entregable(adjunto, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Archivo retirado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def entregar_view(request, pk):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    # Acción irreversible: se exige confirmación explícita en el envío.
    if request.POST.get("confirmar") != "1":
        messages.error(request, "Confirme la entrega para continuar.")
        return redirect("tickets:detalle", pk=pk)
    try:
        entrega = entregas_ops.entregar_ticket(ticket, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        if entrega.estado == EntregaTicket.Estado.CERRADA_SIN_RESPUESTA:
            messages.success(request, "Resultado entregado. El ticket quedó cerrado.")
        else:
            messages.success(request, "Resultado entregado al solicitante.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def aceptar_entrega_view(request, pk):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    try:
        entregas_ops.aceptar_entrega(ticket, request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Gracias: el ticket quedó cerrado.")
    return redirect("tickets:detalle", pk=pk)


@login_required
def observar_entrega_view(request, pk):
    ticket = _post_ticket_o_405(request, pk)
    if ticket is None:
        return HttpResponseNotAllowed(["POST"])
    try:
        entregas_ops.observar_entrega(ticket, request.user, request.POST.get("observaciones", ""))
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Observaciones enviadas. El responsable realizará los ajustes.")
    return redirect("tickets:detalle", pk=pk)
