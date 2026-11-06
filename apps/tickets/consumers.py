"""WebSocket de la Conversación del ticket (Django Channels). Toda regla vive en `apps.tickets.conversacion`.

Flujo: al CONECTAR se autoriza la lectura (`puede_leer`) con la sesión de Django; quien no puede leer recibe un
cierre 4403 sin unirse al grupo (manipular el id del ticket no da acceso). Cada operación posterior vuelve a
autorizar: enviar, sincronizar y marcar leído revalidan en el momento; la entrega de cada difusión revalida la
participación como máximo cada `REVALIDAR_CADA` segundos (si perdió el acceso, se cierra la conexión). El cliente nunca envía el autor.

Mensajes del cliente (JSON): `{"tipo": "enviar", "texto", "cliente_id"}` · `{"tipo": "sincronizar", "despues"}`
(recuperación tras reconectar) · `{"tipo": "estado"}` (solo el contador) · `{"tipo": "leido", "hasta"}`. Respuestas: `mensaje` (nuevo o confirmado, con
`cliente_id` para que el emisor no lo duplique), `mensajes` (lote de sincronización), `no_leidos` y `error`."""

import time

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from django.core.exceptions import PermissionDenied, ValidationError

from apps.tickets import conversacion
from apps.tickets.models import Ticket

CIERRE_SIN_ACCESO = 4403
# Cada cuánto se vuelve a calcular la participación al ENTREGAR difusiones (la relación con las piezas del ticket
# cuesta varias consultas). Enviar, sincronizar y marcar leído siempre revalidan en el momento.
REVALIDAR_CADA = 30


class ConversacionConsumer(JsonWebsocketConsumer):
    def connect(self):
        self.ticket_id = int(self.scope["url_route"]["kwargs"]["pk"])
        self.grupo = None
        ticket = self._ticket()
        if ticket is None or not conversacion.puede_leer(self.scope.get("user"), ticket):
            self.close(code=CIERRE_SIN_ACCESO)
            return
        self._validado_en = time.monotonic()
        self.grupo = conversacion.grupo_de(self.ticket_id)
        async_to_sync(self.channel_layer.group_add)(self.grupo, self.channel_name)
        self.accept()

    def disconnect(self, code):
        if self.grupo:
            async_to_sync(self.channel_layer.group_discard)(self.grupo, self.channel_name)

    def _ticket(self):
        return Ticket.objects.filter(pk=self.ticket_id).first()

    def _usuario(self):
        return self.scope.get("user")

    def receive_json(self, contenido, **kwargs):
        if not isinstance(contenido, dict):
            return
        ticket = self._ticket()
        usuario = self._usuario()
        if ticket is None or not conversacion.puede_leer(usuario, ticket):
            self.close(code=CIERRE_SIN_ACCESO)
            return
        tipo = contenido.get("tipo")
        if tipo == "enviar":
            self._enviar(ticket, usuario, contenido)
        elif tipo == "sincronizar":
            despues = _entero(contenido.get("despues"))
            self.send_json({"tipo": "mensajes", "mensajes": conversacion.mensajes(ticket, despues_de=despues)})
            self.send_json({"tipo": "no_leidos", "total": conversacion.no_leidos(usuario, ticket)})
        elif tipo == "estado":
            self.send_json({"tipo": "no_leidos", "total": conversacion.no_leidos(usuario, ticket)})
        elif tipo == "leido":
            total = conversacion.marcar_leido(usuario, ticket, _entero(contenido.get("hasta")))
            self.send_json({"tipo": "no_leidos", "total": total})

    def _enviar(self, ticket, usuario, contenido):
        cliente_id = str(contenido.get("cliente_id") or "")[:64]
        try:
            mensaje = conversacion.enviar(ticket, usuario, contenido.get("texto"))
        except PermissionDenied as error:
            self.send_json({"tipo": "error", "cliente_id": cliente_id, "detalle": str(error)})
            return
        except ValidationError as error:
            self.send_json({"tipo": "error", "cliente_id": cliente_id, "detalle": " ".join(error.messages)})
            return
        # Confirmación al emisor (con su `cliente_id`); al resto le llega por el grupo. El emisor también recibe
        # la difusión: el cliente descarta el duplicado por `id`.
        self.send_json({"tipo": "mensaje", "mensaje": mensaje, "cliente_id": cliente_id})

    # Difusión del grupo (`conversacion.difundir`).
    def mensaje_nuevo(self, evento):
        if time.monotonic() - getattr(self, "_validado_en", 0) > REVALIDAR_CADA:
            ticket = self._ticket()
            if ticket is None or not conversacion.puede_leer(self._usuario(), ticket):
                self.close(code=CIERRE_SIN_ACCESO)
                return
            self._validado_en = time.monotonic()
        self.send_json({"tipo": "mensaje", "mensaje": evento["mensaje"]})


def _entero(valor):
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    return numero if numero > 0 else None
