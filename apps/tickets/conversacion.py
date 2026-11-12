"""Conversación del ticket — chat de texto en tiempo real, en DOS canales aislados (CU-018/RQF-057).

Es el ÚNICO medio de conversación del ticket (la sección de comentarios de la vista completa se retiró). Cada
mensaje es un `ComentarioTicket` con su `canal`, guardado por `operaciones.registrar_comentario` (que publica el aviso
COMENTARIO). No hay otro modelo de mensajes.

Canales (decisión explícita del usuario: hay dos conversaciones distintas por ticket):

- EQUIPO — quienes DESARROLLAN el servicio o proceso: el responsable y su equipo, quien puede tomarlo, los
  responsables de sus actividades, los aprobadores del flujo, los participantes y revisores de sus colecciones, quien
  coordina sus gacetas y la supervisión por alcance (salvo que en ESTE ticket actúe como revisora de una gaceta).
  Aquí, p. ej., el diseñador de una pieza habla con su aprobador.
- INTERESADOS — el equipo con los USUARIOS FINALES: quien solicitó el ticket y quienes revisan o aprueban sus
  gacetas. Quien es solo interesado lee y escribe aquí y NUNCA lee el canal del equipo.

La pertenencia sale de la relación real con el ticket (`espacio.relacion`), nunca del rol. LEER un canal = pertenecer
a él; ESCRIBIR = pertenecer y que el ticket esté RADICADO o EN_ATENCION. Texto plano (máx. `LONGITUD_MAXIMA`; los
emojis son texto), con ARCHIVOS opcionales (hasta `MAX_ARCHIVOS` de `TAMANO_MAXIMO_MB` cada uno, `Adjunto` de la rama
COMENTARIO; se descargan solo si se puede leer ese canal: `puede_ver_archivo`); el autor es la sesión y el cliente
pinta con `textContent`. PRESENCIA: `participantes` dice quién del canal tiene Órbita abierta ahora
(`apps.core.presencia`); un mensaje a alguien fuera de línea que siga sin leer unos minutos después le llega por
correo, junto con los demás de ese canal (`apps.notificaciones.correo_conversacion`). Tras confirmar la transacción, el mensaje se difunde al
grupo de SU canal (`grupo_de`); Redis solo distribuye y lo perdido se recupera al reconectar. No leídos:
`LecturaConversacion` por persona, ticket y canal; marcar leído también marca los avisos COMENTARIO de ese canal
(el aviso lleva el canal en sus datos clave: «Conversación: Equipo|Interesados»)."""

import logging

from asgiref.sync import async_to_sync
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.tickets.models import ComentarioTicket, LecturaConversacion, Ticket

logger = logging.getLogger(__name__)

EQUIPO = ComentarioTicket.Canal.EQUIPO
INTERESADOS = ComentarioTicket.Canal.INTERESADOS
CANALES = (EQUIPO, INTERESADOS)
ETIQUETAS = {str(k): str(v) for k, v in ComentarioTicket.Canal.choices}
HECHO_CANAL = "Conversación"
LONGITUD_MAXIMA = 2000
# Archivos por mensaje: un límite propio de la conversación (no hay uno de negocio para adjuntos operativos).
MAX_ARCHIVOS = 5
TAMANO_MAXIMO_MB = 25
LIMITE_CARGA = 200  # mensajes que se entregan por petición (los más recientes)
ESTADOS_ABIERTOS = (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)


def grupo_de(ticket_id, canal):
    """Grupo de canales de la conversación de un ticket en un canal (solo letras, números y puntos)."""
    return f"conversacion.ticket.{int(ticket_id)}.{str(canal).lower()}"


# --- Pertenencia ----------------------------------------------------------------------------------------------

