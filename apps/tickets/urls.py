from django.urls import path

from apps.tickets import views

app_name = "tickets"

urlpatterns = [
    path("", views.mis_tickets_view, name="mis_tickets"),
    path("nuevo/<int:servicio_id>/", views.iniciar_borrador_view, name="iniciar"),
    path("<int:pk>/borrador/", views.borrador_formulario_view, name="borrador"),
    path("<int:pk>/radicar/", views.radicar_view, name="radicar"),
    path("<int:pk>/detalle/", views.detalle_view, name="detalle"),
    path("<int:pk>/eliminar/", views.eliminar_borrador_view, name="eliminar"),
    path("archivos/<int:archivo_id>/", views.descargar_archivo_respuesta_view, name="descargar_archivo"),
]
