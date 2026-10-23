from django.urls import path

from apps.gacetas import views

app_name = "gacetas"

urlpatterns = [
    path("", views.lista_view, name="lista"),
    path("<int:pk>/", views.detalle_view, name="detalle"),
    path("<int:pk>/presentar/", views.presentar_view, name="presentar"),
    path("<int:pk>/cerrar-periodo/", views.cerrar_periodo_view, name="cerrar_periodo"),
    path("<int:pk>/resolver/", views.resolver_view, name="resolver"),
    path("participacion/<int:pk>/observar/", views.observar_view, name="observar"),
    path("participacion/<int:pk>/reenviar/", views.reenviar_view, name="reenviar"),
    path("participacion/<int:pk>/excluir/", views.excluir_view, name="excluir"),
    path("observacion/<int:pk>/responder/", views.responder_view, name="responder"),
]
