from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("servicios/", include("apps.catalogo.urls")),
    path("formularios/", include("apps.catalogo.urls_formularios")),
    path("tickets/", include("apps.tickets.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    import debug_toolbar

    urlpatterns += [path("__debug__/", include(debug_toolbar.urls))]

handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"
