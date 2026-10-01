"""Django Admin de Aprobaciones — inspección técnica provisional, mismo
criterio que `apps/tareas/admin.py`: íntegramente de solo lectura, ninguna
vía de creación/edición/eliminación por Admin — todas las mutaciones pasan
por `apps.aprobaciones.operaciones`."""

from django.contrib import admin

from apps.aprobaciones.autorizacion import PERMISO_GESTIONAR
from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion, ReasignacionAprobacion
from apps.core.autorizacion import usuario_tiene_permiso


class PermisoGlobalAdminMixin:
    """Duplicado deliberadamente de `apps/workflows/admin.py`/
    `apps/tareas/admin.py` (mismo criterio ya documentado ahí: no se
    extrae a un módulo compartido)."""

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


class AprobacionInlineSoloLectura(admin.TabularInline):
    model = Aprobacion
    fields = (
        "orden",
        "tipo_aprobador",
        "aprobador_usuario",
        "aprobador_equipo",
        "estado",
        "decidido_por",
        "decidida_en",
    )
    readonly_fields = fields
    extra = 0
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EsquemaAprobacion)
class EsquemaAprobacionAdmin(PermisoGlobalAdminMixin, admin.ModelAdmin):
    list_display = ("id", "modo", "politica", "resultado", "resuelto_en", "creado_en")
    list_filter = ("modo", "politica", "resultado")
    readonly_fields = ("modo", "politica", "resultado", "resuelto_en", "creado_en")
    inlines = [AprobacionInlineSoloLectura]


class ReasignacionAprobacionInlineSoloLectura(admin.TabularInline):
    model = ReasignacionAprobacion
    fields = (
        "aprobador_anterior_usuario",
        "aprobador_anterior_equipo",
        "aprobador_nuevo_usuario",
        "aprobador_nuevo_equipo",
        "reasignado_por",
        "motivo",
        "creado_en",
    )
    readonly_fields = fields
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Aprobacion)
class AprobacionAdmin(PermisoGlobalAdminMixin, admin.ModelAdmin):
    list_display = ("id", "esquema", "orden", "tipo_aprobador", "aprobador_usuario", "aprobador_equipo", "estado")
    list_filter = ("estado", "tipo_aprobador")
    readonly_fields = (
        "esquema",
        "orden",
        "tipo_aprobador",
        "aprobador_usuario",
        "aprobador_equipo",
        "decidido_por",
        "estado",
        "observacion",
        "decidida_en",
    )
    inlines = [ReasignacionAprobacionInlineSoloLectura]
