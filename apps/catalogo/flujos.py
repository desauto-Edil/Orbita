"""Diseñador de Flujos (Fase D2) — la sección Flujos del Diseñador.

Un Flujo es un `Workflow` REAL de la biblioteca (definición reutilizable) con
versiones inmutables: aquí se crea, se diseña (lienzo de bloques
empresariales), se versiona, se publica y se copia, SIN pasar por un
Servicio. No hay ningún modelo, motor ni concepto nuevo: se coordinan las
operaciones de `apps.catalogo.ejecucion` (variantes `*_en_flujo`, `crear_flujo`,
`preparar_borrador_de_flujo`, `publicar_flujo`, `duplicar_flujo`), que delegan
en el editor y el versionamiento de `apps.workflows` ya existentes.

Autorización (nunca por rol):
  ver el lienzo, versiones y «usado por»   `workflows.consultar|administrar`
  crear, editar, versionar, publicar, copiar  `workflows.administrar`
  (`workflows.vincular` no abre esta pantalla: vincular es elegir un flujo
  PUBLICADO desde un Servicio, no ver ni modificar su definición.)

Los bloques se editan con los MISMOS manejadores que Studio
(`studio._h_*`): una sola regla de negocio y una sola presentación de bloques
(`catalogo/_ejecucion_bloques.html`); solo cambia el ancla (`_AnclaFlujo`).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.catalogo import studio
from apps.catalogo.ejecucion import (
    crear_flujo,
    duplicar_flujo,
    preparar_borrador_de_flujo,
    publicar_flujo,
    servicios_que_comparten,
)
from apps.core import disenador
from apps.workflows.autorizacion import puede_administrar_workflows
from apps.workflows.forms import WorkflowForm
from apps.workflows.models import Workflow, WorkflowVersion
from apps.workflows.validacion import validar_estructura
from apps.workflows.versionamiento import editar_workflow


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _exigir_administrar(request):
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied


def _solo_post(request):
    return None if request.method == "POST" else HttpResponseNotAllowed(["POST"])


def _ancla_urls_flujo(workflow):
    """URLs de la lista de bloques cuando el ancla es un Flujo (ver
    `studio._ancla_urls_servicio`, su equivalente para Servicios)."""
    base = reverse("flujos:lienzo", args=[workflow.pk])
    return {
        "pk": workflow.pk,
        "url_guardar_nuevo": "flujos:bloque_crear",
        "url_editar": "flujos:bloque_editar",
        "url_eliminar": "flujos:bloque_eliminar",
        "url_ruta": "flujos:bloque_ruta",
        "url_cond_crear": "flujos:cond_crear",
        "url_cond_editar": "flujos:cond_editar",
        "url_cond_eliminar": "flujos:cond_eliminar",
        "url_fallback": "flujos:fallback",
        "pagina": base,
        "pagina_despues": f"{base}?despues=",
    }


# --- Nuevo flujo ------------------------------------------------------------


@login_required
def nuevo_view(request):
    _exigir_administrar(request)
    if request.method == "POST":
        form = WorkflowForm(request.POST)
        if form.is_valid():
            try:
                workflow = crear_flujo(
                    request.user, nombre=form.cleaned_data["nombre"],
                    descripcion=form.cleaned_data.get("descripcion", ""),
                )
            except ValidationError as exc:
                form.add_error(None, _mensaje_error(exc))
            else:
                messages.success(request, "Flujo creado. Empieza agregando sus bloques.")
                return redirect("flujos:lienzo", pk=workflow.pk)
    else:
        form = WorkflowForm()
    return render(request, "flujos/nuevo.html", {"form": form, "titulo_pagina": "Nuevo flujo"})


# --- Lienzo -------------------------------------------------------------------


def _contexto_lienzo(request, workflow, caps):
    versiones = list(workflow.versiones.order_by("-numero"))
    activa = workflow.version_activa
    borrador = next((v for v in versiones if v.estado == WorkflowVersion.Estado.BORRADOR), None)
    pedida = None
    try:
        pedida = next((v for v in versiones if v.pk == int(request.GET.get("version", ""))), None)
    except ValueError:
        pass
    mostrada = pedida or borrador or activa or (versiones[0] if versiones else None)
    editable = mostrada if (
        mostrada is not None and mostrada.estado == WorkflowVersion.Estado.BORRADOR and caps["administrar_flujos"]
    ) else None

    contexto = studio._contexto_bloques(editable, mostrada, request.GET.get("despues"))
    usado_por = list(servicios_que_comparten(workflow))

    errores, publicable = [], False
    if borrador is not None:
        crudos = validar_estructura(borrador)
        publicable = not crudos
        errores = [m for m in (studio._traducir_error_ejecucion(e) for e in crudos) if m]
        if crudos and not errores:
            errores = ["La estructura del flujo todavía no está completa."]

    contexto.update(
        {
            "caps": caps,
            "workflow": workflow,
            "versiones": versiones,
            "version_activa": activa,
            "version_borrador": borrador,
            "version_mostrada": mostrada,
            "viendo_otra": pedida is not None and pedida != (borrador or activa),
            "editable_ejecucion": editable is not None,
            "usado_por": usado_por,
            "errores_publicacion": errores,
            "publicable": publicable and caps["administrar_flujos"],
            "ancla": _ancla_urls_flujo(workflow),
            "form_datos": WorkflowForm(initial={"nombre": workflow.nombre, "descripcion": workflow.descripcion}),
            "titulo_pagina": workflow.nombre,
        }
    )
    return contexto


@login_required
def lienzo_view(request, pk):
    caps = disenador.capacidades(request.user)
    if not caps["consultar_flujos"]:
        raise PermissionDenied
    workflow = get_object_or_404(Workflow.objects.select_related("version_activa"), pk=pk)
    return render(request, "flujos/lienzo.html", _contexto_lienzo(request, workflow, caps))


# --- Ciclo de vida: abrir borrador, publicar, copiar, datos ------------------------


@login_required
def preparar_view(request, pk):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk)
    try:
        preparar_borrador_de_flujo(
            workflow, request.user, confirmar_compartido=request.POST.get("confirmo_compartido") == "1"
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Borrador listo para editar.")
    return redirect("flujos:lienzo", pk=pk)


@login_required
def publicar_view(request, pk):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk)
    borrador = workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()
    if borrador is None:
        messages.error(request, "No hay un borrador para publicar.")
        return redirect("flujos:lienzo", pk=pk)
    confirmado = (
        request.POST.get("confirmo_compartido") == "1"
        and request.POST.get("confirmacion_nombre", "").strip() == workflow.nombre
    )
    try:
        publicar_flujo(workflow, borrador, request.user, confirmar_impacto_compartido=confirmado)
    except ValidationError as exc:
        mensajes = [m for m in (studio._traducir_error_ejecucion(msg) for msg in exc.messages) if m] or exc.messages
        messages.error(request, "; ".join(mensajes))
    else:
        messages.success(request, f"Versión {borrador.numero} publicada.")
    return redirect("flujos:lienzo", pk=pk)


@login_required
def copia_view(request, pk):
    """Crea un Workflow independiente a partir de este: el original y los
    servicios vinculados a él no cambian."""
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk)
    try:
        copia = duplicar_flujo(workflow, request.user, nombre=request.POST.get("nombre", ""))
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("flujos:lienzo", pk=pk)
    messages.success(
        request,
        f"Copia creada: «{copia.nombre}». Es un flujo independiente; el original no cambió.",
    )
    return redirect("flujos:lienzo", pk=copia.pk)


@login_required
def datos_view(request, pk):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk)
    form = WorkflowForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revisa el nombre del flujo.")
    else:
        try:
            editar_workflow(
                workflow, request.user, nombre=form.cleaned_data["nombre"].strip(),
                descripcion=form.cleaned_data.get("descripcion", ""),
            )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Datos del flujo actualizados.")
    return redirect("flujos:lienzo", pk=pk)


# --- Bloques (mismos manejadores que Studio, con el Flujo como ancla) ---------------


def _con_ancla_de_flujo(manejador):
    def vista(request, pk, *args, **kwargs):
        if (respuesta := _solo_post(request)) is not None:
            return respuesta
        _exigir_administrar(request)
        ancla = studio._AnclaFlujo(get_object_or_404(Workflow, pk=pk))
        return manejador(request, ancla, *args, **kwargs)

    return vista


@login_required
def bloque_guardar_view(request, pk, bloque_id=None):
    return _con_ancla_de_flujo(studio._h_bloque_guardar)(request, pk, bloque_id)


@login_required
def bloque_eliminar_view(request, pk, bloque_id):
    return _con_ancla_de_flujo(studio._h_bloque_eliminar)(request, pk, bloque_id)


@login_required
def ruta_aprobacion_view(request, pk, bloque_id):
    return _con_ancla_de_flujo(studio._h_ruta_aprobacion_guardar)(request, pk, bloque_id)


@login_required
def condicional_guardar_view(request, pk, bloque_id, condicional_id=None):
    return _con_ancla_de_flujo(studio._h_condicional_guardar)(request, pk, bloque_id, condicional_id)


@login_required
def condicional_eliminar_view(request, pk, bloque_id, condicional_id):
    return _con_ancla_de_flujo(studio._h_condicional_eliminar)(request, pk, bloque_id, condicional_id)


@login_required
def fallback_guardar_view(request, pk, bloque_id):
    return _con_ancla_de_flujo(studio._h_fallback_guardar)(request, pk, bloque_id)