def _trabaja_el_ticket(usuario, ticket, rel):
    """Relación DIRECTA con el desarrollo del ticket: responsable o miembro de su equipo, quien puede tomarlo o
    iniciar su atención, responsable de una actividad, aprobador del flujo, quien opera o participa en una colección,
    revisor por conjunto o coordinador de una gaceta."""
    from apps.tickets.autorizacion import puede_iniciar_atencion, puede_tomar

    if rel.responsable or rel.tareas or rel.conjuntos:
        return True
    if any(c["participante"] or c["opera"] for c in rel.colecciones):
        return True
    if any(g["coordina"] for g in rel.gacetas):
        return True
    # Aprobar en el FLUJO es del equipo; aprobar la ronda de una gaceta es revisar como interesado.
    if any(not hasattr(a.esquema, "ronda_gaceta") for a in rel.aprobaciones):
        return True
    return ticket.estado == Ticket.Estado.RADICADO and (puede_tomar(usuario, ticket) or puede_iniciar_atencion(usuario, ticket))


def canales_de(usuario, ticket, rel=None):
    """Los canales que `usuario` puede leer en el ticket, en orden (Equipo, Interesados).

    EQUIPO: quien trabaja el ticket directamente (`_trabaja_el_ticket`) y la supervisión por alcance (`rel.atiende`:
    asignar / reasignar), SALVO que en este ticket esa persona actúe como revisora o aprobadora de una gaceta: ahí su
    papel es el de un interesado que revisa, y no lee lo que el equipo habla internamente.
    INTERESADOS: todo el equipo, quien solicitó y quienes revisan o aprueban sus gacetas."""
    if not getattr(usuario, "is_authenticated", False) or ticket.estado == Ticket.Estado.BORRADOR:
        return []
    if rel is None:
        from apps.tickets.espacio import relacion

        rel = relacion(usuario, ticket)
    revisa_gaceta = any(g["revisor"] for g in rel.gacetas)
    equipo = _trabaja_el_ticket(usuario, ticket, rel) or (rel.atiende and not revisa_gaceta)
    interesado = equipo or rel.es_solicitante or bool(rel.gacetas)
    return [canal for canal, entra in ((EQUIPO, equipo), (INTERESADOS, interesado)) if entra]


def puede_leer(usuario, ticket, canal, rel=None):
    return canal in canales_de(usuario, ticket, rel)


def puede_escribir(usuario, ticket, canal, rel=None):
    return ticket.estado in ESTADOS_ABIERTOS and puede_leer(usuario, ticket, canal, rel)


def destinatarios(ticket, canal, actor_id=None):
    """Personas a quienes avisar de un mensaje del canal: las que intervienen directamente en el ticket (solicitante,
    responsable y su equipo, responsables de actividades abiertas, aprobadores con decisión pendiente, revisores y
    coordinadores de gacetas, participantes y revisores de colecciones) y pertenecen a ESE canal; nunca el autor. La
    supervisión solo por alcance lee el canal del equipo, pero no recibe un aviso por cada mensaje."""
    from apps.notificaciones import destinatarios as dest
    from apps.tickets import vinculos
    from apps.tickets.models import ParticipanteParrilla, RevisorEntregable

    candidatos = dest.unir(dest.solicitante(ticket), dest.responsable(ticket), dest.equipo_responsable(ticket))
    for tarea in vinculos.tareas_del_ticket(ticket).exclude(estado__in=("COMPLETADA", "CANCELADA")):
        candidatos = dest.unir(candidatos, dest.de_tarea(tarea))
    for aprobacion in vinculos.aprobaciones_del_ticket(ticket).filter(estado="PENDIENTE"):
        candidatos = dest.unir(candidatos, dest.de_aprobacion(aprobacion))
    for gaceta in ticket.gacetas.exclude(estado="CANCELADA"):
        gaceta.ticket = ticket
        candidatos = dest.unir(candidatos, dest.revisores_gaceta(gaceta), dest.coordinadores_gaceta(gaceta))
    for fila in ParticipanteParrilla.objects.filter(parrilla__ticket=ticket):
        candidatos = dest.unir(candidatos, dest.personas([fila.usuario_id]) if fila.usuario_id else dest.miembros_de([fila.equipo_id]))
    for fila in RevisorEntregable.objects.filter(entregable__ticket=ticket):
        candidatos = dest.unir(candidatos, dest.personas([fila.usuario_id]) if fila.usuario_id else dest.miembros_de([fila.equipo_id]))
    return [u for u in candidatos if u.pk != actor_id and canal in canales_de(u, ticket)]


