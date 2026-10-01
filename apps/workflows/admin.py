"""Django Admin como interfaz administrativa provisional de Workflows (3.1),
mismo criterio ya aprobado para Catálogo/Formulario (1.1/1.2): no es la UX
definitiva de Órbita, cubre CU-020 mientras no exista una pantalla propia.

`Workflow → WorkflowVersion → Etapa → TransicionEtapa` tiene la misma forma
de jerarquía de 3 niveles que Form Builder; se resuelve igual:
`show_change_link=True` en vez de anidar inlines dentro de inlines.
`TransicionEtapa` se administra aparte (relaciona dos etapas, no
"pertenece" naturalmente a una sola) — mismo criterio que
`ReglaCondicionalAdmin` en `apps/catalogo/admin.py`.

Crear una versión y activarla son operaciones de dominio explícitas
(`apps/workflows/versionamiento.py`), expuestas como acciones de Admin, no
como edición directa de `Workflow.version_activa` (de solo lectura).

Nota de seguridad: las comprobaciones `has_*_permission` de abajo (gateo
por versión BORRADOR) son una conveniencia de UX, no la barrera real de
integridad — esa es `WorkflowVersion.exigir_editable()`, invocada desde
`save()`/`delete()` de `Etapa`/`TransicionEtapa` sin importar por qué
`ModelAdmin` se llegue a intentar la escritura.

3.2 agrega `InstanciaWorkflowAdmin`/`InstanciaEtapaAdmin`, íntegramente de
solo lectura (sección U de la propuesta aprobada): no hay botón para
iniciar/avanzar/reanudar una ejecución desde aquí — esas son funciones de
`apps/workflows/motor.py`, invocadas programáticamente (W.9).

3.3 agrega `ConfiguracionEtapaTareaInline` (editable mientras la versión
sigue en BORRADOR, mismo gateo que `EtapaInline`) y expone `TareaWorkflow`
de solo lectura dentro de `InstanciaEtapaAdmin` — ninguna acción de
completar una Tarea vive aquí tampoco (esa es `apps.tareas.operaciones`/
`apps.workflows.integracion`, backend únicamente en 3.3).
"""

from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from apps.core.admin import AdminAuditableMixin
from apps.core.auditoria import guardar_formset_auditado
from apps.core.autorizacion import usuario_tiene_permiso
from apps.workflows.autorizacion import PERMISO_ADMINISTRAR
from apps.workflows.models import (
    ConfiguracionEtapaAprobacion,
    ConfiguracionEtapaTarea,
    Etapa,
    InstanciaEtapa,
    InstanciaWorkflow,
    ParticipanteEtapaAprobacion,
    TransicionEtapa,
    Workflow,
    WorkflowVersion,
)
from apps.workflows.versionamiento import activar_version, crear_nueva_version


class InlinesWorkflowAuditablesMixin:
    """Mismo patrón que RolFuncionalAdmin, compartido por los tres dueños."""

    def save_formset(self, request, form, formset, change):
        if formset.model not in (
            Etapa,
            ConfiguracionEtapaTarea,
            ConfiguracionEtapaAprobacion,
            ParticipanteEtapaAprobacion,
        ):
            super().save_formset(request, form, formset, change)
            return
        guardar_formset_auditado(request, formset)


class PermisoGlobalAdminMixin:
    """Gatea el `ModelAdmin` por `permiso_codigo` (alcance GLOBAL).

    Duplicado deliberadamente de `apps/catalogo/admin.py::PermisoGlobalAdminMixin`
    (no se extrae a un módulo compartido en 3.1: eso sería una refactorización
    fuera del alcance aprobado, no algo que este incremento pidiera).
    """

    permiso_codigo = PERMISO_ADMINISTRAR

    def _autorizado(self, request):
        return usuario_tiene_permiso(request.user, self.permiso_codigo)

    def has_view_permission(self, request, obj=None):
        return self._autorizado(request)

    def has_module_permission(self, request):
        return self._autorizado(request)

    def has_add_permission(self, request):
        return self._autorizado(request)

    def has_change_permission(self, request, obj=None):
        return self._autorizado(request)

    def has_delete_permission(self, request, obj=None):
        return self._autorizado(request)


