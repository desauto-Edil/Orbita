"""Django Admin como interfaz administrativa provisional de Sprint 0.

No es la UX definitiva de Órbita (decisión estructural ya aprobada) — cubre
CU-004, CU-005, CU-006, CU-007 y CU-008 mientras el módulo de navegación
"Administración" no está diseñado.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.core.auditoria import guardar_formset_auditado, registrar_evento, serializar
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import (
    Area,
    AreaUnidadNegocio,
    AsignacionRol,
    Equipo,
    EquipoArea,
    EquipoUnidadNegocio,
    MiembroEquipo,
    PerfilOrganizacional,
    Permiso,
    RegistroAuditoria,
    RolFuncional,
    RolPermiso,
    UnidadNegocio,
    Usuario,
    UsuarioArea,
    UsuarioUnidadNegocio,
)


class AdminAuditableMixin:
    """Registra en `RegistroAuditoria` cada alta/edición/baja hecha desde
    este `ModelAdmin`. CU-040 (RQF-116/120): cierra, para cada modelo que lo
    usa, la auditoría de sus cambios administrativos.

    Explícito, no basado en señales: estos hooks de Django Admin
    (`save_model`/`delete_model`/`delete_queryset`) son el único lugar del
    proyecto hoy donde una mutación de estos modelos ocurre *y* se conoce a
    `request.user` al mismo tiempo — ver `apps/core/auditoria.py`.
    """

    def save_model(self, request, obj, form, change):
        anterior = None
        if change:
            fila_previa = type(obj).objects.filter(pk=obj.pk).first()
            if fila_previa is not None:
                anterior = serializar(fila_previa)
        super().save_model(request, obj, form, change)
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR if change else RegistroAuditoria.Accion.CREAR,
            instancia=obj,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=request.user,
            datos_anteriores=anterior,
            datos_nuevos=serializar(obj),
        )

    def delete_model(self, request, obj):
        anterior = serializar(obj)
        registrar_evento(
            accion=RegistroAuditoria.Accion.ELIMINAR,
            instancia=obj,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=request.user,
            datos_anteriores=anterior,
            datos_nuevos=None,
        )
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            registrar_evento(
                accion=RegistroAuditoria.Accion.ELIMINAR,
                instancia=obj,
                origen=RegistroAuditoria.Origen.USUARIO,
                usuario=request.user,
                datos_anteriores=serializar(obj),
                datos_nuevos=None,
            )
        super().delete_queryset(request, queryset)


@admin.register(Usuario)
class UsuarioAdmin(AdminAuditableMixin, UserAdmin):
    pass


@admin.register(PerfilOrganizacional)
class PerfilOrganizacionalAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("usuario", "cargo", "telefono")
    search_fields = ("usuario__username", "cargo")


@admin.register(Area)
class AreaAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("nombre", "codigo", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre", "codigo")


@admin.register(UnidadNegocio)
class UnidadNegocioAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("nombre", "codigo", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre", "codigo")


@admin.register(AreaUnidadNegocio)
class AreaUnidadNegocioAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("area", "unidad_negocio", "activo")
    list_filter = ("activo",)


@admin.register(UsuarioArea)
class UsuarioAreaAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("usuario", "area", "es_principal", "activo")
    list_filter = ("activo", "es_principal")
    search_fields = ("usuario__username", "area__nombre")


@admin.register(UsuarioUnidadNegocio)
class UsuarioUnidadNegocioAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("usuario", "unidad_negocio", "es_principal", "activo")
    list_filter = ("activo", "es_principal")
    search_fields = ("usuario__username", "unidad_negocio__nombre")


@admin.register(Equipo)
class EquipoAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("nombre", "activo")
    list_filter = ("activo",)


@admin.register(MiembroEquipo)
class MiembroEquipoAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("equipo", "usuario", "activo", "fecha_inicio", "fecha_fin")
    list_filter = ("activo",)


@admin.register(EquipoArea)
class EquipoAreaAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("equipo", "area", "activo")
    list_filter = ("activo",)


@admin.register(EquipoUnidadNegocio)
class EquipoUnidadNegocioAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("equipo", "unidad_negocio", "activo")
    list_filter = ("activo",)


@admin.register(Permiso)
class PermisoAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("codigo", "nombre", "activo")
    list_filter = ("activo",)
    search_fields = ("codigo", "nombre")


class RolPermisoInline(admin.TabularInline):
    model = RolPermiso
    extra = 1


@admin.register(RolFuncional)
class RolFuncionalAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("nombre", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre",)
    inlines = [RolPermisoInline]

    def save_formset(self, request, form, formset, change):
        """`RolPermisoInline` no pasa por `save_model` (es un formset, no un
        `ModelAdmin` aparte) — sin este override, asociar/quitar permisos de
        un rol quedaría sin auditar, dejando incompleta la cobertura de
        CU-007 (RQF-120) para `RolPermiso`.
        """
        if formset.model is not RolPermiso:
            super().save_formset(request, form, formset, change)
            return
        guardar_formset_auditado(request, formset)


@admin.register(AsignacionRol)
class AsignacionRolAdmin(AdminAuditableMixin, admin.ModelAdmin):
    list_display = ("usuario", "rol", "tipo_alcance", "area", "unidad_negocio", "activo")
    list_filter = ("tipo_alcance", "activo")
    search_fields = ("usuario__username", "rol__nombre")


@admin.register(RegistroAuditoria)
class RegistroAuditoriaAdmin(admin.ModelAdmin):
    """Consulta de solo lectura. CU-040 (RQF-117, RN-033).

    Autorización resuelta por el sistema funcional de Órbita (0.3), no por
    `django.contrib.auth.Permission` ni por nombre de rol: `auditoria.consultar`
    es el primer consumidor real de `usuario_tiene_permiso()` — cierra
    parcialmente RQF-005 (solo para este módulo).
    """

    list_display = ("creado_en", "accion", "modelo", "objeto_repr", "origen", "usuario")
    list_filter = ("accion", "origen", "content_type")
    search_fields = ("modelo", "objeto_repr", "usuario__username")
    readonly_fields = [campo.name for campo in RegistroAuditoria._meta.fields]

    def has_view_permission(self, request, obj=None):
        return usuario_tiene_permiso(request.user, "auditoria.consultar")

    def has_module_permission(self, request):
        return usuario_tiene_permiso(request.user, "auditoria.consultar")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
