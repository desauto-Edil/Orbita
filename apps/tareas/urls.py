from django.urls import path

from apps.tareas import views

app_name = "tareas"

urlpatterns = [
    path("", views.bandeja_view, name="lista"),
    path("<int:pk>/", views.detalle_view, name="detalle"),
    path("<int:pk>/vista/", views.vista_previa_view, name="vista_previa"),
    path("<int:pk>/tomar/", views.tomar_view, name="tomar"),
    path("<int:pk>/iniciar/", views.iniciar_view, name="iniciar"),
    path("<int:pk>/completar/", views.completar_view, name="completar"),
    path("<int:pk>/asignar/", views.asignar_view, name="asignar"),
    path("<int:pk>/reasignar/", views.reasignar_view, name="reasignar"),
    path("<int:pk>/delegar/", views.delegar_view, name="delegar"),
    path("<int:pk>/subtareas/nueva/", views.crear_subtarea_view, name="crear_subtarea"),
    path("<int:pk>/comentar/", views.comentar_view, name="comentar"),
    path("<int:pk>/evidencia/", views.adjuntar_evidencia_view, name="adjuntar_evidencia"),
    path("evidencias/<int:adjunto_id>/descargar/", views.descargar_evidencia_view, name="descargar_evidencia"),
]
