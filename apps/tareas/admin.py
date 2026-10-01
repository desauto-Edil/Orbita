"""Django Admin de Tareas — inspección técnica provisional (mismo criterio
que 3.1/3.2 en `apps/workflows/admin.py`): no es la UX definitiva de
Órbita (esa es 3.UI). Íntegramente de solo lectura — ninguna vía de
creación/edición/eliminación por Admin: todas las mutaciones pasan por
`apps.tareas.operaciones`, que valida autorización, concurrencia e
historial/auditoría; un `ModelAdmin` editable las saltaría todas."""

from django.contrib import admin

from apps.core.autorizacion import usuario_tiene_permiso
from apps.tareas.autorizacion import PERMISO_GESTIONAR
from apps.tareas.models import AdjuntoTarea, ComentarioTarea, DelegacionTarea, HistorialTarea, Tarea


class PermisoGlobalAdminMixin:
    """Duplicado deliberadamente de `apps/workflows/admin.py` (mismo
    criterio ya documentado ahí: no se extrae a un módulo compartido)."""

    permiso_codigo = PERMISO_GESTIONAR

    def _autorizado(self, request):
        return usuario_tiene_permiso(request.user, self.permiso_codigo)

    def has_view_permission(self, request, obj=None):
        return self._autorizado(request)

    def has_module_permission(self, request):
        return self._autorizado(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ComentarioTareaInlineSoloLectura(admin.TabularInline):
    model = ComentarioTarea
    fields = ("autor", "contenido", "creado_en")
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class HistorialTareaInlineSoloLectura(admin.TabularInline):
    model = HistorialTarea
    fields = ("tipo_evento", "actor", "datos", "creado_en")
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DelegacionTareaInlineSoloLectura(admin.TabularInline):
    model = DelegacionTarea
    fk_name = "tarea"
    fields = ("delegado_a", "delegada_por", "desde", "hasta", "motivo")
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Tarea)
class TareaAdmin(PermisoGlobalAdminMixin, admin.ModelAdmin):
    list_display = ("titulo", "estado", "usuario_responsable", "equipo_responsable", "fecha_limite", "tarea_padre")
    list_filter = ("estado", "origen")
    search_fields = ("titulo",)
    readonly_fields = (
        "titulo",
        "descripcion",
        "estado",
        "origen",
        "fecha_limite",
        "creada_por",
        "usuario_responsable",
        "equipo_responsable",
        "iniciada_en",
        "completada_por",
        "completada_en",
        "permite_subtareas",
        "tarea_padre",
    )
    inlines = [ComentarioTareaInlineSoloLectura, DelegacionTareaInlineSoloLectura, HistorialTareaInlineSoloLectura]


@admin.register(AdjuntoTarea)
class AdjuntoTareaAdmin(PermisoGlobalAdminMixin, admin.ModelAdmin):
    list_display = ("nombre_original", "tipo_relacion", "tarea", "comentario", "subido_por", "creado_en")
    list_filter = ("tipo_relacion",)
    readonly_fields = (
        "tipo_relacion",
        "tarea",
        "comentario",
        "archivo",
        "nombre_original",
        "tipo_mime",
        "tamano_bytes",
        "subido_por",
    )