# --- Mensajes ---------------------------------------------------------------------------------------------------

def _nombre(usuario):
    return usuario.nombre_visible


def _archivo(adjunto):
    from django.urls import reverse

    from apps.tickets.vista_previa import clase_de

    clase = clase_de(adjunto.nombre_original)
    url = reverse("tickets:descargar_adjunto", args=[adjunto.pk])
    return {
        "id": adjunto.pk,
        "nombre": adjunto.nombre_original,
        "tamano": adjunto.tamano_bytes,
        "url": url,
        # Imagen, video o PDF se abren en el navegador (`?ver=1`, misma autorización); lo demás se descarga.
        "ver": f"{url}?ver=1" if clase else url,
        "imagen": clase == "imagen",
    }


def serializar(comentario):
    """Representación del mensaje para el cliente: solo datos de lectura, el texto tal cual (el cliente lo pinta
    con `textContent`, así que no se interpreta HTML) y sus archivos (enlaces a la descarga autorizada)."""
    autor = comentario.autor
    return {
        "id": comentario.pk,
        "canal": comentario.canal,
        "autor_id": autor.pk,
        "autor": _nombre(autor),
        "inicial": (autor.nombre_visible[:1] or "?").upper(),
        "foto": getattr(autor, "google_foto_url", "") or "",
        "texto": comentario.contenido,
        "archivos": [_archivo(a) for a in comentario.adjuntos.all()],
        "creado_en": timezone.localtime(comentario.creado_en).isoformat(),
    }


def mensajes(ticket, canal, *, despues_de=None):
    """Mensajes del canal en orden cronológico. Sin `despues_de`, los últimos `LIMITE_CARGA`; con él, los
    posteriores a ese id (recuperación tras reconectar)."""
    consulta = ComentarioTicket.objects.filter(ticket=ticket, canal=canal).select_related("autor").prefetch_related("adjuntos")
    if despues_de:
        return [serializar(c) for c in consulta.filter(pk__gt=despues_de).order_by("pk")[:LIMITE_CARGA]]
    recientes = list(consulta.order_by("-pk")[:LIMITE_CARGA])
    return [serializar(c) for c in reversed(recientes)]


def validar_canal(canal):
    if canal not in CANALES:
        raise ValidationError("Conversación desconocida.")
    return canal


def validar_texto(texto, *, con_archivos=False):
    """El texto del mensaje; puede ir vacío solo si el mensaje lleva archivos."""
    texto = (texto or "").strip()
    if not texto and not con_archivos:
        raise ValidationError("El mensaje no puede estar vacío.")
    if len(texto) > LONGITUD_MAXIMA:
        raise ValidationError(f"El mensaje supera los {LONGITUD_MAXIMA} caracteres.")
    return texto


def validar_archivos(archivos):
    archivos = [a for a in archivos or [] if a]
    if len(archivos) > MAX_ARCHIVOS:
        raise ValidationError(f"Puedes enviar hasta {MAX_ARCHIVOS} archivos por mensaje.")
    for archivo in archivos:
        if archivo.size > TAMANO_MAXIMO_MB * 1024 * 1024:
            raise ValidationError(f"«{archivo.name}» supera los {TAMANO_MAXIMO_MB} MB.")
    return archivos


def puede_ver_archivo(usuario, adjunto):
    """Un archivo de la conversación se descarga solo si la persona puede leer SU canal (el del equipo nunca lo
    abre quien solo es interesado, aunque consulte el ticket)."""
    comentario = adjunto.comentario
    return comentario is not None and puede_leer(usuario, comentario.ticket, comentario.canal)


def difundir(ticket_id, mensaje):
    """Publica el mensaje al grupo de SU canal. Nunca rompe la operación: el mensaje ya quedó guardado."""
    from channels.layers import get_channel_layer

    capa = get_channel_layer()
    if capa is None:
        return
    try:
        async_to_sync(capa.group_send)(grupo_de(ticket_id, mensaje["canal"]), {"type": "mensaje.nuevo", "mensaje": mensaje})
    except Exception:  # noqa: BLE001 — la difusión es una mejora; la recuperación al reconectar cubre la falla
        logger.exception("No se pudo difundir el mensaje %s del ticket %s", mensaje.get("id"), ticket_id)


