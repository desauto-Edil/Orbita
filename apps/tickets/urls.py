from django.urls import path

from apps.tickets import views

app_name = "tickets"

urlpatterns = [
    path("", views.mis_tickets_view, name="mis_tickets"),
    path("cola/", views.cola_atencion_view, name="cola"),
    path("nuevo/<int:servicio_id>/", views.iniciar_borrador_view, name="iniciar"),
    path("<int:pk>/borrador/", views.borrador_formulario_view, name="borrador"),
    path("<int:pk>/radicar/", views.radicar_view, name="radicar"),
    path("<int:pk>/detalle/", views.detalle_view, name="detalle"),
    path("<int:pk>/tomar/", views.tomar_view, name="tomar"),
    path("<int:pk>/asignar/", views.asignar_view, name="asignar"),
    path("<int:pk>/reasignar/", views.reasignar_view, name="reasignar"),
    path("<int:pk>/resolver/", views.resolver_view, name="resolver"),
    path("<int:pk>/cerrar/", views.cerrar_view, name="cerrar"),
    path("<int:pk>/cancelar/", views.cancelar_view, name="cancelar"),
    path("<int:pk>/reabrir/", views.reabrir_view, name="reabrir"),
    path("<int:pk>/eliminar/", views.eliminar_borrador_view, name="eliminar"),
    path("<int:pk>/comentar/", views.comentar_view, name="comentar"),
    path("<int:pk>/adjuntar/", views.adjuntar_view, name="adjuntar"),
    path("<int:pk>/solicitar-informacion/", views.solicitar_informacion_view, name="solicitar_informacion"),
    path(
        "<int:pk>/solicitudes/<int:solicitud_id>/responder/",
        views.responder_solicitud_view,
        name="responder_solicitud",
    ),
    path("archivos/<int:archivo_id>/", views.descargar_archivo_respuesta_view, name="descargar_archivo"),
    path("adjuntos/<int:adjunto_id>/descargar/", views.descargar_adjunto_view, name="descargar_adjunto"),
]
