"""Administración del Ticket General (4.C1) — acciones de la tarjeta "Ticket
general" de Diseñador › Servicios. Solo HTTP: habilitar, crear el Servicio
interno o elegir uno existente. Toda regla vive en `apps.catalogo.ticket_general`
y exige `catalogo.administrar` también allí; el resto de la configuración
(formulario, tiempo objetivo, prórroga, entrega, visibilidad) se edita en el
Studio normal de ese Servicio, sin una segunda pantalla. 4.C2 añade las acciones
de los destinos (crear, cambiar responsable, activar/desactivar, destino de
reserva), con las mismas reglas en `apps.catalogo.destinos_ticket_general`."""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect

from apps.catalogo import destinos_ticket_general as destinos
from apps.catalogo import ticket_general
from apps.catalogo.models import Categoria, DestinoTicketGeneral, Servicio
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import Area, Equipo


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _entrada(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not usuario_tiene_permiso(request.user, "catalogo.administrar"):
        raise PermissionDenied
    return None


def _volver():
    return redirect("core:disenador_servicios")


@login_required
def habilitar_view(request):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    habilitar = request.POST.get("habilitado") == "1"
    try:
        ticket_general.configurar_habilitacion(request.user, habilitado=habilitar)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ticket general habilitado." if habilitar else "Ticket general deshabilitado.")
    return _volver()


@login_required
def crear_view(request):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    categoria = Categoria.objects.filter(pk=request.POST.get("categoria") or None).first()
    try:
        servicio = ticket_general.crear_servicio_ticket_general(
            request.user, categoria=categoria, nombre=request.POST.get("nombre", "")
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return _volver()
    messages.success(
        request,
        "Servicio interno creado. Completa su formulario y publícalo; luego habilita el ticket general.",
    )
    return redirect("core:disenador_servicio", pk=servicio.pk)


@login_required
def designar_view(request):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    servicio = get_object_or_404(Servicio, pk=request.POST.get("servicio") or 0)
    try:
        ticket_general.designar_servicio_ticket_general(servicio, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return _volver()
    messages.success(
        request, "Servicio interno elegido. El ticket general queda deshabilitado hasta que lo valides y habilites."
    )
    return _volver()


# --- 4.C2 — destinos -------------------------------------------------------------


def _por_id(modelo, valor, mensaje):
    try:
        objeto = modelo.objects.filter(pk=int(valor)).first()
    except (TypeError, ValueError):
        objeto = None
    if objeto is None:
        raise ValidationError(mensaje)
    return objeto


def _responsable_de(post):
    """El selector único de responsable viaja como `USUARIO:<id>` o `EQUIPO:<id>`;
    vacío = que lo sea el propio destino (solo EQUIPO/PERSONA)."""
    valor = (post.get("responsable") or "").strip()
    if not valor:
        return None
    clase, _, ident = valor.partition(":")
    modelo = {"USUARIO": get_user_model(), "EQUIPO": Equipo}.get(clase)
    if modelo is None:
        raise ValidationError("Elige un responsable válido.")
    return _por_id(modelo, ident, "Elige un responsable válido.")


def _ejecutar(request, accion, mensaje_exito):
    try:
        accion()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, mensaje_exito)
    return _volver()


@login_required
def destino_crear_view(request):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    tipo = request.POST.get("tipo")
    modelo = {
        DestinoTicketGeneral.Tipo.AREA: ("objeto_area", Area),
        DestinoTicketGeneral.Tipo.EQUIPO: ("objeto_equipo", Equipo),
        DestinoTicketGeneral.Tipo.USUARIO: ("objeto_usuario", get_user_model()),
    }.get(tipo)

    def crear():
        if modelo is None:
            raise ValidationError("Elige si el destino es un área, un equipo o una persona.")
        campo, clase = modelo
        objeto = _por_id(clase, request.POST.get(campo), "Elige el destino.")
        destinos.crear_destino(request.user, tipo=tipo, objeto=objeto, responsable=_responsable_de(request.POST))

    return _ejecutar(request, crear, "Destino creado.")


@login_required
def destino_responsable_view(request, pk):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    destino = get_object_or_404(DestinoTicketGeneral, pk=pk)

    def cambiar():
        responsable = _responsable_de(request.POST)
        if responsable is None:
            raise ValidationError("Elige quién atenderá los tickets de este destino.")
        destinos.cambiar_responsable(request.user, destino, responsable=responsable)

    return _ejecutar(request, cambiar, "Responsable actualizado. Los tickets ya radicados no cambian.")


@login_required
def destino_estado_view(request, pk):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    destino = get_object_or_404(DestinoTicketGeneral, pk=pk)
    activar = request.POST.get("activo") == "1"
    return _ejecutar(
        request,
        lambda: (destinos.activar_destino if activar else destinos.desactivar_destino)(request.user, destino),
        "Destino activado." if activar else "Destino desactivado. Los tickets ya radicados no cambian.",
    )


@login_required
def destino_predeterminado_view(request):
    if (rechazo := _entrada(request)) is not None:
        return rechazo
    valor = request.POST.get("destino")

    def definir():
        destino = _por_id(DestinoTicketGeneral, valor, "Elige un destino.") if valor else None
        destinos.definir_predeterminado(request.user, destino)

    return _ejecutar(
        request, definir, "Destino de reserva definido." if valor else "Sin destino de reserva: el destino será obligatorio."
    )
