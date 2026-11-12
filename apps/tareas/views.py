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
from django.shortcuts import get_object_or_404, redirect

from apps.core.redirecciones import volver_a
from apps.tickets.vinculos import url_de_tarea
from apps.tareas import operaciones
from apps.tareas.autorizacion import (
    puede_asignar_tarea,
    puede_comentar_tarea,
    puede_completar_tarea,
    puede_consultar_tarea,
    puede_crear_subtarea,
    puede_iniciar_tarea,
    puede_reasignar_tarea,
    puede_tomar_tarea,
)
from apps.tareas.forms import (
    AsignacionTareaForm,
    ComentarioTareaForm,
    EvidenciaTareaForm,
    ReasignacionTareaForm,
    SubtareaForm,
)
from apps.tareas.models import AdjuntoTarea, ItemVerificacionTarea, Tarea
from apps.workflows.integracion import completar_tarea_workflow


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@login_required
def bandeja_view(request):
    """Retirada: las tareas se trabajan en Mi trabajo (bandeja por ticket). La URL se conserva como redirección."""
    return redirect("core:mi_trabajo")


@login_required
def detalle_view(request, pk):
    """Retirada como página: redirige al espacio del ticket de la tarea, así siguen sirviendo los enlaces de correos
    y notificaciones ya enviados. Misma autorización de objeto de siempre (`puede_consultar_tarea`)."""
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_consultar_tarea(request.user, tarea):
        raise PermissionDenied
    return redirect(url_de_tarea(tarea))


@login_required
def verificar_view(request, pk, item_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    item = get_object_or_404(ItemVerificacionTarea, pk=item_id, tarea=tarea)
    if not puede_completar_tarea(request.user, tarea):
        raise PermissionDenied
    try:
        operaciones.marcar_verificacion(
            item, request.user, marcado=request.POST.get("marcado") == "1",
            conclusion=request.POST.get("conclusion") if "conclusion" in request.POST else None,
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    return volver_a(request, "tareas:detalle", pk)


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
    return volver_a(request, "tareas:detalle", pk)


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
    return volver_a(request, "tareas:detalle", pk)


@login_required
def completar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    tarea = get_object_or_404(Tarea, pk=pk)
    if not puede_completar_tarea(request.user, tarea):
        raise PermissionDenied
    try:
        if hasattr(tarea, "vinculo_workflow") or hasattr(tarea, "vinculo_elemento"):
            completar_tarea_workflow(tarea, request.user)
        elif hasattr(tarea, "ronda_gaceta"):
            # Fase 3: completar la corrección de una gaceta es reenviarla a revisión (una sola operación).
            from apps.gacetas.operaciones import reenviar_por_tarea

            reenviar_por_tarea(tarea, request.user)
        else:
            operaciones.completar_tarea(tarea, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea completada.")
    return volver_a(request, "tareas:detalle", pk)


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
        return volver_a(request, "tareas:detalle", pk)
    try:
        operaciones.asignar_tarea(
            tarea, request.user, usuario=form.cleaned_data["usuario"], equipo=form.cleaned_data["equipo"]
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea asignada.")
    return volver_a(request, "tareas:detalle", pk)


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
        return volver_a(request, "tareas:detalle", pk)
    try:
        operaciones.reasignar_tarea(
            tarea, request.user, usuario=form.cleaned_data["usuario"], equipo=form.cleaned_data["equipo"]
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Tarea reasignada.")
    return volver_a(request, "tareas:detalle", pk)


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
        return volver_a(request, "tareas:detalle", pk)
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
    return volver_a(request, "tareas:detalle", pk)


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
        return volver_a(request, "tareas:detalle", pk)
    try:
        operaciones.comentar_tarea(tarea, request.user, form.cleaned_data["contenido"])
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Comentario agregado.")
    return volver_a(request, "tareas:detalle", pk)


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
        return volver_a(request, "tareas:detalle", pk)
    try:
        operaciones.adjuntar_evidencia_tarea(request.user, form.cleaned_data["archivo"], tarea=tarea)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Evidencia adjuntada.")
    return volver_a(request, "tareas:detalle", pk)


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
