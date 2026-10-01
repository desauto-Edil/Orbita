"""Vistas de Workflows — 3.UI.3 (CU-020, "Diseñar y versionar workflow",
RQF-063/064/069/070).

Interfaz propia de Órbita para administrar el CONTENEDOR (`Workflow`) y su
versionamiento (`WorkflowVersion`) — reemplaza al ítem de navegación
"Workflows" que hasta ahora apuntaba directo a Django Admin (que sigue
existiendo como herramienta técnica, sin cambios). Mismo patrón
function-based/PRG/autorización server-side ya validado en 3.UI.1/3.UI.2.

**Alcance explícito de este incremento — solo lectura de la definición
interna de una versión** (Etapas/Transiciones se muestran, nunca se crean/
editan/eliminan desde aquí: eso es el editor, un incremento posterior). Se
exponen únicamente las operaciones de dominio ya existentes o agregadas en
este mismo incremento: `crear_workflow`, `editar_workflow`,
`crear_nueva_version`, `activar_version` — la vista nunca reimplementa
clonado ni reglas de validación, solo invoca la API pública y presenta lo
que el dominio devuelve.

**Autorización** — mismo patrón estricto que 3.UI.1/3.UI.2: cada vista de
acción vuelve a comprobar `puede_administrar_workflows`/
`puede_consultar_workflows` antes de operar, dejando propagar
`PermissionDenied` sin capturar. `crear_workflow`/`editar_workflow`/
`crear_nueva_version`/`activar_version` no verifican autorización
internamente (mismo criterio ya documentado en `versionamiento.py`), así
que esa comprobación es responsabilidad exclusiva de esta capa.

**Sin disparo de ejecución**: `apps.workflows.motor.iniciar_workflow` NO
se expone en ninguna vista de este módulo (decisión explícita — la
integración real que dispara Workflows, vía Ticket/Proceso, sigue
diferida a un sprint posterior).

**3.UI.4 agrega el editor** de Etapas/Transiciones de una versión
BORRADOR (`crear_etapa_view`/`editar_etapa_view`/`cambiar_tipo_etapa_view`/
`eliminar_etapa_view`/`configurar_etapa_view`/`crear_transicion_view`/
`editar_transicion_view`/`eliminar_transicion_view`) — todas invocan
exclusivamente `apps.workflows.editor`, nunca escriben `Etapa`/
`TransicionEtapa`/configuraciones directamente. `configurar_etapa_view` es
un único endpoint con dispatch interno por `etapa.tipo` (TAREA/APROBACION/
ESPERA) — evita 3 rutas casi idénticas. Mismo patrón de autorización
estricta (`puede_administrar_workflows` re-comprobado antes de cada
mutación) y de capturar únicamente `ValidationError` alrededor de la
operación de dominio."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from apps.workflows import editor
from apps.workflows.autorizacion import puede_administrar_workflows, puede_consultar_workflows
from apps.workflows.forms import (
    CambiarTipoEtapaForm,
    ConfiguracionAprobacionForm,
    ConfiguracionEsperaForm,
    ConfiguracionTareaForm,
    EtapaEdicionForm,
    EtapaForm,
    ParticipanteAprobacionFormSet,
    TransicionForm,
    WorkflowForm,
)
from apps.workflows.models import Etapa, ParticipanteEtapaAprobacion, TransicionEtapa, Workflow, WorkflowVersion
from apps.workflows.validacion import validar_estructura
from apps.workflows.versionamiento import activar_version, crear_nueva_version, crear_workflow, editar_workflow


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required
def lista_view(request):
    if not puede_consultar_workflows(request.user):
        raise PermissionDenied
    workflows = Workflow.objects.select_related("version_activa").annotate(num_versiones=Count("versiones")).order_by(
        "nombre"
    )
    contexto = {
        "workflows": workflows,
        "puede_administrar": puede_administrar_workflows(request.user),
        "titulo_pagina": "Workflows",
    }
    return render(request, "workflows/lista.html", contexto)


@login_required
def crear_view(request):
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = WorkflowForm(request.POST)
        if form.is_valid():
            workflow = crear_workflow(
                request.user,
                nombre=form.cleaned_data["nombre"],
                descripcion=form.cleaned_data.get("descripcion", ""),
            )
            messages.success(request, "Workflow creado.")
            return redirect("workflows:detalle", pk=workflow.pk)
        # Sin objeto previo al que volver (a diferencia de las acciones
        # inline de 3.UI.1/3.UI.2, que redirigen a un detalle ya
        # existente): aquí se re-renderiza el mismo formulario con sus
        # errores, único caso de este proyecto donde una vista no aplica
        # PRG sobre un POST inválido.
    else:
        form = WorkflowForm()
    return render(request, "workflows/crear.html", {"form": form, "titulo_pagina": "Nuevo workflow"})


@login_required
def detalle_view(request, pk):
    workflow = get_object_or_404(Workflow.objects.select_related("version_activa"), pk=pk)
    if not puede_consultar_workflows(request.user):
        raise PermissionDenied
    administra = puede_administrar_workflows(request.user)
    contexto = {
        "workflow": workflow,
        "versiones": workflow.versiones.order_by("-numero"),
        "puede_administrar": administra,
        "form": (
            WorkflowForm(initial={"nombre": workflow.nombre, "descripcion": workflow.descripcion})
            if administra
            else None
        ),
        "titulo_pagina": workflow.nombre,
    }
    return render(request, "workflows/detalle.html", contexto)


@login_required
def editar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    workflow = get_object_or_404(Workflow, pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    form = WorkflowForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos del workflow.")
        return redirect("workflows:detalle", pk=pk)
    editar_workflow(
        workflow, request.user, nombre=form.cleaned_data["nombre"], descripcion=form.cleaned_data.get("descripcion", "")
    )
    messages.success(request, "Workflow actualizado.")
    return redirect("workflows:detalle", pk=pk)


@login_required
def crear_version_view(request, pk):
    """Crea una nueva `WorkflowVersion` BORRADOR — exclusivamente vía
    `crear_nueva_version(workflow, actor)`, sin `clonar_desde`: sigue el
    comportamiento ya definido por el dominio (clona `version_activa` si
    existe, o crea una v1 vacía si no) sin que esta vista decida una
    política distinta."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    workflow = get_object_or_404(Workflow, pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version = crear_nueva_version(workflow, request.user)
    messages.success(request, f"Versión {version.numero} (borrador) creada.")
    return redirect("workflows:version_detalle", pk=version.pk)