@transaction.atomic
def enviar(ticket, actor, texto, canal, archivos=None):
    """Guarda un mensaje (texto y, si los hay, archivos) en el canal (núcleo de los comentarios del ticket) y lo
    difunde al confirmar."""
    from apps.tickets.operaciones import registrar_comentario

    validar_canal(canal)
    archivos = validar_archivos(archivos)
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if not puede_escribir(actor, ticket, canal):
        raise PermissionDenied("No puedes escribir en esta conversación del ticket.")
    comentario = registrar_comentario(
        ticket, actor, validar_texto(texto, con_archivos=bool(archivos)), archivos, canal=canal
    )
    mensaje = serializar(comentario)
    transaction.on_commit(lambda: difundir(ticket.pk, mensaje))
    # Lo propio nunca cuenta como no leído, y quien RESPONDE en el canal ya vio lo anterior: sus avisos de ese canal
    # quedan leídos (si no, le llegaría por correo lo que ya contestó).
    _avanzar_marca(actor, ticket, canal, comentario.pk)
    _marcar_avisos(actor, ticket, canal)
    return mensaje


# --- No leídos ----------------------------------------------------------------------------------------------------

def _avanzar_marca(usuario, ticket, canal, hasta_id):
    lectura, _creada = LecturaConversacion.objects.get_or_create(usuario=usuario, ticket=ticket, canal=canal)
    if hasta_id > lectura.ultimo_leido_id:
        lectura.ultimo_leido_id = hasta_id
        lectura.save(update_fields=["ultimo_leido_id", "actualizado_en"])


def marcar_leido(usuario, ticket, canal, hasta_id=None):
    """Regla explícita: se llama cuando la persona VE ese canal abierto. Avanza la marca hasta `hasta_id` (o el
    último mensaje del canal) y marca como leídos los avisos COMENTARIO de ese canal y ticket."""
    validar_canal(canal)
    if not puede_leer(usuario, ticket, canal):
        raise PermissionDenied("No puedes consultar esta conversación del ticket.")
    ultimo = (
        ComentarioTicket.objects.filter(ticket=ticket, canal=canal).order_by("-pk").values_list("pk", flat=True).first()
        or 0
    )
    hasta = min(int(hasta_id), ultimo) if hasta_id else ultimo
    _avanzar_marca(usuario, ticket, canal, hasta)
    _marcar_avisos(usuario, ticket, canal)
    return no_leidos(usuario, ticket, canal)


def _marcar_avisos(usuario, ticket, canal):
    """Marca leídos los avisos COMENTARIO de `usuario` en ese canal y ticket."""
    from apps.notificaciones.models import Notificacion

    Notificacion.objects.filter(
        destinatario=usuario, ticket=ticket, evento="COMENTARIO", leida_en__isnull=True,
        hechos__contains=[[HECHO_CANAL, ETIQUETAS[canal]]],
    ).update(leida_en=timezone.now())


def no_leidos(usuario, ticket, canal):
    """Comentarios de OTRAS personas en el canal, posteriores a la marca de lectura de `usuario`."""
    marca = (
        LecturaConversacion.objects.filter(usuario=usuario, ticket=ticket, canal=canal)
        .values_list("ultimo_leido_id", flat=True).first()
        or 0
    )
    return ComentarioTicket.objects.filter(ticket=ticket, canal=canal, pk__gt=marca).exclude(autor=usuario).count()


# Colores del círculo con la inicial en el CORREO (los clientes de correo no leen tokens: literales, como el resto
# de `templates/correo/`). Se elige uno fijo por persona para que cada quien se reconozca en la lista.
COLORES_CORREO = ("#E8710A", "#1A73E8", "#188038", "#A142F4", "#D93025", "#12A4AF", "#5F6368")
MENSAJES_EN_CORREO = 15


