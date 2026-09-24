"""Django Admin como interfaz administrativa provisional de Catálogo (1.1)
y Form Builder (1.2).

No es la UX definitiva de Órbita — cubre CU-010/CU-012/CU-013 mientras no
existan pantallas propias de administración. Gatea alta/edición/baja/
consulta con permisos funcionales, siempre en alcance GLOBAL
(`usuario_tiene_permiso` sin `area`/`unidad_negocio` pregunta explícitamente
por GLOBAL, por diseño de 0.3) — limitación deliberada de esta interfaz
provisional: no representa todavía administración por Área/Unidad. Se
revisita cuando exista una interfaz de administración propia. No se compara
nombre de rol en ningún punto (RN-007).

Form Builder (Formulario → FormularioVersion → Campo → Opciones/Reglas)
tiene 3 niveles de jerarquía real; Django Admin no anida inlines dentro de
inlines, así que se resuelve con `show_change_link=True`: cada inline
enlaza al `ModelAdmin` standalone del siguiente nivel en vez de anidarlo.
`ReglaCondicional` se administra aparte (relaciona dos campos, no
"pertenece" naturalmente a uno).

Nota de seguridad: las comprobaciones `has_*_permission` de abajo (gateo por
versión BORRADOR) son una conveniencia de UX, no la barrera real de
integridad — esa es `FormularioVersion.exigir_editable()`, invocada desde
`save()`/`delete()` de `Campo`/`OpcionCampo`/`ReglaCondicional`
(`apps/catalogo/models/formularios.py`) sin importar por qué `ModelAdmin` se
llegue a intentar la escritura.

Crear una versión y activarla son operaciones de dominio explícitas
(`apps/catalogo/versionamiento.py`), no altas/ediciones de fila libres — se
exponen como acciones de Admin, no como inline editable ni como edición
directa de `Formulario.version_activa` (que por eso es de solo lectura).
"""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from apps.core.admin import AdminAuditableMixin
from apps.core.auditoria import guardar_formset_auditado
from apps.core.autorizacion import usuario_tiene_permiso
from apps.catalogo.models import (
    Campo,
    Categoria,
    Formulario,
    FormularioVersion,
    OpcionCampo,
    ReglaCondicional,
    Servicio,
    ServicioResponsable,
    ServicioVisibilidad,
)
from apps.catalogo.versionamiento import activar_version, crear_nueva_version


class PermisoGlobalAdminMixin:
    """Gatea el `ModelAdmin` por `permiso_codigo` (alcance GLOBAL).

    Generalización de lo que en 1.1 era `CatalogoAutorizadoAdminMixin`
    (hardcodeaba `"catalogo.administrar"`) — 1.2 necesita el mismo mecanismo
    para un permiso distinto (`formulario.administrar`, separado a
    propósito de `catalogo.administrar`: el Excel usa actores distintos
    para CU-010 ["Gestor de Servicios"] y CU-012/013 ["Gestor autorizado"]).
    En vez de duplicar las 4 comprobaciones por cada `ModelAdmin`, se
    parametriza con un atributo de clase.
    """

    permiso_codigo = None

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