def _formularios_de_edicion(version, etapas):
    """Precarga, para cada Etapa (y cada una de sus transiciones
    salientes), los Forms de edición/configuración con sus valores
    actuales — usados únicamente por `version_detalle_view` cuando la
    versión es BORRADOR y el usuario administra. Ningún Form se usa para
    persistir aquí: solo para render (GET); las vistas de acción vuelven a
    instanciarlos con `request.POST`.

    Se adjuntan como atributos directos de cada instancia (`etapa.
    form_editar`, `transicion.form_editar`, ...) en vez de un dict externo
    indexado por `pk` — evita necesitar un filtro de plantilla custom
    (`{{ dict.variable_como_clave }}` no es una operación que los
    templates de Django resuelvan sin un filtro adicional; un atributo sí
    se resuelve con la sintaxis de punto normal).

    Cada Form lleva un `prefix` único por etapa/transición
    (`etapa-<pk>-editar`, `etapa-<pk>-tipo`, `etapa-<pk>-config`,
    `etapa-<pk>-transicion`, `transicion-<pk>`) — sin esto, Django genera
    el mismo `id_<campo>` para el mismo campo en CADA etapa de la página
    (ej. `id_nombre` repetido tantas veces como etapas), rompiendo la
    asociación `<label for>` de todas menos la primera. Las vistas de
    acción (`editar_etapa_view`, etc.) deben usar exactamente el mismo
    `prefix` al volver a instanciar el Form con `request.POST` — si no
    coinciden, `is_valid()` no encuentra los campos."""
    etapas_qs = version.etapas.all()
    for etapa in etapas:
        etapa.form_editar = EtapaEdicionForm(
            initial={"nombre": etapa.nombre, "descripcion": etapa.descripcion}, prefix=f"etapa-{etapa.pk}-editar"
        )
        etapa.form_tipo = CambiarTipoEtapaForm(initial={"tipo": etapa.tipo}, prefix=f"etapa-{etapa.pk}-tipo")
        etapa.form_transicion = TransicionForm(
            etapas_destino_queryset=etapas_qs.exclude(pk=etapa.pk), prefix=f"etapa-{etapa.pk}-transicion"
        )

        for transicion in etapa.transiciones_salientes.all():
            transicion.form_editar = TransicionForm(
                initial={
                    "etapa_destino": transicion.etapa_destino_id,
                    "nombre": transicion.nombre,
                    "prioridad": transicion.prioridad,
                    "variable": transicion.variable,
                    "operador": transicion.operador,
                    "valor": transicion.valor,
                    "es_fallback": transicion.es_fallback,
                    "resultado_aprobacion": transicion.resultado_aprobacion,
                },
                etapas_destino_queryset=etapas_qs.exclude(pk=etapa.pk),
                prefix=f"transicion-{transicion.pk}",
            )

        if etapa.tipo == Etapa.Tipo.TAREA:
            configuracion = getattr(etapa, "configuracion_tarea", None)
            etapa.form_configurar_tarea = ConfiguracionTareaForm(
                initial={
                    "tipo_responsable": configuracion.tipo_responsable if configuracion else "",
                    "usuario_responsable": configuracion.usuario_responsable_id if configuracion else None,
                    "equipo_responsable": configuracion.equipo_responsable_id if configuracion else None,
                    "permite_subtareas": configuracion.permite_subtareas if configuracion else False,
                },
                prefix=f"etapa-{etapa.pk}-config",
            )
        elif etapa.tipo == Etapa.Tipo.APROBACION:
            configuracion = getattr(etapa, "configuracion_aprobacion", None)
            etapa.form_configurar_aprobacion = ConfiguracionAprobacionForm(
                initial={"modo": configuracion.modo, "politica": configuracion.politica} if configuracion else {},
                prefix=f"etapa-{etapa.pk}-config",
            )
            participantes_iniciales = (
                [
                    {"tipo_aprobador": p.tipo_aprobador, "usuario": p.usuario_id, "equipo": p.equipo_id}
                    for p in configuracion.participantes.order_by("orden")
                ]
                if configuracion
                else []
            )
            etapa.formset_participantes = ParticipanteAprobacionFormSet(
                initial=participantes_iniciales, prefix=f"participantes-{etapa.pk}"
            )
        elif etapa.tipo == Etapa.Tipo.ESPERA:
            etapa.form_configurar_espera = ConfiguracionEsperaForm(
                initial=etapa.configuracion or {}, prefix=f"etapa-{etapa.pk}-config"
            )
    return etapas