def para_correo(usuario, ticket, canal):
    """Lo que `usuario` aún no ha leído del canal, para el correo «mensajes nuevos» (estilo chat): los últimos
    `MENSAJES_EN_CORREO` mensajes de OTRAS personas posteriores a su marca de lectura, en orden, y el total. Solo
    texto y nombres de archivo (el correo nunca adjunta ni enlaza archivos: se abren en el ticket)."""
    marca = (
        LecturaConversacion.objects.filter(usuario=usuario, ticket=ticket, canal=canal)
        .values_list("ultimo_leido_id", flat=True).first()
        or 0
    )
    consulta = (
        ComentarioTicket.objects.filter(ticket=ticket, canal=canal, pk__gt=marca)
        .exclude(autor=usuario).select_related("autor").prefetch_related("adjuntos")
    )
    total = consulta.count()
    recientes = list(reversed(consulta.order_by("-pk")[:MENSAJES_EN_CORREO]))
    lista = [
        {
            "autor": _nombre(c.autor),
            "autor_id": c.autor_id,
            "inicial": (_nombre(c.autor)[:1] or "?").upper(),
            "color": COLORES_CORREO[c.autor_id % len(COLORES_CORREO)],
            "foto": getattr(c.autor, "google_foto_url", "") or "",
            "texto": c.contenido,
            "archivos": [a.nombre_original for a in c.adjuntos.all()],
            "hora": timezone.localtime(c.creado_en),
        }
        for c in recientes
    ]
    return {"mensajes": lista, "total": total, "anteriores": total - len(lista),
            "autores": list(dict.fromkeys(m["autor"] for m in lista))}


# --- Presencia -------------------------------------------------------------------------------------------------

def participantes(usuario, ticket, canal):
    """Quiénes conversan en el canal y si están EN LÍNEA ahora (tienen Órbita abierta, `apps.core.presencia`): las
    personas que intervienen directamente en el ticket y pertenecen al canal (`destinatarios`), con la persona que
    mira primero. Solo lectura; exige poder leer el canal. En línea primero, luego por nombre."""
    from apps.core.presencia import esta_en_linea

    validar_canal(canal)
    if not puede_leer(usuario, ticket, canal):
        raise PermissionDenied("No puedes consultar esta conversación del ticket.")
    ahora = timezone.now()
    otros = destinatarios(ticket, canal, actor_id=usuario.pk)
    filas = [
        {
            "id": persona.pk,
            "nombre": _nombre(persona),
            "inicial": (persona.nombre_visible[:1] or "?").upper(),
            "foto": getattr(persona, "google_foto_url", "") or "",
            "en_linea": esta_en_linea(persona.visto_en, ahora),
            "visto_en": timezone.localtime(persona.visto_en).isoformat() if persona.visto_en else "",
            "yo": False,
        }
        for persona in otros
    ]
    filas.sort(key=lambda f: (not f["en_linea"], f["nombre"].lower()))
    return {"canal": canal, "personas": filas, "en_linea": sum(1 for f in filas if f["en_linea"])}


def contexto(usuario, ticket, rel=None):
    """Lo que necesita el panel al pintarse (sin mensajes: se cargan al abrir cada canal)."""
    from apps.tickets.espacios_chat import enlaces_para

    abiertos = ticket.estado in ESTADOS_ABIERTOS
    en_chat = enlaces_para(usuario, ticket)  # «Abrir en Google Chat» donde la persona está en el espacio
    canales = [
        {"clave": canal, "etiqueta": ETIQUETAS[canal], "puede_escribir": abiertos, "no_leidos": no_leidos(usuario, ticket, canal),
         "google_chat": en_chat.get(canal, "")}
        for canal in canales_de(usuario, ticket, rel)
    ]
    return {
        "canales": canales,
        "puede_leer": bool(canales),
        "no_leidos": sum(c["no_leidos"] for c in canales),
        "longitud_maxima": LONGITUD_MAXIMA,
        "max_archivos": MAX_ARCHIVOS,
        "tamano_maximo_mb": TAMANO_MAXIMO_MB,
    }
