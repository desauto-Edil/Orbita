"""Rutas del Diseñador de Flujos (D2), montadas en `disenador/flujos/`.
La lista (biblioteca) vive en `apps.core` (`core:disenador_flujos`)."""

from django.urls import path

from apps.catalogo import flujos

app_name = "flujos"

urlpatterns = [
    path("nuevo/", flujos.nuevo_view, name="nuevo"),
    path("<int:pk>/", flujos.lienzo_view, name="lienzo"),
    path("<int:pk>/datos/", flujos.datos_view, name="datos"),
    path("<int:pk>/borrador/", flujos.preparar_view, name="preparar"),
    path("<int:pk>/publicar/", flujos.publicar_view, name="publicar"),
    path("<int:pk>/copia/", flujos.copia_view, name="copia"),
    path("<int:pk>/fases/nueva/", flujos.fase_guardar_view, name="fase_crear"),
    path("<int:pk>/fases/<int:fase_id>/editar/", flujos.fase_guardar_view, name="fase_editar"),
    path("<int:pk>/fases/<int:fase_id>/eliminar/", flujos.fase_eliminar_view, name="fase_eliminar"),
    path("<int:pk>/fases/<int:fase_id>/conectar/", flujos.fase_conectar_view, name="fase_conectar"),
    path(
        "<int:pk>/fases/conexiones/<int:transicion_id>/eliminar/",
        flujos.fase_desconectar_view,
        name="fase_desconectar",
    ),
    path("<int:pk>/bloques/nuevo/", flujos.bloque_guardar_view, name="bloque_crear"),
    path("<int:pk>/bloques/<int:bloque_id>/editar/", flujos.bloque_guardar_view, name="bloque_editar"),
    path("<int:pk>/bloques/<int:bloque_id>/eliminar/", flujos.bloque_eliminar_view, name="bloque_eliminar"),
    path("<int:pk>/bloques/<int:bloque_id>/rutas/", flujos.ruta_aprobacion_view, name="bloque_ruta"),
    path("<int:pk>/bloques/<int:bloque_id>/condiciones/nueva/", flujos.condicional_guardar_view, name="cond_crear"),
    path(
        "<int:pk>/bloques/<int:bloque_id>/condiciones/<int:condicional_id>/editar/",
        flujos.condicional_guardar_view, name="cond_editar",
    ),
    path(
        "<int:pk>/bloques/<int:bloque_id>/condiciones/<int:condicional_id>/eliminar/",
        flujos.condicional_eliminar_view, name="cond_eliminar",
    ),
    path("<int:pk>/bloques/<int:bloque_id>/fallback/", flujos.fallback_guardar_view, name="fallback"),
]