class WorkflowVersionInlineSoloLectura(admin.TabularInline):
    """Muestra las versiones existentes de un `Workflow`. Sin alta manual:
    crear una versión es `crear_nueva_version_action` (clonado), nunca una
    fila en blanco."""

    model = WorkflowVersion
    fields = ("numero", "estado", "creado_en")
    readonly_fields = fields
    extra = 0
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Workflow)
class WorkflowAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("nombre", "version_activa")
    search_fields = ("nombre",)
    readonly_fields = ("version_activa",)
    inlines = [WorkflowVersionInlineSoloLectura]
    actions = ["crear_nueva_version_action"]

    @admin.action(description="Crear nueva versión borrador (clona la versión activa)")
    def crear_nueva_version_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Seleccione un único workflow.", level=messages.ERROR)
            return
        workflow = queryset.first()
        version = crear_nueva_version(workflow, actor=request.user)
        self.message_user(request, f"Versión {version.numero} (borrador) creada.", level=messages.SUCCESS)


class EtapaInline(admin.TabularInline):
    model = Etapa
    fields = ("tipo", "nombre", "descripcion", "configuracion")
    extra = 1
    show_change_link = True

    def _version_editable(self, obj):
        return obj is None or obj.estado == WorkflowVersion.Estado.BORRADOR

    def has_add_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_delete_permission(request, obj)


@admin.register(WorkflowVersion)
class WorkflowVersionAdmin(
    InlinesWorkflowAuditablesMixin, PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin
):
    list_display = ("workflow", "numero", "estado")
    list_filter = ("estado",)
    fields = ("workflow", "numero", "estado")
    readonly_fields = ("workflow", "numero", "estado")
    inlines = [EtapaInline]
    actions = ["activar_version_action"]

    def has_add_permission(self, request):
        # Crear versión es `crear_nueva_version_action` en WorkflowAdmin.
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.estado != WorkflowVersion.Estado.BORRADOR:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Activar esta versión")
    def activar_version_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Seleccione una única versión.", level=messages.ERROR)
            return
        version = queryset.first()
        try:
            activar_version(version.workflow, version, actor=request.user)
        except (ValueError, ValidationError) as exc:
            mensajes = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
            self.message_user(request, " / ".join(mensajes), level=messages.ERROR)
            return
        self.message_user(request, f"Versión {version.numero} activada.", level=messages.SUCCESS)


class ConfiguracionEtapaTareaInline(admin.StackedInline):
    """3.3 (W.2) — configuración real de una Etapa TAREA (relación, no
    JSON). Mismo gateo que `EtapaInline`: editable únicamente mientras la
    versión sigue en BORRADOR — `ConfiguracionEtapaTarea.save()`/`delete()`
    ya lo exigen a nivel de modelo (`exigir_editable()`), esto es solo la
    conveniencia de UX equivalente."""

    model = ConfiguracionEtapaTarea
    fields = ("tipo_responsable", "usuario_responsable", "equipo_responsable", "permite_subtareas")
    extra = 0
    max_num = 1

    def _version_editable(self, obj):
        return obj is None or obj.version.estado == WorkflowVersion.Estado.BORRADOR

    def has_add_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_delete_permission(request, obj)


class ConfiguracionEtapaAprobacionInline(admin.StackedInline):
    """3.4 — configuración real de una Etapa APROBACION (relación, no
    JSON). Solo `modo`/`politica` aquí; los participantes se administran
    en `ConfiguracionEtapaAprobacionAdmin` (Django Admin no anida inlines
    dentro de inlines) — mismo criterio de navegación que
    `WorkflowVersionInlineSoloLectura`/`EtapaInline` (`show_change_link`).
    Mismo gateo por versión BORRADOR que `ConfiguracionEtapaTareaInline`."""

    model = ConfiguracionEtapaAprobacion
    fields = ("modo", "politica")
    extra = 0
    max_num = 1
    show_change_link = True

    def _version_editable(self, obj):
        return obj is None or obj.version.estado == WorkflowVersion.Estado.BORRADOR

    def has_add_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_delete_permission(request, obj)


