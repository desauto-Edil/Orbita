from django.urls import path

from apps.workflows import views

app_name = "workflows"

urlpatterns = [
    path("", views.lista_view, name="lista"),
    path("nuevo/", views.crear_view, name="crear"),
    path("<int:pk>/", views.detalle_view, name="detalle"),
    path("<int:pk>/editar/", views.editar_view, name="editar"),
    path("<int:pk>/versiones/nueva/", views.crear_version_view, name="crear_version"),
    path("versiones/<int:pk>/", views.version_detalle_view, name="version_detalle"),
    path("versiones/<int:pk>/activar/", views.activar_version_view, name="activar_version"),
    # 3.UI.4 — Editor
    path("versiones/<int:version_pk>/etapas/nueva/", views.crear_etapa_view, name="crear_etapa"),
    path("etapas/<int:pk>/editar/", views.editar_etapa_view, name="editar_etapa"),
    path("etapas/<int:pk>/tipo/", views.cambiar_tipo_etapa_view, name="cambiar_tipo_etapa"),
    path("etapas/<int:pk>/configurar/", views.configurar_etapa_view, name="configurar_etapa"),
    path("etapas/<int:pk>/eliminar/", views.eliminar_etapa_view, name="eliminar_etapa"),
    path("etapas/<int:etapa_pk>/transiciones/nueva/", views.crear_transicion_view, name="crear_transicion"),
    path("transiciones/<int:pk>/editar/", views.editar_transicion_view, name="editar_transicion"),
    path("transiciones/<int:pk>/eliminar/", views.eliminar_transicion_view, name="eliminar_transicion"),
]