@login_required
def version_detalle_view(request, pk):
    """Muestra la definición interna (Etapas/Transiciones) y, desde
    3.UI.4, aloja el editor cuando la versión es BORRADOR y el usuario
    administra (`editable`) — los formularios de creación/edición viven
    en esta misma página (sin modal, sin tabs), cada uno enviando a su
    propio endpoint de acción. `errores` se calcula con la misma
    `validar_estructura(version)` que usa `activar_version` internamente
    — "preview" real, nunca una reimplementación paralela de la regla."""
    version = get_object_or_404(
        WorkflowVersion.objects.select_related("workflow").prefetch_related(
            "etapas__transiciones_salientes__etapa_destino",
            "etapas__configuracion_tarea",
            "etapas__configuracion_aprobacion__participantes",
        ),
        pk=pk,
    )
    if not puede_consultar_workflows(request.user):
        raise PermissionDenied
    administra = puede_administrar_workflows(request.user)
    editable = administra and version.estado == WorkflowVersion.Estado.BORRADOR
    errores = validar_estructura(version) if version.estado == WorkflowVersion.Estado.BORRADOR else []
    etapas = list(version.etapas.all())
    if editable:
        etapas = _formularios_de_edicion(version, etapas)

    contexto = {
        "workflow": version.workflow,
        "version": version,
        "etapas": etapas,
        "errores": errores,
        "puede_administrar": administra,
        "editable": editable,
        "form_crear_etapa": EtapaForm() if editable else None,
        "tipos_no_disponibles": editor.TIPOS_NO_DISPONIBLES_EN_EDITOR,
        "titulo_pagina": f"{version.workflow.nombre} — v{version.numero}",
    }
    return render(request, "workflows/version_detalle.html", contexto)


