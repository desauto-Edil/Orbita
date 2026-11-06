"""Conversación del ticket — chat de texto en tiempo real (CU-018/RQF-057 sobre `ComentarioTicket`).

Una conversación por TICKET (no por fase ni por actividad): hablan TODOS los involucrados. No hay modelo de
mensajes nuevo: cada mensaje es un `ComentarioTicket` registrado por el mismo núcleo de `operaciones.comentar_ticket`
(`operaciones.registrar_comentario`: guarda y publica el aviso COMENTARIO). Este módulo solo añade:

- Participación (decisión explícita del usuario: el chat es del ticket entero, no de una fase): participa quien
  CONSULTA el ticket (`puede_consultar_ticket`: solicitante, responsable y su equipo, supervisión por alcance) o
  tiene una relación directa con una de sus piezas — actividad, aprobación, gaceta, colección o revisión por
  conjunto —, exactamente la población que entra al espacio del ticket (`espacio.relacion(...).puede_abrir`).
  LEER = participar; ESCRIBIR = participar mientras el ticket esté RADICADO o EN_ATENCION. Los comentarios con
  adjuntos de la vista de detalle siguen con `puede_comentar_ticket` (sin cambios).
- Texto plano: sin adjuntos en el chat, longitud máxima `LONGITUD_MAXIMA`. El autor es siempre la persona
  autenticada (nunca un dato del cliente) y el cliente pinta el texto como texto (sin HTML).
- Difusión: tras confirmar la transacción, el mensaje se publica en el grupo de canales del ticket
  (`grupo_de`). Redis solo distribuye; si la difusión falla el mensaje ya está guardado y se recupera al
  reconectar (`mensajes(..., despues_de=)`).
- No leídos: `LecturaConversacion` (marca «leído hasta»); no leídos = comentarios de OTRAS personas posteriores
  a la marca. Marcar leído también marca como leídos los avisos COMENTARIO de ese ticket (no se duplica el
  mecanismo: la campana deja de contar lo que ya se vio en el chat)."""

import logging

from asgiref.sync import async_to_sync
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.tickets.autorizacion import puede_consultar_ticket
from apps.tickets.models import ComentarioTicket, LecturaConversacion, Ticket

logger = logging.getLogger(__name__)

LONGITUD_MAXIMA = 2000
LIMITE_CARGA = 200  # mensajes que se entregan por petición (los más recientes)


def grupo_de(ticket_id):
    """Nombre del grupo de canales de la conversación de un ticket (solo letras, números y puntos)."""
    return f"conversacion.ticket.{int(ticket_id)}"


ESTADOS_ABIERTOS = (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)


def participa(usuario, ticket, rel=None):
    """Quien consulta el ticket o interviene en una de sus piezas (`rel`: la relación ya calculada, si la hay)."""
    if not getattr(usuario, "is_authenticated", False) or ticket.estado == Ticket.Estado.BORRADOR:
        return False
    if puede_consultar_ticket(usuario, ticket):
        return True
    if rel is None:
        from apps.tickets.espacio import relacion

        rel = relacion(usuario, ticket)
    return rel.puede_abrir


def puede_leer(usuario, ticket, rel=None):
    return participa(usuario, ticket, rel)


def puede_escribir(usuario, ticket, rel=None):
    return ticket.estado in ESTADOS_ABIERTOS and participa(usuario, ticket, rel)


def _nombre(usuario):
    return usuario.get_full_name() or usuario.get_username()


def serializar(comentario):
    """Representación del mensaje para el cliente: solo datos de lectura, el texto tal cual (el cliente lo pinta
    con `textContent`, así que no se interpreta HTML)."""
    autor = comentario.autor
    return {
        "id": comentario.pk,
        "autor_id": autor.pk,
        "autor": _nombre(autor),
        "inicial": (autor.get_username()[:1] or "?").upper(),
        "foto": getattr(autor, "google_foto_url", "") or "",
        "texto": comentario.contenido,
        "creado_en": timezone.localtime(comentario.creado_en).isoformat(),
    }


