"""Punto de entrada ASGI: HTTP (Django) y WebSocket (Django Channels).

El WebSocket solo atiende la Conversación del ticket (`apps.tickets.routing`). Valida el origen contra
`ALLOWED_HOSTS` y autentica con la sesión de Django; cada consumidor vuelve a autorizar al conectar y en cada
operación."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Django debe inicializarse antes de importar código que use modelos (rutas y consumidores).
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from apps.tickets.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    }
)
