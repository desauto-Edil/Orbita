from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Siempre enrutada, pero el sitio de Órbita responde 404 mientras ADMIN_HABILITADA esté apagada (sitio público).
    path("admin/", admin.site.urls),
    path("", include("apps.notificaciones.urls")),
    path("servicios/", include("apps.catalogo.urls")),
    path("formularios/", include("apps.catalogo.urls_formularios")),
    path("tickets/", include("apps.tickets.urls")),
    path("tareas/", include("apps.tareas.urls")),
    path("aprobaciones/", include("apps.aprobaciones.urls")),
    path("gacetas/", include("apps.gacetas.urls")),
    path("workflows/", include("apps.workflows.urls")),
    path("disenador/flujos/", include("apps.catalogo.urls_flujos")),
    path("", include("apps.core.urls")),
]

if "debug_toolbar" in settings.INSTALLED_APPS:
    import debug_toolbar

    urlpatterns += [path("__debug__/", include(debug_toolbar.urls))]

handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"
