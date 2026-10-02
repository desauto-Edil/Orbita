from apps.core.navegacion import construir_navegacion


def navegacion(request):
    """Expone la navegación global a todos los templates: `nav_dock` y
    `nav_mas_grupos` (dock y panel "Más"), `nav_mas_activo`, `nav_trabajo_tabs`
    (pestañas locales de Trabajo) y, por compatibilidad, `nav_items` /
    `nav_item_activo`. Toda la lógica de qué ve cada usuario vive en
    `apps.core.navegacion`."""
    return construir_navegacion(request.user, request.resolver_match)
