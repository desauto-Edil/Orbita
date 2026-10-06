from django.urls import path

from apps.tickets import views

app_name = "tickets"

urlpatterns = [
    path("", views.mis_tickets_view, name="mis_tickets"),
    path("cola/", views.cola_atencion_view, name="cola"),
    path("nuevo/<int:servicio_id>/", views.iniciar_borrador_view, name="iniciar"),
    # V2 — experiencia de solicitud: entrar → completar → revisar → enviar → confirmar
    path("solicitar/<int:servicio_id>/", views.solicitar_view, name="solicitar"),
    # 4.C1 — entrada explícita del Ticket General (no es un Servicio catalogado)
    path("general/", views.solicitar_general_view, name="ticket_general"),
    path("<int:pk>/borrador/", views.borrador_formulario_view, name="borrador"),
    path("<int:pk>/solicitud/estado/", views.solicitud_estado_view, name="solicitud_estado"),
    path("<int:pk>/revisar/", views.revisar_view, name="revisar"),
    path("<int:pk>/enviar/", views.enviar_view, name="enviar"),
    path("<int:pk>/enviada/", views.solicitud_enviada_view, name="enviada"),
    path("<int:pk>/radicar/", views.radicar_view, name="radicar"),
    path("<int:pk>/detalle/", views.detalle_view, name="detalle"),
    path("<int:pk>/tomar/", views.tomar_view, name="tomar"),
    path("<int:pk>/iniciar-atencion/", views.iniciar_atencion_view, name="iniciar_atencion"),
    path("<int:pk>/asignar/", views.asignar_view, name="asignar"),
    path("<int:pk>/reasignar/", views.reasignar_view, name="reasignar"),
    path("<int:pk>/resolver/", views.resolver_view, name="resolver"),
    # 4.A2 — prórrogas de la fecha objetivo (se resuelven en Aprobaciones)
    path("<int:pk>/prorroga/solicitar/", views.solicitar_prorroga_view, name="solicitar_prorroga"),
    path("<int:pk>/prorrogas/<int:prorroga_id>/cancelar/", views.cancelar_prorroga_view, name="cancelar_prorroga"),
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
    # 4.5 — entregables del responsable, entrega formal y respuesta del solicitante
    path(
        "<int:pk>/entregables/<int:entregable_id>/resultado/",
        views.entregable_resultado_view, name="entregable_resultado",
    ),
    path(
        "<int:pk>/entregables/<int:entregable_id>/confirmar/",
        views.entregable_confirmar_view, name="entregable_confirmar",
    ),
    path(
        "<int:pk>/entregables/<int:entregable_id>/archivos/",
        views.entregable_adjuntar_view, name="entregable_adjuntar",
    ),
    path(
        "<int:pk>/entregables/archivos/<int:adjunto_id>/retirar/",
        views.entregable_retirar_archivo_view, name="entregable_retirar_archivo",
    ),
    path("<int:pk>/entregar/", views.entregar_view, name="entregar"),
    path("<int:pk>/entrega/aceptar/", views.aceptar_entrega_view, name="aceptar_entrega"),
    path("<int:pk>/entrega/observar/", views.observar_entrega_view, name="observar_entrega"),
    path("archivos/<int:archivo_id>/", views.descargar_archivo_respuesta_view, name="descargar_archivo"),
    path("adjuntos/<int:adjunto_id>/descargar/", views.descargar_adjunto_view, name="descargar_adjunto"),
]
