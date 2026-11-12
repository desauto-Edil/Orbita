"""Administración avanzada SEGURA (Versión 1, etapa 4 del despliegue; requerimiento del 2026-11-12).

Django Admin queda solo para lo que aún no tiene interfaz propia (auditoría, catálogo técnico), con cuatro
protecciones:

1. APAGADA por defecto: con `ADMIN_HABILITADA=False` (el sitio público) toda ruta `/admin/…` responde 404. Solo la
   enciende el contenedor `admin`, publicado únicamente en 127.0.0.1 del servidor y al que se llega por túnel SSH.
2. Entra solo quien tiene `plataforma.administracion_avanzada` (GLOBAL), nunca por `is_staff`/`is_superuser`, Y
   confirmó su identidad con Google en esta misma sesión hace menos de `ADMIN_REAUTENTICACION_MINUTOS`
   (`apps.core.plataforma`). Si pasó más tiempo, se le pide volver a confirmar con Google.
3. NADA SE BORRA: ningún modelo ni inline ofrece eliminar y se retira la acción «Eliminar seleccionados» (en Órbita
   retirar es desactivar).
4. TODO CAMBIO QUEDA AUDITADO en `RegistroAuditoria`: los `ModelAdmin` que no lo hacían ya lo hacen aquí.

Los permisos de modelo de Django (`view_*`, `add_*`, `change_*`) que usan los `ModelAdmin` de `apps.core` los
concede `PermisosAdministracionAvanzada` a quien tiene el permiso de plataforma; `delete_*`, nunca."""

from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.backends import BaseBackend
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render, resolve_url
from django.urls import reverse

from apps.core import plataforma
from apps.core.auditoria import guardar_formset_auditado, registrar_evento, serializar


def habilitada():
    return bool(getattr(settings, "ADMIN_HABILITADA", False))


class _ProteccionOrbita:
    """Se antepone a cada `ModelAdmin` registrado en el sitio de Órbita."""

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        acciones = super().get_actions(request)
        acciones.pop("delete_selected", None)
        return acciones

    def get_inline_instances(self, request, obj=None):
        instancias = super().get_inline_instances(request, obj)
        for inline in instancias:
            inline.has_delete_permission = lambda *args, **kwargs: False
        return instancias

    def save_model(self, request, obj, form, change):
        if getattr(self, "_audita_cambios", False):  # `AdminAuditableMixin` ya lo registra
            return super().save_model(request, obj, form, change)
        anterior = None
        if change:
            previa = type(obj).objects.filter(pk=obj.pk).first()
            anterior = serializar(previa) if previa is not None else None
        super().save_model(request, obj, form, change)
        from apps.core.models import RegistroAuditoria

        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR if change else RegistroAuditoria.Accion.CREAR,
            instancia=obj, origen=RegistroAuditoria.Origen.USUARIO, usuario=request.user,
            datos_anteriores=anterior, datos_nuevos=serializar(obj),
        )

    def save_formset(self, request, form, formset, change):
        if getattr(self, "_orbita_formset_propio", False):  # el ModelAdmin ya decide (y audita) su formset
            return super().save_formset(request, form, formset, change)
        guardar_formset_auditado(request, formset)


class OrbitaAdminSite(admin.AdminSite):
    site_header = "Órbita · Administración avanzada"
    site_title = "Órbita · Administración avanzada"
    index_title = "Administración avanzada"

    def has_permission(self, request):
        return (
            habilitada()
            and plataforma.administra_plataforma(request.user)
            and plataforma.ingreso_google_reciente(request)
        )

    def admin_view(self, view, cacheable=False):
        protegida = super().admin_view(view, cacheable)

        @wraps(view)
        def vista(request, *args, **kwargs):
            if not habilitada():
                raise Http404
            return protegida(request, *args, **kwargs)

        return vista

    def login(self, request, extra_context=None):
        """Sin formulario de contraseña: se entra con Google. Con sesión y permiso pero sin un ingreso reciente,
        se pide confirmar la identidad con Google (vuelve con `next`)."""
        if not habilitada():
            raise Http404
        from apps.core.redirecciones import destino_interno

        destino = destino_interno(request, request.GET.get("next")) or reverse("admin:index", current_app=self.name)
        if not request.user.is_authenticated:
            return redirect(f"{resolve_url(settings.LOGIN_URL)}?{urlencode({'next': destino})}")
        if not plataforma.administra_plataforma(request.user):
            raise PermissionDenied
        if plataforma.ingreso_google_reciente(request):
            return redirect(destino)
        return render(request, "admin/orbita_confirmar_identidad.html", {
            "next": destino, "minutos": plataforma.minutos_de_reautenticacion(),
            "titulo_pagina": "Confirma tu identidad",
        })

    def register(self, model_or_iterable, admin_class=None, **options):
        base = admin_class or admin.ModelAdmin
        protegida = type(base.__name__, (_ProteccionOrbita, base), {
            "__module__": base.__module__,
            "_orbita_formset_propio": base.save_formset is not admin.ModelAdmin.save_formset,
        })
        return super().register(model_or_iterable, protegida, **options)


class PermisosAdministracionAvanzada(BaseBackend):
    """Permisos de modelo de Django para la administración avanzada: los tiene quien tiene el permiso de
    plataforma; eliminar, nadie. No autentica a nadie (el único ingreso es Google)."""

    def has_perm(self, user_obj, perm, obj=None):
        if perm.partition(".")[2].startswith("delete_"):
            return False
        return plataforma.administra_plataforma(user_obj)

    def has_module_perms(self, user_obj, app_label):
        return plataforma.administra_plataforma(user_obj)