@admin.register(Categoria)
class CategoriaAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    permiso_codigo = "catalogo.administrar"
    list_display = ("nombre", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre",)


class ServicioVisibilidadInline(admin.TabularInline):
    model = ServicioVisibilidad
    extra = 1


class ServicioResponsableInline(admin.TabularInline):
    model = ServicioResponsable
    extra = 1


@admin.register(Servicio)
class ServicioAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    permiso_codigo = "catalogo.administrar"
    list_display = ("nombre", "categoria", "formulario", "alcance_visibilidad", "activo")
    list_filter = ("categoria", "alcance_visibilidad", "activo")
    search_fields = ("nombre",)
    inlines = [ServicioVisibilidadInline, ServicioResponsableInline]

    def save_formset(self, request, form, formset, change):
        """`ServicioVisibilidad`/`ServicioResponsable` son inlines, no pasan
        por `save_model` — sin este override quedarían sin auditar (RQF-120).
        """
        if formset.model not in (ServicioVisibilidad, ServicioResponsable):
            super().save_formset(request, form, formset, change)
            return
        guardar_formset_auditado(request, formset)


class FormularioVersionInlineSoloLectura(admin.TabularInline):
    """Muestra las versiones existentes de un `Formulario`. Sin alta manual:
    crear una versión es `crear_nueva_version_action` (clonado), nunca una
    fila en blanco.
    """

    model = FormularioVersion
    fields = ("numero", "estado", "creado_en")
    readonly_fields = fields
    extra = 0
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Formulario)
class FormularioAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    permiso_codigo = "formulario.administrar"
    list_display = ("nombre", "version_activa")
    search_fields = ("nombre",)
    readonly_fields = ("version_activa",)
    inlines = [FormularioVersionInlineSoloLectura]
    actions = ["crear_nueva_version_action"]

    @admin.action(description="Crear nueva versión borrador (clona la versión activa)")
    def crear_nueva_version_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Seleccione un único formulario.", level=messages.ERROR)
            return
        formulario = queryset.first()
        version = crear_nueva_version(formulario, actor=request.user)
        self.message_user(request, f"Versión {version.numero} (borrador) creada.", level=messages.SUCCESS)


class CampoInline(admin.TabularInline):
    model = Campo
    fields = ("orden", "tipo", "etiqueta", "obligatorio", "configuracion")
    extra = 1
    show_change_link = True

    def _version_editable(self, obj):
        return obj is None or obj.estado == FormularioVersion.Estado.BORRADOR

    def has_add_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_delete_permission(request, obj)


@admin.register(FormularioVersion)
class FormularioVersionAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    permiso_codigo = "formulario.administrar"
    list_display = ("formulario", "numero", "estado")
    list_filter = ("estado",)
    fields = ("formulario", "numero", "estado", "enlace_previsualizar")
    readonly_fields = ("formulario", "numero", "estado", "enlace_previsualizar")
    inlines = [CampoInline]
    actions = ["activar_version_action"]

    @admin.display(description="Previsualización")
    def enlace_previsualizar(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        url = reverse("formularios:previsualizar_version", args=[obj.pk])
        return format_html('<a href="{}" target="_blank">Previsualizar</a>', url)

    def has_add_permission(self, request):
        # Crear versión es `crear_nueva_version_action` en FormularioAdmin.
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.estado != FormularioVersion.Estado.BORRADOR:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Activar esta versión")
    def activar_version_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Seleccione una única versión.", level=messages.ERROR)
            return
        version = queryset.first()
        try:
            activar_version(version.formulario, version, actor=request.user)
        except ValueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return
        self.message_user(request, f"Versión {version.numero} activada.", level=messages.SUCCESS)


class OpcionCampoInline(admin.TabularInline):
    model = OpcionCampo
    fields = ("orden", "valor", "etiqueta")
    extra = 1

    def _version_editable(self, obj):
        return obj is None or obj.version.estado == FormularioVersion.Estado.BORRADOR

    def has_add_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._version_editable(obj) and super().has_delete_permission(request, obj)


@admin.register(Campo)
class CampoAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    permiso_codigo = "formulario.administrar"
    list_display = ("etiqueta", "version", "tipo", "obligatorio", "orden")
    list_filter = ("tipo",)
    inlines = [OpcionCampoInline]

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        return obj is None or obj.version.estado == FormularioVersion.Estado.BORRADOR

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        return obj is None or obj.version.estado == FormularioVersion.Estado.BORRADOR


@admin.register(ReglaCondicional)
class ReglaCondicionalAdmin(PermisoGlobalAdminMixin, AdminAuditableMixin, admin.ModelAdmin):
    permiso_codigo = "formulario.administrar"
    list_display = ("campo_origen", "operador", "valor", "efecto", "campo_objetivo")

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        return obj is None or obj.campo_origen.version.estado == FormularioVersion.Estado.BORRADOR

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        return obj is None or obj.campo_origen.version.estado == FormularioVersion.Estado.BORRADOR
