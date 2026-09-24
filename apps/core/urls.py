from django.urls import path

from apps.core import views

app_name = "core"

urlpatterns = [
    path("", views.inicio_view, name="inicio"),
    path("salud/", views.salud, name="salud"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("perfil/", views.perfil_view, name="perfil"),
]
