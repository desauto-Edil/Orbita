from django.urls import path

from apps.catalogo import views

app_name = "catalogo"

urlpatterns = [
    path("", views.catalogo_lista_view, name="lista"),
    path("<int:pk>/", views.catalogo_detalle_view, name="detalle"),
]
