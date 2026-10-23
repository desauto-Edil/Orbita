"""Pantallas de gacetas — Fase 3. Una sola página por gaceta reúne la revisión: ítems, evidencias, versiones,
observaciones, correcciones y resultados. Las DECISIONES (aprobar, devolver, rechazar, excluir) se registran en la
pantalla de la aprobación de siempre (`aprobaciones:detalle`), no aquí: no se duplica la operación. Cada acción es
la operación de dominio, que revalida permisos y estado bajo lock."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.core.redirecciones import volver_a
from apps.gacetas import autorizacion, consultas, operaciones
from apps.gacetas.models import GacetaTicket, ObservacionGaceta, ParticipacionItemGaceta
from apps.tickets.models import ElementoParrilla


def _mensaje(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _gaceta_visible(request, pk):
    gaceta = get_object_or_404(GacetaTicket.objects.select_related("ticket__detalle_servicio__servicio"), pk=pk)
    if not autorizacion.puede_consultar_gaceta(request.user, gaceta):
        raise PermissionDenied
    return gaceta


def _ejecutar(request, gaceta_pk, accion, exito):
    try:
        accion()
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje(exc))
    else:
        messages.success(request, exito)
    return volver_a(request, "gacetas:detalle", gaceta_pk)


@login_required
@require_GET
def lista_view(request):
    gacetas = consultas.gacetas_visibles_para(request.user)
    return render(request, "gacetas/lista.html", {"gacetas": gacetas, "titulo_pagina": "Gacetas en revisión"})


@login_required
@require_GET
def detalle_view(request, pk):
    gaceta = _gaceta_visible(request, pk)
    contexto = consultas.detalle(request.user, gaceta)
    contexto["titulo_pagina"] = f"{gaceta.nombre} — {gaceta.ticket.codigo or gaceta.ticket.pk}"
    return render(request, "gacetas/detalle.html", contexto)


@login_required
@require_POST
def presentar_view(request, pk):
    gaceta = _gaceta_visible(request, pk)
    elemento = get_object_or_404(ElementoParrilla, pk=request.POST.get("elemento_id"), parrilla__ticket=gaceta.ticket)
    return _ejecutar(request, pk, lambda: operaciones.presentar(gaceta, elemento, request.user),
                     f"#{elemento.numero} presentado a la gaceta.")


def _participacion(request, pk):
    participacion = get_object_or_404(ParticipacionItemGaceta.objects.select_related("gaceta"), pk=pk)
    if not autorizacion.puede_consultar_gaceta(request.user, participacion.gaceta):
        raise PermissionDenied
    return participacion


@login_required
@require_POST
def observar_view(request, pk):
    participacion = _participacion(request, pk)
    return _ejecutar(request, participacion.gaceta_id,
                     lambda: operaciones.observar(participacion, request.user, request.POST.get("texto", "")),
                     "Observación registrada: el ítem vuelve a su responsable para corregirlo.")


@login_required
@require_POST
def responder_view(request, pk):
    observacion = get_object_or_404(ObservacionGaceta.objects.select_related("participacion__gaceta"), pk=pk)
    if not autorizacion.puede_consultar_gaceta(request.user, observacion.participacion.gaceta):
        raise PermissionDenied
    return _ejecutar(request, observacion.participacion.gaceta_id,
                     lambda: operaciones.responder_observacion(observacion, request.user, request.POST.get("respuesta", "")),
                     "Respuesta registrada.")


@login_required
@require_POST
def reenviar_view(request, pk):
    participacion = _participacion(request, pk)
    return _ejecutar(request, participacion.gaceta_id,
                     lambda: operaciones.reenviar(participacion, request.user, request.POST.get("comentario", "")),
                     "Corrección reenviada: se registró una nueva versión y vuelve a revisión.")


@login_required
@require_POST
def excluir_view(request, pk):
    participacion = _participacion(request, pk)
    return _ejecutar(request, participacion.gaceta_id,
                     lambda: operaciones.excluir_directo(participacion, request.user, request.POST.get("justificacion", "")),
                     "Ítem excluido del alcance.")


@login_required
@require_POST
def cerrar_periodo_view(request, pk):
    gaceta = _gaceta_visible(request, pk)
    return _ejecutar(request, pk, lambda: operaciones.cerrar_periodo(gaceta, request.user),
                     "Periodo de revisión cerrado.")


@login_required
@require_POST
def resolver_view(request, pk):
    gaceta = _gaceta_visible(request, pk)
    return _ejecutar(request, pk, lambda: operaciones.resolver(gaceta, request.user, request.POST.get("justificacion", "")),
                     "Gaceta resuelta.")
