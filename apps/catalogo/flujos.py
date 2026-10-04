"""Disenador de Flujos: editor unico de plantillas de fases.

En D3 el Workflow reutilizable deja de ser una cadena ejecutable de bloques
operativos. Aqui se administran solamente las fases compartidas; la
configuracion operativa vive en cada Servicio/Proceso.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.catalogo.ejecucion import (
    crear_flujo,
    duplicar_flujo,
    preparar_borrador_de_flujo,
    publicar_flujo,
    servicios_que_comparten,
)
from apps.catalogo.models import Servicio
from apps.core import disenador
from apps.workflows.autorizacion import puede_administrar_workflows
from apps.workflows.fases import (
    agregar_fase,
    conectar_fases,
    editar_fase,
    eliminar_fase,
    eliminar_transicion_fase,
    validar_estructura_fases,
)
from apps.workflows.forms import WorkflowForm
from apps.workflows.models import FaseWorkflow, TransicionFaseWorkflow, Workflow, WorkflowVersion
from apps.workflows.versionamiento import editar_workflow


class FaseForm(forms.Form):
    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea, label="Descripcion")
    orden = forms.IntegerField(required=False, min_value=1, label="Orden")


class TransicionFaseForm(forms.Form):
    fase_destino = forms.ModelChoiceField(queryset=FaseWorkflow.objects.none(), label="Fase destino")
    nombre = forms.CharField(max_length=150, required=False, label="Nombre")
    prioridad = forms.IntegerField(required=False, min_value=0, initial=0, label="Prioridad")

    def __init__(self, *args, fases_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fases_queryset is not None:
            self.fields["fase_destino"].queryset = fases_queryset


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _exigir_administrar(request):
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied


def _solo_post(request):
    return None if request.method == "POST" else HttpResponseNotAllowed(["POST"])


@login_required
def nuevo_view(request):
    _exigir_administrar(request)
    if request.method == "POST":
        form = WorkflowForm(request.POST)
        if form.is_valid():
            try:
                workflow = crear_flujo(
                    request.user,
                    nombre=form.cleaned_data["nombre"],
                    descripcion=form.cleaned_data.get("descripcion", ""),
                )
            except ValidationError as exc:
                form.add_error(None, _mensaje_error(exc))
            else:
                messages.success(request, "Flujo creado. Empieza agregando sus fases.")
                return redirect("flujos:lienzo", pk=workflow.pk)
    else:
        form = WorkflowForm()
    return render(request, "flujos/nuevo.html", {"form": form, "titulo_pagina": "Nuevo flujo"})


def _fase_iniciales_finales(version):
    if version is None:
        return set(), set()
    iniciales = {fase.pk for fase in version.fases.all() if not fase.transiciones_entrantes.exists()}
    finales = {fase.pk for fase in version.fases.all() if not fase.transiciones_salientes.exists()}
    return iniciales, finales


def _fases_para_presentacion(version, editable):
    if version is None:
        return []
    fases = list(
        version.fases.prefetch_related("transiciones_salientes__fase_destino", "transiciones_entrantes").order_by(
            "orden", "pk"
        )
    )
    iniciales, finales = _fase_iniciales_finales(version)
    resultado = []
    for fase in fases:
        destinos = FaseWorkflow.objects.filter(version=version).exclude(pk=fase.pk).order_by("orden", "pk")
        resultado.append(
            {
                "fase": fase,
                "es_inicial": fase.pk in iniciales,
                "es_final": fase.pk in finales,
                "transiciones": list(
                    fase.transiciones_salientes.select_related("fase_destino").order_by("prioridad", "pk")
                ),
                "form_editar": FaseForm(
                    initial={"nombre": fase.nombre, "descripcion": fase.descripcion, "orden": fase.orden},
                    prefix=f"fase-{fase.pk}-editar",
                )
                if editable
                else None,
                "form_transicion": TransicionFaseForm(
                    fases_queryset=destinos,
                    prefix=f"fase-{fase.pk}-transicion",
                )
                if editable
                else None,
            }
        )
    return resultado


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
    editable = (
        mostrada is not None
        and mostrada.estado == WorkflowVersion.Estado.BORRADOR
        and caps["administrar_flujos"]
    )

    usado_por = list(servicios_que_comparten(workflow))
    errores, publicable = [], False
    if borrador is not None:
        errores = validar_estructura_fases(borrador)
        publicable = not errores

    contexto = {
        "caps": caps,
        "workflow": workflow,
        "versiones": versiones,
        "version_activa": activa,
        "version_borrador": borrador,
        "version_mostrada": mostrada,
        "viendo_otra": pedida is not None and pedida != (borrador or activa),
        "editable_ejecucion": editable,
        "usado_por": usado_por,
        "errores_publicacion": errores,
        "publicable": publicable and caps["administrar_flujos"],
        "fases": _fases_para_presentacion(mostrada, editable),
        "fase_form_nueva": FaseForm(prefix="fase-nueva") if editable else None,
        "form_datos": WorkflowForm(initial={"nombre": workflow.nombre, "descripcion": workflow.descripcion}),
        "titulo_pagina": workflow.nombre,
    }
    servicio_retorno_id = request.session.get(f"retorno_servicio_flujo_{workflow.pk}")
    servicio_retorno = None
    if servicio_retorno_id and caps.get("gestionar_servicios"):
        servicio_retorno = Servicio.objects.filter(pk=servicio_retorno_id).first()
    if servicio_retorno is not None:
        contexto["servicio_retorno"] = servicio_retorno
        contexto["servicio_retorno_url"] = (
            f"{reverse('catalogo:studio', args=[servicio_retorno.pk])}?tab=ejecucion&plantilla={workflow.pk}"
        )
    return contexto


@login_required
def lienzo_view(request, pk):
    caps = disenador.capacidades(request.user)
    if not caps["consultar_flujos"]:
        raise PermissionDenied
    workflow = get_object_or_404(
        Workflow.objects.select_related("version_activa"),
        pk=pk,
        modo=Workflow.Modo.PLANTILLA_FASES,
    )
    return render(request, "flujos/lienzo.html", _contexto_lienzo(request, workflow, caps))


@login_required
def preparar_view(request, pk):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    try:
        preparar_borrador_de_flujo(
            workflow,
            request.user,
            confirmar_compartido=request.POST.get("confirmo_compartido") == "1",
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
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
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
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, f"Version {borrador.numero} publicada.")
    return redirect("flujos:lienzo", pk=pk)


@login_required
def copia_view(request, pk):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    try:
        copia = duplicar_flujo(workflow, request.user, nombre=request.POST.get("nombre", ""))
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect("flujos:lienzo", pk=pk)
    messages.success(request, f"Copia creada: {copia.nombre}.")
    return redirect("flujos:lienzo", pk=copia.pk)


@login_required
def datos_view(request, pk):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    form = WorkflowForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revisa el nombre del flujo.")
    else:
        try:
            editar_workflow(
                workflow,
                request.user,
                nombre=form.cleaned_data["nombre"].strip(),
                descripcion=form.cleaned_data.get("descripcion", ""),
            )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Datos del flujo actualizados.")
    return redirect("flujos:lienzo", pk=pk)


def _version_borrador_o_error(workflow):
    version = workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()
    if version is None:
        raise ValidationError("No hay un borrador editable.")
    return version


@login_required
def fase_guardar_view(request, pk, fase_id=None):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    try:
        version = _version_borrador_o_error(workflow)
        if fase_id is None:
            form = FaseForm(request.POST, prefix="fase-nueva")
            if not form.is_valid():
                raise ValidationError("Revise los datos de la fase.")
            agregar_fase(
                version,
                request.user,
                nombre=form.cleaned_data["nombre"],
                descripcion=form.cleaned_data.get("descripcion", ""),
                orden=form.cleaned_data.get("orden") or None,
            )
            messages.success(request, "Fase agregada.")
        else:
            fase = get_object_or_404(FaseWorkflow, pk=fase_id, version=version)
            form = FaseForm(request.POST, prefix=f"fase-{fase_id}-editar")
            if not form.is_valid():
                raise ValidationError("Revise los datos de la fase.")
            editar_fase(
                fase,
                request.user,
                nombre=form.cleaned_data["nombre"],
                descripcion=form.cleaned_data.get("descripcion", ""),
                orden=form.cleaned_data.get("orden") or None,
            )
            messages.success(request, "Fase actualizada.")
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    return redirect("flujos:lienzo", pk=pk)


@login_required
def fase_eliminar_view(request, pk, fase_id):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    try:
        version = _version_borrador_o_error(workflow)
        fase = get_object_or_404(FaseWorkflow, pk=fase_id, version=version)
        eliminar_fase(fase, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Fase eliminada.")
    return redirect("flujos:lienzo", pk=pk)


@login_required
def fase_conectar_view(request, pk, fase_id):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    try:
        version = _version_borrador_o_error(workflow)
        origen = get_object_or_404(FaseWorkflow, pk=fase_id, version=version)
        destinos = FaseWorkflow.objects.filter(version=version).exclude(pk=origen.pk)
        form = TransicionFaseForm(request.POST, fases_queryset=destinos, prefix=f"fase-{fase_id}-transicion")
        if not form.is_valid():
            raise ValidationError("Revise la conexion entre fases.")
        conectar_fases(
            origen,
            form.cleaned_data["fase_destino"],
            request.user,
            nombre=form.cleaned_data.get("nombre", ""),
            prioridad=form.cleaned_data.get("prioridad") or 0,
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Fases conectadas.")
    return redirect("flujos:lienzo", pk=pk)


@login_required
def fase_desconectar_view(request, pk, transicion_id):
    if (respuesta := _solo_post(request)) is not None:
        return respuesta
    _exigir_administrar(request)
    workflow = get_object_or_404(Workflow, pk=pk, modo=Workflow.Modo.PLANTILLA_FASES)
    try:
        version = _version_borrador_o_error(workflow)
        transicion = get_object_or_404(TransicionFaseWorkflow, pk=transicion_id, fase_origen__version=version)
        eliminar_transicion_fase(transicion, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Conexion eliminada.")
    return redirect("flujos:lienzo", pk=pk)


def _redirigir_editor_antiguo(request, pk):
    messages.info(request, "El editor de Flujos ahora administra fases; los bloques se configuran en cada servicio.")
    return redirect("flujos:lienzo", pk=pk)


@login_required
def bloque_guardar_view(request, pk, bloque_id=None):
    return _redirigir_editor_antiguo(request, pk)


@login_required
def bloque_eliminar_view(request, pk, bloque_id):
    return _redirigir_editor_antiguo(request, pk)


@login_required
def ruta_aprobacion_view(request, pk, bloque_id):
    return _redirigir_editor_antiguo(request, pk)


@login_required
def condicional_guardar_view(request, pk, bloque_id, condicional_id=None):
    return _redirigir_editor_antiguo(request, pk)


@login_required
def condicional_eliminar_view(request, pk, bloque_id, condicional_id):
    return _redirigir_editor_antiguo(request, pk)


@login_required
def fallback_guardar_view(request, pk, bloque_id):
    return _redirigir_editor_antiguo(request, pk)