@login_required
def activar_version_view(request, pk):
    """Exclusivamente vía `activar_version(workflow, version, actor)` — la
    vista nunca reimplementa `validar_estructura` ni decide por su cuenta
    si la versión puede activarse; solo presenta los errores que el
    dominio ya devuelve (`ValidationError`) o el `ValueError` si la
    versión no está en BORRADOR."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    version = get_object_or_404(WorkflowVersion.objects.select_related("workflow"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    try:
        activar_version(version.workflow, version, actor=request.user)
    except (ValueError, ValidationError) as exc:
        mensajes = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
        messages.error(request, "; ".join(mensajes))
    else:
        messages.success(request, f"Versión {version.numero} activada.")
    return redirect("workflows:version_detalle", pk=pk)


# --- 3.UI.4 — Editor: Etapa ----------------------------------------------


@login_required
def crear_etapa_view(request, version_pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    version = get_object_or_404(WorkflowVersion, pk=version_pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    form = EtapaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos de la etapa.")
        return redirect("workflows:version_detalle", pk=version_pk)
    try:
        editor.crear_etapa(
            version,
            request.user,
            tipo=form.cleaned_data["tipo"],
            nombre=form.cleaned_data["nombre"],
            descripcion=form.cleaned_data.get("descripcion", ""),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Etapa creada.")
    return redirect("workflows:version_detalle", pk=version_pk)


@login_required
def editar_etapa_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    etapa = get_object_or_404(Etapa.objects.select_related("version"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = etapa.version_id
    form = EtapaEdicionForm(request.POST, prefix=f"etapa-{pk}-editar")
    if not form.is_valid():
        messages.error(request, "Revise los datos de la etapa.")
        return redirect("workflows:version_detalle", pk=version_pk)
    try:
        editor.editar_etapa(
            etapa, request.user, nombre=form.cleaned_data["nombre"], descripcion=form.cleaned_data.get("descripcion", "")
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Etapa actualizada.")
    return redirect("workflows:version_detalle", pk=version_pk)


@login_required
def cambiar_tipo_etapa_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    etapa = get_object_or_404(Etapa.objects.select_related("version"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = etapa.version_id
    form = CambiarTipoEtapaForm(request.POST, prefix=f"etapa-{pk}-tipo")
    if not form.is_valid():
        messages.error(request, "Revise el tipo seleccionado.")
        return redirect("workflows:version_detalle", pk=version_pk)
    try:
        editor.cambiar_tipo_etapa(etapa, request.user, nuevo_tipo=form.cleaned_data["tipo"])
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tipo de etapa actualizado.")
    return redirect("workflows:version_detalle", pk=version_pk)


@login_required
def eliminar_etapa_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    etapa = get_object_or_404(Etapa.objects.select_related("version"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = etapa.version_id
    try:
        editor.eliminar_etapa(etapa, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Etapa eliminada.")
    return redirect("workflows:version_detalle", pk=version_pk)


@login_required
def configurar_etapa_view(request, pk):
    """Único endpoint con dispatch interno por `etapa.tipo` — evita 3
    rutas casi idénticas (`configurar_tarea`/`configurar_aprobacion`/
    `configurar_espera`). Cada rama valida su Form específico y delega
    en la operación de dominio correspondiente de `apps.workflows.editor`."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    etapa = get_object_or_404(Etapa.objects.select_related("version"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = etapa.version_id

    if etapa.tipo == Etapa.Tipo.TAREA:
        form = ConfiguracionTareaForm(request.POST, prefix=f"etapa-{pk}-config")
        if not form.is_valid():
            messages.error(request, "Revise los datos de la configuración.")
            return redirect("workflows:version_detalle", pk=version_pk)
        try:
            editor.configurar_etapa_tarea(
                etapa,
                request.user,
                tipo_responsable=form.cleaned_data["tipo_responsable"],
                usuario_responsable=form.cleaned_data.get("usuario_responsable"),
                equipo_responsable=form.cleaned_data.get("equipo_responsable"),
                permite_subtareas=form.cleaned_data.get("permite_subtareas", False),
            )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Configuración guardada.")

    elif etapa.tipo == Etapa.Tipo.APROBACION:
        config_form = ConfiguracionAprobacionForm(request.POST, prefix=f"etapa-{pk}-config")
        formset = ParticipanteAprobacionFormSet(request.POST, prefix=f"participantes-{pk}")
        if not config_form.is_valid() or not formset.is_valid():
            messages.error(request, "Revise los datos de la configuración y los participantes.")
            return redirect("workflows:version_detalle", pk=version_pk)
        participantes = []
        for form_participante in formset:
            datos = form_participante.cleaned_data
            if not datos or datos.get("DELETE"):
                continue
            tipo_aprobador = datos["tipo_aprobador"]
            aprobador = (
                datos.get("usuario")
                if tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.USUARIO
                else datos.get("equipo") if tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.EQUIPO
                else None
            )
            participantes.append((tipo_aprobador, aprobador))
        try:
            editor.configurar_etapa_aprobacion(
                etapa,
                request.user,
                modo=config_form.cleaned_data["modo"],
                politica=config_form.cleaned_data.get("politica") or None,
                participantes=participantes,
            )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Configuración guardada.")

    elif etapa.tipo == Etapa.Tipo.ESPERA:
        form = ConfiguracionEsperaForm(request.POST, prefix=f"etapa-{pk}-config")
        if not form.is_valid():
            messages.error(request, "Revise los datos de la espera.")
            return redirect("workflows:version_detalle", pk=version_pk)
        fecha_objetivo = form.cleaned_data.get("fecha_objetivo")
        try:
            editor.configurar_etapa_espera(
                etapa,
                request.user,
                modo=form.cleaned_data["modo"],
                duracion_valor=form.cleaned_data.get("duracion_valor"),
                duracion_unidad=form.cleaned_data.get("duracion_unidad"),
                fecha_objetivo=fecha_objetivo.isoformat() if fecha_objetivo else None,
            )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Configuración guardada.")

    else:
        messages.error(request, "Este tipo de etapa no tiene configuración especializada.")

    return redirect("workflows:version_detalle", pk=version_pk)


