"""Acciones de gacetas — Fase 3. La gaceta se VE dentro del espacio del ticket (galería de piezas, visor y menú
«Coordinar»; `tickets:espacio`); la página propia de la gaceta se retiró y su URL redirige allí. Aquí quedan solo
las acciones (observar, responder, reenviar, excluir, presentar, cerrar el periodo, resolver): cada una es la
operación de dominio, que revalida permisos y estado bajo lock, y vuelve a la pantalla de origen (`next`) o a la
gaceta dentro del ticket."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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


def _en_el_ticket(gaceta):
    """La gaceta dentro del espacio de su ticket (su única vista)."""
    return reverse("tickets:espacio", args=[gaceta.ticket_id]) + f"#gaceta-{gaceta.pk}"


def _ejecutar(request, gaceta_pk, accion, exito):
    try:
        accion()
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, _mensaje(exc))
    else:
        messages.success(request, exito)
    destino = request.POST.get("next") or ""
    if destino.startswith("/"):
        return volver_a(request, "tickets:mis_tickets")
    return redirect(_en_el_ticket(GacetaTicket.objects.only("pk", "ticket_id").get(pk=gaceta_pk)))


@login_required
@require_GET
def lista_view(request):
    gacetas = consultas.gacetas_visibles_para(request.user)
    return render(request, "gacetas/lista.html", {"gacetas": gacetas, "titulo_pagina": "Gacetas en revisión"})


@login_required
@require_GET
def detalle_view(request, pk):
    """La página propia de la gaceta se retiró: enlaces viejos y avisos llevan a la gaceta dentro del ticket (la
    misma autorización: quien consulta la gaceta entra al espacio por su relación con ella)."""
    gaceta = _gaceta_visible(request, pk)
    return redirect(_en_el_ticket(gaceta))


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
