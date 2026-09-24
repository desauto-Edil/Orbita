"""Rutas de la capacidad transversal Form Builder — deliberadamente
separadas de `apps/catalogo/urls.py` (que es Catálogo/Servicios, montado
bajo `/servicios/`). `Formulario` no es propiedad exclusiva del catálogo
(ver `apps/catalogo/models/formularios.py`), así que su única pantalla
propia (previsualización, CU-013) no vive bajo el prefijo `/servicios/`,
aunque el código siga técnicamente dentro de `apps.catalogo` en 1.2.
"""

from django.urls import path

from apps.catalogo import views

app_name = "formularios"

urlpatterns = [
    path("<int:version_id>/previsualizar/", views.previsualizar_version_view, name="previsualizar_version"),
]
