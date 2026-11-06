from django.urls import path

from apps.notificaciones import views, views_studio

app_name = "notificaciones"

urlpatterns = [
    path("notificaciones/", views.centro_view, name="centro"),
    path("notificaciones/<int:pk>/abrir/", views.abrir_view, name="abrir"),
    path("notificaciones/<int:pk>/leida/", views.marcar_leida_view, name="leida"),
    path("notificaciones/leidas/", views.marcar_todas_view, name="todas_leidas"),
    path("notificaciones/preferencias/", views.preferencias_view, name="preferencias"),
    path("configuracion/notificaciones/", views.configuracion_view, name="configuracion"),
    path("servicios/<int:pk>/studio/notificaciones/base/", views_studio.avisos_base_view, name="studio_base"),
    path("servicios/<int:pk>/studio/notificaciones/reglas/nueva/", views_studio.regla_crear_view, name="studio_regla_crear"),
    path(
        "servicios/<int:pk>/studio/notificaciones/reglas/<int:regla_id>/retirar/",
        views_studio.regla_retirar_view, name="studio_regla_retirar",
    ),
    path("servicios/<int:pk>/studio/notificaciones/recordatorios/", views_studio.recordatorios_view, name="studio_recordatorios"),
    path("servicios/<int:pk>/studio/notificaciones/actividad/", views_studio.recordatorio_actividad_view, name="studio_actividad"),
]
