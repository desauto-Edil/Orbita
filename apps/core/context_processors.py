from apps.core.navegacion import elementos_navegacion


def navegacion(request):
    """Expone `nav_items` a todos los templates — lo usa `layout/sidebar.html`."""
    if not request.user.is_authenticated:
        return {"nav_items": []}
    return {"nav_items": elementos_navegacion(request.user)}