# --- 3.UI.4 — Editor: Transición -------------------------------------------


@login_required
def crear_transicion_view(request, etapa_pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    etapa_origen = get_object_or_404(Etapa.objects.select_related("version"), pk=etapa_pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = etapa_origen.version_id
    form = TransicionForm(
        request.POST,
        etapas_destino_queryset=Etapa.objects.filter(version_id=version_pk).exclude(pk=etapa_origen.pk),
        prefix=f"etapa-{etapa_pk}-transicion",
    )
    if not form.is_valid():
        messages.error(request, "Revise los datos de la transición.")
        return redirect("workflows:version_detalle", pk=version_pk)
    try:
        editor.crear_transicion(
            etapa_origen,
            form.cleaned_data["etapa_destino"],
            request.user,
            nombre=form.cleaned_data.get("nombre", ""),
            prioridad=form.cleaned_data.get("prioridad") or 0,
            variable=form.cleaned_data.get("variable", ""),
            operador=form.cleaned_data.get("operador", ""),
            valor=form.cleaned_data.get("valor", ""),
            es_fallback=form.cleaned_data.get("es_fallback", False),
            resultado_aprobacion=form.cleaned_data.get("resultado_aprobacion", ""),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Transición creada.")
    return redirect("workflows:version_detalle", pk=version_pk)


@login_required
def editar_transicion_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    transicion = get_object_or_404(TransicionEtapa.objects.select_related("etapa_origen__version"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = transicion.etapa_origen.version_id
    form = TransicionForm(
        request.POST,
        etapas_destino_queryset=Etapa.objects.filter(version_id=version_pk).exclude(pk=transicion.etapa_origen_id),
        prefix=f"transicion-{pk}",
    )
    if not form.is_valid():
        messages.error(request, "Revise los datos de la transición.")
        return redirect("workflows:version_detalle", pk=version_pk)
    try:
        editor.editar_transicion(
            transicion,
            request.user,
            etapa_destino=form.cleaned_data["etapa_destino"],
            nombre=form.cleaned_data.get("nombre", ""),
            prioridad=form.cleaned_data.get("prioridad") or 0,
            variable=form.cleaned_data.get("variable", ""),
            operador=form.cleaned_data.get("operador", ""),
            valor=form.cleaned_data.get("valor", ""),
            es_fallback=form.cleaned_data.get("es_fallback", False),
            resultado_aprobacion=form.cleaned_data.get("resultado_aprobacion", ""),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Transición actualizada.")
    return redirect("workflows:version_detalle", pk=version_pk)


@login_required
def eliminar_transicion_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    transicion = get_object_or_404(TransicionEtapa.objects.select_related("etapa_origen__version"), pk=pk)
    if not puede_administrar_workflows(request.user):
        raise PermissionDenied
    version_pk = transicion.etapa_origen.version_id
    try:
        editor.eliminar_transicion(transicion, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Transición eliminada.")
    return redirect("workflows:version_detalle", pk=version_pk)
