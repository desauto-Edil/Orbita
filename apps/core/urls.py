from django.urls import path

from apps.core import views
from apps.catalogo import studio as catalogo_studio

app_name = "core"

urlpatterns = [
    path("", views.inicio_view, name="inicio"),
    path("salud/", views.salud, name="salud"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("perfil/", views.perfil_view, name="perfil"),
    path("explorar/", views.explorar_view, name="explorar"),
    path("mi-trabajo/", views.mi_trabajo_view, name="mi_trabajo"),
    path("sistema-visual/", views.sistema_visual_view, name="sistema_visual"),
    path("disenador/", views.disenador_view, name="disenador"),
    path("disenador/flujos/", views.disenador_flujos_view, name="disenador_flujos"),
    path("disenador/servicios/", views.disenador_servicios_view, name="disenador_servicios"),
    path("disenador/servicios/nuevo/", catalogo_studio.studio_crear_view, name="disenador_servicio_crear"),
    path("disenador/servicios/<int:pk>/", catalogo_studio.studio_view, name="disenador_servicio"),
]
