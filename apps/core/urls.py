from django.urls import path

from apps.core import views
from apps.core import views_configuracion as config
from apps.catalogo import studio as catalogo_studio

app_name = "core"

urlpatterns = [
    path("", views.inicio_view, name="inicio"),
    path("salud/", views.salud, name="salud"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("perfil/", views.perfil_view, name="perfil"),
    path("explorar/", views.explorar_view, name="explorar"),
    path("necesito/", views.necesidad_view, name="necesidad"),
    path("mi-trabajo/", views.mi_trabajo_view, name="mi_trabajo"),
    path("sistema-visual/", views.sistema_visual_view, name="sistema_visual"),
    path("disenador/", views.disenador_view, name="disenador"),
    path("disenador/flujos/", views.disenador_flujos_view, name="disenador_flujos"),
    path("disenador/servicios/", views.disenador_servicios_view, name="disenador_servicios"),
    path("disenador/servicios/nuevo/", catalogo_studio.studio_crear_view, name="disenador_servicio_crear"),
    path("disenador/servicios/<int:pk>/", catalogo_studio.studio_view, name="disenador_servicio"),
    path("logo/", config.logo_view, name="logo"),
    path("configuracion/", config.configuracion_view, name="configuracion"),
    path("configuracion/general/", config.general_view, name="configuracion_general"),
    path("configuracion/usuarios/", config.usuarios_view, name="configuracion_usuarios"),
    path("configuracion/usuarios/nuevo/", config.usuario_crear_view, name="configuracion_usuario_crear"),
    path("configuracion/usuarios/<int:pk>/", config.usuario_view, name="configuracion_usuario"),
    path("configuracion/roles/", config.roles_view, name="configuracion_roles"),
    path("configuracion/roles/nuevo/", config.rol_view, name="configuracion_rol_crear"),
    path("configuracion/roles/<int:pk>/", config.rol_view, name="configuracion_rol"),
]

# Catálogos que comparten lista y formulario (`views_configuracion._catalogo`).
for _clave, _plural, _singular in (
    ("areas", "areas", "area"),
    ("unidades", "unidades", "unidad"),
    ("categorias", "categorias", "categoria"),
    ("permisos", None, "permiso"),
):
    _extra = {"catalogo": _clave}
    if _plural:
        urlpatterns.append(
            path(f"configuracion/{_clave}/", config.catalogo_view, _extra, name=f"configuracion_{_plural}")
        )
    urlpatterns += [
        path(f"configuracion/{_clave}/nuevo/", config.catalogo_item_view, _extra, name=f"configuracion_{_singular}_crear"),
        path(f"configuracion/{_clave}/<int:pk>/", config.catalogo_item_view, _extra, name=f"configuracion_{_singular}"),
    ]