def mensajes(ticket, *, despues_de=None):
    """Mensajes del ticket en orden cronológico. Sin `despues_de`, los últimos `LIMITE_CARGA`; con él, los
    posteriores a ese id (recuperación tras reconectar)."""
    consulta = ComentarioTicket.objects.filter(ticket=ticket).select_related("autor")
    if despues_de:
        return [serializar(c) for c in consulta.filter(pk__gt=despues_de).order_by("pk")[:LIMITE_CARGA]]
    recientes = list(consulta.order_by("-pk")[:LIMITE_CARGA])
    return [serializar(c) for c in reversed(recientes)]


def validar_texto(texto):
    texto = (texto or "").strip()
    if not texto:
        raise ValidationError("El mensaje no puede estar vacío.")
    if len(texto) > LONGITUD_MAXIMA:
        raise ValidationError(f"El mensaje supera los {LONGITUD_MAXIMA} caracteres.")
    return texto


def difundir(ticket_id, mensaje):
    """Publica el mensaje al grupo del ticket. Nunca rompe la operación: el mensaje ya quedó guardado."""
    from channels.layers import get_channel_layer

    capa = get_channel_layer()
    if capa is None:
        return
    try:
        async_to_sync(capa.group_send)(grupo_de(ticket_id), {"type": "mensaje.nuevo", "mensaje": mensaje})
    except Exception:  # noqa: BLE001 — la difusión es una mejora; la recuperación al reconectar cubre la falla
        logger.exception("No se pudo difundir el mensaje %s del ticket %s", mensaje.get("id"), ticket_id)


@transaction.atomic
def enviar(ticket, actor, texto):
    """Guarda un mensaje de texto (núcleo de `comentar_ticket`) y lo difunde al confirmar."""
    from apps.tickets.operaciones import registrar_comentario

    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if not puede_escribir(actor, ticket):
        raise PermissionDenied("No puedes escribir en la conversación de este ticket.")
    comentario = registrar_comentario(ticket, actor, validar_texto(texto))
    mensaje = serializar(comentario)
    transaction.on_commit(lambda: difundir(ticket.pk, mensaje))
    _avanzar_marca(actor, ticket, comentario.pk)  # lo propio nunca cuenta como no leído
    return mensaje


def _avanzar_marca(usuario, ticket, hasta_id):
    lectura, _creada = LecturaConversacion.objects.get_or_create(usuario=usuario, ticket=ticket)
    if hasta_id > lectura.ultimo_leido_id:
        lectura.ultimo_leido_id = hasta_id
        lectura.save(update_fields=["ultimo_leido_id", "actualizado_en"])


def marcar_leido(usuario, ticket, hasta_id=None):
    """Regla explícita: se llama cuando la persona VE la pestaña Conversación abierta. Avanza la marca hasta
    `hasta_id` (o el último mensaje) y marca como leídos los avisos COMENTARIO de ese ticket."""
    if not puede_leer(usuario, ticket):
        raise PermissionDenied("No puedes consultar la conversación de este ticket.")
    ultimo = ComentarioTicket.objects.filter(ticket=ticket).order_by("-pk").values_list("pk", flat=True).first() or 0
    hasta = min(int(hasta_id), ultimo) if hasta_id else ultimo
    _avanzar_marca(usuario, ticket, hasta)
    from apps.notificaciones.models import Notificacion

    Notificacion.objects.filter(
        destinatario=usuario, ticket=ticket, evento="COMENTARIO", leida_en__isnull=True
    ).update(leida_en=timezone.now())
    return no_leidos(usuario, ticket)


def no_leidos(usuario, ticket):
    """Comentarios de OTRAS personas posteriores a la marca de lectura de `usuario`."""
    marca = (
        LecturaConversacion.objects.filter(usuario=usuario, ticket=ticket).values_list("ultimo_leido_id", flat=True).first()
        or 0
    )
    return ComentarioTicket.objects.filter(ticket=ticket, pk__gt=marca).exclude(autor=usuario).count()


def contexto(usuario, ticket, rel=None):
    """Lo que necesita el panel al pintarse (sin mensajes: se cargan al abrir la conversación)."""
    lee = puede_leer(usuario, ticket, rel)
    return {
        "puede_leer": lee,
        "puede_escribir": lee and ticket.estado in ESTADOS_ABIERTOS,
        "no_leidos": no_leidos(usuario, ticket) if lee else 0,
        "longitud_maxima": LONGITUD_MAXIMA,
    }
