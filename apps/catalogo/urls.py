from django.urls import path

from apps.catalogo import studio, views, views_ticket_general

app_name = "catalogo"

urlpatterns = [
    path("", views.catalogo_lista_view, name="lista"),
    # 4.C1 — administración del Ticket General (desde Diseñador › Servicios)
    path("ticket-general/habilitar/", views_ticket_general.habilitar_view, name="ticket_general_habilitar"),
    path("ticket-general/crear/", views_ticket_general.crear_view, name="ticket_general_crear"),
    path("ticket-general/designar/", views_ticket_general.designar_view, name="ticket_general_designar"),
    path("ticket-general/destinos/crear/", views_ticket_general.destino_crear_view, name="ticket_general_destino_crear"),
    path(
        "ticket-general/destinos/predeterminado/",
        views_ticket_general.destino_predeterminado_view, name="ticket_general_destino_predeterminado",
    ),
    path(
        "ticket-general/destinos/<int:pk>/responsable/",
        views_ticket_general.destino_responsable_view, name="ticket_general_destino_responsable",
    ),
    path(
        "ticket-general/destinos/<int:pk>/estado/",
        views_ticket_general.destino_estado_view, name="ticket_general_destino_estado",
    ),
    path("studio/", studio.studio_lista_view, name="studio_lista"),
    path("studio/nuevo/", studio.studio_crear_view, name="studio_crear"),
    path("<int:pk>/", views.catalogo_detalle_view, name="detalle"),
    path("<int:pk>/studio/", studio.studio_view, name="studio"),
    path("<int:pk>/studio/general/", studio.studio_general_guardar_view, name="studio_general_guardar"),
    path(
        "<int:pk>/studio/general/tiempo/",
        studio.studio_tiempo_objetivo_guardar_view, name="studio_tiempo_objetivo_guardar",
    ),
    path(
        "<int:pk>/studio/general/prorroga/",
        studio.studio_prorroga_guardar_view, name="studio_prorroga_guardar",
    ),
    # Entrada
    path("<int:pk>/studio/entrada/version/", studio.studio_entrada_version_view, name="studio_entrada_version"),
    path(
        "<int:pk>/studio/entrada/version/<int:version_id>/activar/",
        studio.studio_entrada_activar_view, name="studio_entrada_activar",
    ),
    path("<int:pk>/studio/entrada/campos/nuevo/", studio.studio_campo_guardar_view, name="studio_campo_crear"),
    path(
        "<int:pk>/studio/entrada/campos/<int:campo_id>/editar/",
        studio.studio_campo_guardar_view, name="studio_campo_editar",
    ),
    path(
        "<int:pk>/studio/entrada/campos/<int:campo_id>/eliminar/",
        studio.studio_campo_eliminar_view, name="studio_campo_eliminar",
    ),
    path(
        "<int:pk>/studio/entrada/campos/<int:campo_id>/opciones/nueva/",
        studio.studio_opcion_guardar_view, name="studio_opcion_crear",
    ),
    path(
        "<int:pk>/studio/entrada/campos/<int:campo_id>/opciones/<int:opcion_id>/editar/",
        studio.studio_opcion_guardar_view, name="studio_opcion_editar",
    ),
    path(
        "<int:pk>/studio/entrada/campos/<int:campo_id>/opciones/<int:opcion_id>/eliminar/",
        studio.studio_opcion_eliminar_view, name="studio_opcion_eliminar",
    ),
    path("<int:pk>/studio/entrada/reglas/nueva/", studio.studio_regla_guardar_view, name="studio_regla_crear"),
    path(
        "<int:pk>/studio/entrada/reglas/<int:regla_id>/editar/",
        studio.studio_regla_guardar_view, name="studio_regla_editar",
    ),
    path(
        "<int:pk>/studio/entrada/reglas/<int:regla_id>/eliminar/",
        studio.studio_regla_eliminar_view, name="studio_regla_eliminar",
    ),
    # Ejecución
    path(
        "<int:pk>/studio/ejecucion/configurar/",
        studio.studio_ejecucion_configurar_view, name="studio_ejecucion_configurar",
    ),
    path(
        "<int:pk>/studio/ejecucion/activar-configuracion/",
        studio.studio_ejecucion_activar_configuracion_view,
        name="studio_ejecucion_activar_configuracion",
    ),
    path(
        "<int:pk>/studio/ejecucion/crear-flujo/",
        studio.studio_ejecucion_crear_flujo_view, name="studio_ejecucion_crear_flujo",
    ),
    path(
        "<int:pk>/studio/ejecucion/vincular/",
        studio.studio_ejecucion_vincular_view, name="studio_ejecucion_vincular",
    ),
    path(
        "<int:pk>/studio/ejecucion/copia/",
        studio.studio_ejecucion_copia_view, name="studio_ejecucion_copia",
    ),
    path("<int:pk>/studio/ejecucion/bloques/nuevo/", studio.studio_bloque_guardar_view, name="studio_bloque_crear"),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/editar/",
        studio.studio_bloque_guardar_view, name="studio_bloque_editar",
    ),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/eliminar/",
        studio.studio_bloque_eliminar_view, name="studio_bloque_eliminar",
    ),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/ruta/",
        studio.studio_ruta_aprobacion_guardar_view, name="studio_ruta_aprobacion_guardar",
    ),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/condiciones/nueva/",
        studio.studio_condicional_guardar_view, name="studio_condicional_crear",
    ),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/condiciones/<int:condicional_id>/editar/",
        studio.studio_condicional_guardar_view, name="studio_condicional_editar",
    ),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/condiciones/<int:condicional_id>/eliminar/",
        studio.studio_condicional_eliminar_view, name="studio_condicional_eliminar",
    ),
    path(
        "<int:pk>/studio/ejecucion/bloques/<int:bloque_id>/fallback/",
        studio.studio_fallback_guardar_view, name="studio_fallback_guardar",
    ),
    # Salida
    path("<int:pk>/studio/salida/nuevo/", studio.studio_entregable_guardar_view, name="studio_entregable_crear"),
    path(
        "<int:pk>/studio/salida/<int:definicion_id>/editar/",
        studio.studio_entregable_guardar_view, name="studio_entregable_editar",
    ),
    path(
        "<int:pk>/studio/salida/<int:definicion_id>/retirar/",
        studio.studio_entregable_retirar_view, name="studio_entregable_retirar",
    ),
    path("<int:pk>/studio/salida/entrega/", studio.studio_entrega_guardar_view, name="studio_entrega_guardar"),
    # Visibilidad (General) y responsables (Publicación) — 4.4.1
    path(
        "<int:pk>/studio/visibilidad/nueva/",
        studio.studio_visibilidad_conceder_view, name="studio_visibilidad_conceder",
    ),
    path(
        "<int:pk>/studio/visibilidad/<int:concesion_id>/retirar/",
        studio.studio_visibilidad_retirar_view, name="studio_visibilidad_retirar",
    ),
    path(
        "<int:pk>/studio/responsables/nuevo/",
        studio.studio_responsable_agregar_view, name="studio_responsable_agregar",
    ),
    path(
        "<int:pk>/studio/responsables/<int:responsable_id>/retirar/",
        studio.studio_responsable_retirar_view, name="studio_responsable_retirar",
    ),
    # Publicación
    path("<int:pk>/studio/terminos/nuevo/", studio.studio_termino_guardar_view, name="studio_termino_crear"),
    path(
        "<int:pk>/studio/terminos/<int:termino_id>/editar/",
        studio.studio_termino_guardar_view, name="studio_termino_editar",
    ),
    path(
        "<int:pk>/studio/terminos/<int:termino_id>/estado/",
        studio.studio_termino_estado_view, name="studio_termino_estado",
    ),
    path(
        "<int:pk>/studio/terminos/<int:termino_id>/eliminar/",
        studio.studio_termino_eliminar_view, name="studio_termino_eliminar",
    ),
    path("<int:pk>/studio/publicar/", studio.studio_publicar_view, name="studio_publicar"),
]
