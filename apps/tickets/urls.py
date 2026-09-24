from django.urls import path

from apps.tickets import views

app_name = "tickets"

urlpatterns = [
    path("", views.mis_borradores_view, name="mis_borradores"),
    path("nuevo/<int:servicio_id>/", views.iniciar_borrador_view, name="iniciar"),
    path("<int:pk>/borrador/", views.borrador_formulario_view, name="borrador"),
    path("<int:pk>/eliminar/", views.eliminar_borrador_view, name="eliminar"),
    path("archivos/<int:archivo_id>/", views.descargar_archivo_respuesta_view, name="descargar_archivo"),
]