@admin.register(Etapa)
class EtapaAdmin(InlinesWorkflowAuditablesMixin, PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("nombre", "version", "tipo")
    list_filter = ("tipo",)
    inlines = [ConfiguracionEtapaTareaInline, ConfiguracionEtapaAprobacionInline]

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        return obj is None or obj.version.estado == WorkflowVersion.Estado.BORRADOR

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        return obj is None or obj.version.estado == WorkflowVersion.Estado.BORRADOR


class ParticipanteEtapaAprobacionInline(admin.TabularInline):
    """3.4 — participantes de una `ConfiguracionEtapaAprobacion`, mismo
    gateo por versión BORRADOR que el resto de inlines editables de esta
    jerarquía."""

    model = ParticipanteEtapaAprobacion
    fields = ("orden", "tipo_aprobador", "usuario", "equipo")
    extra = 1

    def _version_editable(self, obj):
        return obj is None or obj.etapa.version.estado == WorkflowVersion.Estado.BORRADOR

    def has_add_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_delete_permission(request, obj)


@admin.register(ConfiguracionEtapaAprobacion)
class ConfiguracionEtapaAprobacionAdmin(
    InlinesWorkflowAuditablesMixin, PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin
):
    """Administrada aparte (mismo criterio que `TransicionEtapaAdmin`):
    aloja la lista de participantes, que Django Admin no puede anidar
    dentro del inline de `EtapaAdmin`."""

    list_display = ("etapa", "modo", "politica")
    list_filter = ("modo", "politica")
    inlines = [ParticipanteEtapaAprobacionInline]

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        return obj is None or obj.etapa.version.estado == WorkflowVersion.Estado.BORRADOR

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        return obj is None or obj.etapa.version.estado == WorkflowVersion.Estado.BORRADOR


@admin.register(TransicionEtapa)
class TransicionEtapaAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    list_display = (
        "etapa_origen",
        "etapa_destino",
        "prioridad",
        "es_fallback",
        "operador",
        "valor",
        "resultado_aprobacion",
    )
    list_filter = ("es_fallback", "operador", "resultado_aprobacion")

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        return obj is None or obj.etapa_origen.version.estado == WorkflowVersion.Estado.BORRADOR

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        return obj is None or obj.etapa_origen.version.estado == WorkflowVersion.Estado.BORRADOR


class InstanciaEtapaInlineSoloLectura(admin.TabularInline):
    """Historial de ejecución de una `InstanciaWorkflow` (RQF-086) — 3.2.
    Sin alta/edición/baja manual: estas filas solo las escribe
    `apps/workflows/motor.py`, nunca un Gestor desde el Admin (3.2 no tiene
    UI de disparo manual, W.9/U de la propuesta aprobada)."""

    model = InstanciaEtapa
    fields = ("orden", "etapa", "estado", "iniciada_en", "finalizada_en", "transicion_tomada")
    readonly_fields = fields
    extra = 0
    show_change_link = True
    ordering = ("orden",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InstanciaWorkflow)
class InstanciaWorkflowAdmin(PermisoGlobalAdminMixin, admin.ModelAdmin):
    """Solo lectura (U de la propuesta aprobada): ninguna acción de
    iniciar/avanzar/reanudar se expone aquí — son funciones de dominio de
    `apps/workflows/motor.py`, invocadas programáticamente, no por un botón
    de Admin en 3.2. No hereda `AdminAuditableMixin`: estos modelos no se
    editan desde el Admin, así que no hay nada que auditar en `save_model`/
    `delete_model` — la auditoría de 3.2 es el evento único de
    `iniciar_workflow` (sección P de la propuesta aprobada), ya registrado
    por el propio motor."""

    permiso_codigo = PERMISO_ADMINISTRAR
    list_display = ("workflow_version", "estado", "creado_en", "finalizada_en")
    list_filter = ("estado",)
    readonly_fields = (
        "workflow_version",
        "estado",
        "contexto",
        "iniciado_por",
        "creado_en",
        "finalizada_en",
    )
    inlines = [InstanciaEtapaInlineSoloLectura]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InstanciaEtapa)
class InstanciaEtapaAdmin(PermisoGlobalAdminMixin, admin.ModelAdmin):
    """Solo lectura, igual criterio que `InstanciaWorkflowAdmin` — se
    registra aparte (no solo como inline) para poder consultarlas
    directamente, mismo criterio que `TransicionEtapaAdmin` en 3.1."""

    permiso_codigo = PERMISO_ADMINISTRAR
    list_display = ("instancia_workflow", "orden", "etapa", "estado", "motivo_espera", "iniciada_en", "finalizada_en")
    list_filter = ("estado", "motivo_espera")
    readonly_fields = (
        "instancia_workflow",
        "etapa",
        "orden",
        "estado",
        "motivo_espera",
        "iniciada_en",
        "finalizada_en",
        "resultado",
        "error",
        "transicion_tomada",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
