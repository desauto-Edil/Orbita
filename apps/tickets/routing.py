"""Rutas WebSocket de tickets (Django Channels): solo la Conversación del ticket."""

from django.urls import path

from apps.tickets.consumers import ConversacionConsumer

websocket_urlpatterns = [
    path("ws/tickets/<int:pk>/conversacion/", ConversacionConsumer.as_asgi()),
]
