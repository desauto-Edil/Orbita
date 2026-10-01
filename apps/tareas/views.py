"""Vistas de Tareas — 3.UI.1 (CU-021/022/023).

Interfaz propia de Órbita: reemplaza a Django Admin (que en `apps/tareas/
admin.py` es y sigue siendo de solo lectura) para el trabajo humano normal
sobre `Tarea`. Mismo patrón que `apps/tickets/views.py` — su precedente
más directo: function-based, sin DRF, PRG (POST → operación de dominio →
`messages` → `redirect`), sin lógica de dominio en la vista.

**Autorización — regla obligatoria de este incremento** (distinta, más
estricta, de lo que hasta ahora hacía `apps/tickets/views.py`, que
delegaba el `PermissionDenied` de la operación a un `except` que lo
convertía en `messages.error` + redirect): aquí cada vista de acción
vuelve a cargar el objeto y llama explícitamente al `puede_*` del dominio
ANTES de invocar la operación — si no corresponde, deja propagar
`PermissionDenied` sin capturarlo (Django lo convierte en 403). El
`try/except` alrededor de la operación solo atrapa `ValidationError`
(reglas de estado/relaciones, recuperables como mensaje). La operación
sigue validando `puede_*` internamente también (defensa en profundidad,
no redundancia inútil): ninguna vía —ni un botón oculto, ni un POST
directo, ni un <select> manipulado— sustituye esa doble verificación.

**Única excepción documentada al límite `apps.tareas` nunca importa
`apps.workflows`** (ver docstring de `apps/workflows/models.py::
TareaWorkflow`): esa regla protege la capa de DOMINIO (`models.py`/
`operaciones.py`/`autorizacion.py`/`consultas.py`), para que `Tarea` siga
siendo reutilizable fuera de Workflow. La capa HTTP no es dominio — ya
hay precedente real de que una `views.py` cruza apps (`apps/tickets/
views.py` importa `apps.catalogo.campos`/`apps.catalogo.models`). Esta
vista necesita decidir, para UNA sola acción (completar), cuál API
pública de dominio invocar: si la Tarea es la principal de una
`InstanciaEtapa` (`hasattr(tarea, "vinculo_workflow")` — verdadero solo
por la relación inversa que `apps.workflows.models.TareaWorkflow` declara,
sin que este módulo necesite saber qué es esa relación), la única forma
correcta de completarla es `apps.workflows.integracion.
completar_tarea_workflow` (COMPLETADA + reanuda el motor en una sola
transacción) — nunca `apps.tareas.operaciones.completar_tarea` a secas,
que dejaría el Workflow colgado en EN_ESPERA para siempre. Una subtarea
nunca tiene esa relación (W.6, ya garantizado por el propio dominio), así
que cae siempre en la rama `completar_tarea` sin necesitar ninguna
comprobación adicional aquí."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.tareas import operaciones
from apps.tareas.autorizacion import (
    delegado_actual,
    puede_asignar_tarea,
    puede_comentar_tarea,
    puede_completar_tarea,
    puede_consultar_tarea,
    puede_crear_subtarea,
    puede_delegar_tarea,
    puede_iniciar_tarea,
    puede_reasignar_tarea,
    puede_tomar_tarea,
)
from apps.tareas.consultas import tareas_asignadas_a, tareas_disponibles_para_tomar
from apps.tareas.forms import (
    AsignacionTareaForm,
    ComentarioTareaForm,
    DelegacionTareaForm,
    EvidenciaTareaForm,
    ReasignacionTareaForm,
    SubtareaForm,
)
from apps.tareas.models import AdjuntoTarea, Tarea
from apps.workflows.integracion import completar_tarea_workflow


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _es_vencida(tarea, ahora):
    return tarea.fecha_limite is not None and tarea.fecha_limite < ahora and tarea.estado != Tarea.Estado.COMPLETADA


@login_required
def bandeja_view(request):
    """RQF-071/072/073 — "Asignadas a mí" (`tareas_asignadas_a`) y
    "Disponibles para tomar" (`tareas_disponibles_para_tomar`), ambas
    consultas ya existentes de `apps.tareas.consultas`, usadas sin
    reescribir su `QuerySet`. `vencida` se deriva por fila con exactamente
    el mismo criterio que `apps.tareas.consultas.tareas_vencidas`
    (`fecha_limite` pasada y no COMPLETADA) — nunca se persiste."""
    tab = request.GET.get("tab")
    if tab == "disponibles":
        tareas = tareas_disponibles_para_tomar(request.user)
    else:
        tab = "mias"
        tareas = tareas_asignadas_a(request.user)
    tareas = tareas.select_related("usuario_responsable", "equipo_responsable").order_by("-creado_en")

    ahora = timezone.now()
    filas = [{"tarea": tarea, "vencida": _es_vencida(tarea, ahora)} for tarea in tareas]
    contexto = {"tab": tab, "filas": filas, "titulo_pagina": "Tareas"}
    return render(request, "tareas/lista.html", contexto)


@login_required
def detalle_view(request, pk):
    tarea = get_object_or_404(
        Tarea.objects.select_related(
            "usuario_responsable", "equipo_responsable", "creada_por", "completada_por", "tarea_padre"
        ),
        pk=pk,
    )
    if not puede_consultar_tarea(request.user, tarea):
        raise PermissionDenied

    ahora = timezone.now()
    puede_asignar = puede_asignar_tarea(request.user, tarea)
    puede_reasignar = puede_reasignar_tarea(request.user, tarea)
    puede_delegar = puede_delegar_tarea(request.user, tarea)
    puede_comentar = puede_comentar_tarea(request.user, tarea)
    puede_subtarea = puede_crear_subtarea(request.user, tarea)
    muestra_subtareas = tarea.permite_subtareas or tarea.subtareas.exists()

    contexto = {
        "tarea": tarea,
        "vencida": _es_vencida(tarea, ahora),
        "delegado_actual": delegado_actual(tarea, ahora=ahora),
        "puede_tomar": puede_tomar_tarea(request.user, tarea),
        "puede_iniciar": puede_iniciar_tarea(request.user, tarea),
        "puede_completar": puede_completar_tarea(request.user, tarea),
        "puede_asignar": puede_asignar,
        "puede_reasignar": puede_reasignar,
        "puede_delegar": puede_delegar,
        "puede_comentar": puede_comentar,
        "puede_crear_subtarea": puede_subtarea,
        "asignacion_form": AsignacionTareaForm() if puede_asignar else None,
        "reasignacion_form": ReasignacionTareaForm() if puede_reasignar else None,
        "delegacion_form": DelegacionTareaForm() if puede_delegar else None,
        "subtarea_form": SubtareaForm() if puede_subtarea else None,
        "comentario_form": ComentarioTareaForm() if puede_comentar else None,
        "evidencia_form": EvidenciaTareaForm() if puede_comentar else None,
        "muestra_subtareas": muestra_subtareas,
        "subtareas": (
            tarea.subtareas.select_related("usuario_responsable", "equipo_responsable").all()
            if muestra_subtareas
            else Tarea.objects.none()
        ),
        "comentarios": tarea.comentarios.select_related("autor").prefetch_related("adjuntos").all(),
        "evidencias": tarea.adjuntos.select_related("subido_por").all(),
        "delegaciones": tarea.delegaciones.select_related("delegado_a", "delegada_por").all(),
        "historial": tarea.historial.select_related("actor").all(),
        "titulo_pagina": tarea.titulo,
    }
    return render(request, "tareas/detalle.html", contexto)


@login_required
def tomar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_tomar_tarea(request.user, tarea):
        raise PermissionDenied
    try:
        operaciones.tomar_tarea(tarea, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea tomada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def iniciar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_iniciar_tarea(request.user, tarea):
        raise PermissionDenied
    try:
        operaciones.iniciar_tarea(tarea, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea iniciada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def completar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_completar_tarea(request.user, tarea):
        raise PermissionDenied
    try:
        if hasattr(tarea, "vinculo_workflow"):
            completar_tarea_workflow(tarea, request.user)
        else:
            operaciones.completar_tarea(tarea, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea completada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def asignar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_asignar_tarea(request.user, tarea):
        raise PermissionDenied
    form = AsignacionTareaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos del formulario de asignación.")
        return redirect("tareas:detalle", pk=pk)
    try:
        operaciones.asignar_tarea(
            tarea, request.user, usuario=form.cleaned_data["usuario"], equipo=form.cleaned_data["equipo"]
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea asignada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def reasignar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_reasignar_tarea(request.user, tarea):
        raise PermissionDenied
    form = ReasignacionTareaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos del formulario de reasignación.")
        return redirect("tareas:detalle", pk=pk)
    try:
        operaciones.reasignar_tarea(
            tarea, request.user, usuario=form.cleaned_data["usuario"], equipo=form.cleaned_data["equipo"]
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea reasignada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def delegar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_delegar_tarea(request.user, tarea):
        raise PermissionDenied
    form = DelegacionTareaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos del formulario de delegación.")
        return redirect("tareas:detalle", pk=pk)
    try:
        operaciones.delegar_tarea(
            tarea,
            request.user,
            delegado_a=form.cleaned_data["delegado_a"],
            desde=form.cleaned_data["desde"],
            hasta=form.cleaned_data["hasta"],
            motivo=form.cleaned_data.get("motivo", ""),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea delegada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def crear_subtarea_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea_padre = get_object_or_404(Tarea, pk=pk)
    if not puede_crear_subtarea(request.user, tarea_padre):
        raise PermissionDenied
    form = SubtareaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos de la subtarea.")
        return redirect("tareas:detalle", pk=pk)
    try:
        operaciones.crear_subtarea(
            tarea_padre,
            request.user,
            titulo=form.cleaned_data["titulo"],
            descripcion=form.cleaned_data.get("descripcion", ""),
            usuario_responsable=form.cleaned_data.get("usuario_responsable"),
            equipo_responsable=form.cleaned_data.get("equipo_responsable"),
            fecha_limite=form.cleaned_data.get("fecha_limite"),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Subtarea creada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def comentar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_comentar_tarea(request.user, tarea):
        raise PermissionDenied
    form = ComentarioTareaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "El comentario no puede estar vacío.")
        return redirect("tareas:detalle", pk=pk)
    try:
        operaciones.comentar_tarea(tarea, request.user, form.cleaned_data["contenido"])
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Comentario agregado.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def adjuntar_evidencia_view(request, pk):
    """RQF-076. Evidencia siempre atada directamente a la Tarea
    (`tipo_relacion=TAREA`) — `apps.tareas.operaciones.
    adjuntar_evidencia_tarea` también admite atarla a un comentario
    puntual, pero ningún flujo de 3.UI.1 lo necesita todavía; no se
    inventa esa interacción sin un CU que la pida."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    # Mismo criterio de autorización que usa la propia operación
    # internamente (`adjuntar_evidencia_tarea` exige `puede_comentar_tarea`
    # sobre la Tarea objetivo) — se repite aquí explícitamente, no se
    # infiere, para cumplir la regla de "vuelve a ejecutar puede_*(...)".
    if not puede_comentar_tarea(request.user, tarea):
        raise PermissionDenied
    form = EvidenciaTareaForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Debe seleccionar un archivo.")
        return redirect("tareas:detalle", pk=pk)
    try:
        operaciones.adjuntar_evidencia_tarea(request.user, form.cleaned_data["archivo"], tarea=tarea)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Evidencia adjuntada.")
    return redirect("tareas:detalle", pk=pk)


@login_required
def descargar_evidencia_view(request, adjunto_id):
    """GAP cerrado en 3.UI.1 (ver informe): `apps.tareas` no tenía ningún
    endpoint de descarga protegido — el propio docstring de `AdjuntoTarea`
    ya prescribía este mecanismo exacto (gatear con `puede_consultar_tarea`
    antes de servir el archivo, igual que `apps.tickets.views.
    descargar_adjunto_view`, nunca enlazar `MEDIA_URL` directamente)."""
    adjunto = get_object_or_404(
        AdjuntoTarea.objects.select_related("tarea", "comentario__tarea"), pk=adjunto_id
    )
    tarea = adjunto.tarea if adjunto.tarea_id else adjunto.comentario.tarea
    if not puede_consultar_tarea(request.user, tarea):
        raise PermissionDenied
    return FileResponse(adjunto.archivo.open("rb"), as_attachment=True, filename=adjunto.nombre_original)
