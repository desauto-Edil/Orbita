"""Reemplaza el sitio por defecto de Django Admin por el de Órbita (`apps.core.admin_sitio.OrbitaAdminSite`).

Va en `INSTALLED_APPS` en lugar de `django.contrib.admin`: así cada `admin.site.register` / `@admin.register` del
proyecto queda protegido sin tocar los `admin.py` de cada app."""

from django.contrib.admin.apps import AdminConfig


class OrbitaAdminConfig(AdminConfig):
    default_site = "apps.core.admin_sitio.OrbitaAdminSite"
