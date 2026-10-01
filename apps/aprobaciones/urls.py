from django.urls import path

from apps.aprobaciones import views

app_name = "aprobaciones"

urlpatterns = [
    path("", views.bandeja_view, name="lista"),
    path("<int:pk>/", views.detalle_view, name="detalle"),
    path("<int:pk>/decidir/", views.decidir_view, name="decidir"),
    path("<int:pk>/reasignar/", views.reasignar_view, name="reasignar"),
]
