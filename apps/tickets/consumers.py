"""WebSocket de la Conversación del ticket (Django Channels). Toda regla vive en `apps.tickets.conversacion`.

Una conexión por ticket. Al CONECTAR se calculan los canales que la persona puede leer (`conversacion.canales_de`:
equipo y/o interesados) con la sesión de Django y se une SOLO a los grupos de esos canales; sin ninguno, cierre 4403
(manipular el id del ticket no da acceso). Cada operación posterior vuelve a autorizar el canal que nombra (enviar,
sincronizar y marcar leído, en el momento); la entrega de cada difusión revalida los canales como máximo cada
`REVALIDAR_CADA` segundos y descarta lo de un canal que la persona ya no puede leer. El cliente nunca envía el autor.

Mensajes del cliente (JSON, todos con `canal`): `{"tipo": "enviar", "canal", "texto", "cliente_id"}` ·
`{"tipo": "sincronizar", "canal", "despues"}` (recuperación tras reconectar) · `{"tipo": "estado"}` (contadores) ·
`{"tipo": "leido", "canal", "hasta"}`. Respuestas: `mensaje` (con su `canal`; `cliente_id` para que el emisor no lo
duplique), `mensajes`, `no_leidos` (por canal) y `error`."""

import time

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from django.core.exceptions import PermissionDenied, ValidationError

from apps.tickets import conversacion
from apps.tickets.models import Ticket

CIERRE_SIN_ACCESO = 4403
REVALIDAR_CADA = 30


class ConversacionConsumer(JsonWebsocketConsumer):
    def connect(self):
        self.ticket_id = int(self.scope["url_route"]["kwargs"]["pk"])
        self.grupos = []
        ticket = self._ticket()
        canales = conversacion.canales_de(self.scope.get("user"), ticket) if ticket is not None else []
        if not canales:
            self.close(code=CIERRE_SIN_ACCESO)
            return
        self._fijar_canales(canales)
        for canal in canales:
            grupo = conversacion.grupo_de(self.ticket_id, canal)
            async_to_sync(self.channel_layer.group_add)(grupo, self.channel_name)
            self.grupos.append(grupo)
        self.accept()

    def disconnect(self, code):
        for grupo in getattr(self, "grupos", []):
            async_to_sync(self.channel_layer.group_discard)(grupo, self.channel_name)

    def _ticket(self):
        return Ticket.objects.filter(pk=self.ticket_id).first()

    def _usuario(self):
        return self.scope.get("user")

    def _fijar_canales(self, canales):
        self.canales = set(canales)
        self._validado_en = time.monotonic()

    def receive_json(self, contenido, **kwargs):
        if not isinstance(contenido, dict):
            return
        ticket = self._ticket()
        usuario = self._usuario()
        canales = conversacion.canales_de(usuario, ticket) if ticket is not None else []
        if not canales:
            self.close(code=CIERRE_SIN_ACCESO)
            return
        self._fijar_canales(canales)
        tipo = contenido.get("tipo")
        if tipo == "estado":
            for canal in canales:
                self.send_json({"tipo": "no_leidos", "canal": canal, "total": conversacion.no_leidos(usuario, ticket, canal)})
            return
        canal = contenido.get("canal")
        if canal not in canales:
            self.send_json({"tipo": "error", "cliente_id": str(contenido.get("cliente_id") or "")[:64],
                            "detalle": "No participas en esta conversación."})
            return
        if tipo == "enviar":
            self._enviar(ticket, usuario, canal, contenido)
        elif tipo == "sincronizar":
            despues = _entero(contenido.get("despues"))
            self.send_json({"tipo": "mensajes", "canal": canal, "mensajes": conversacion.mensajes(ticket, canal, despues_de=despues)})
            self.send_json({"tipo": "no_leidos", "canal": canal, "total": conversacion.no_leidos(usuario, ticket, canal)})
        elif tipo == "leido":
            total = conversacion.marcar_leido(usuario, ticket, canal, _entero(contenido.get("hasta")))
            self.send_json({"tipo": "no_leidos", "canal": canal, "total": total})

    def _enviar(self, ticket, usuario, canal, contenido):
        cliente_id = str(contenido.get("cliente_id") or "")[:64]
        try:
            mensaje = conversacion.enviar(ticket, usuario, contenido.get("texto"), canal)
        except PermissionDenied as error:
            self.send_json({"tipo": "error", "cliente_id": cliente_id, "detalle": str(error)})
            return
        except ValidationError as error:
            self.send_json({"tipo": "error", "cliente_id": cliente_id, "detalle": " ".join(error.messages)})
            return
        # Confirmación al emisor (con su `cliente_id`); al resto le llega por el grupo del canal. El emisor también
        # recibe la difusión: el cliente descarta el duplicado por `id`.
        self.send_json({"tipo": "mensaje", "mensaje": mensaje, "cliente_id": cliente_id})

    # Difusión del grupo de un canal (`conversacion.difundir`).
    def mensaje_nuevo(self, evento):
        if time.monotonic() - getattr(self, "_validado_en", 0) > REVALIDAR_CADA:
            ticket = self._ticket()
            canales = conversacion.canales_de(self._usuario(), ticket) if ticket is not None else []
            if not canales:
                self.close(code=CIERRE_SIN_ACCESO)
                return
            self._fijar_canales(canales)
        if evento["mensaje"].get("canal") in self.canales:
            self.send_json({"tipo": "mensaje", "mensaje": evento["mensaje"]})


def _entero(valor):
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    return numero if numero > 0 else None
