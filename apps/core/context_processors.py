from apps.core.navegacion import elementos_navegacion, item_activo


def navegacion(request):
    """Expone `nav_items`/`nav_item_activo` a todos los templates — los usa
    `layout/dock.html` para renderizar el dock y resaltar el ítem activo."""
    if not request.user.is_authenticated:
        return {"nav_items": [], "nav_item_activo": None}
    items = elementos_navegacion(request.user)
    return {
        "nav_items": items,
        "nav_item_activo": item_activo(items, request.resolver_match),
    }
