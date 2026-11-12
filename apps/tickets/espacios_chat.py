"""Conversación del ticket en Google Chat — requerimiento nuevo (CU por asignar; extiende CU-018/RQF-057). Plan y
decisiones en `.claude/plans/conversacion_google_chat.md`.

Cada canal de la Conversación (EQUIPO, INTERESADOS) puede tener SU espacio de Google Chat (`EspacioChatTicket`). Es
un ESPEJO (D1): `ComentarioTicket` sigue siendo la fuente de verdad, la autorización sigue siendo la de
`apps.tickets.conversacion` y Chat es otra superficie del mismo canal.

- D3. El espacio de un canal nace con su PRIMER mensaje (lo crea la tarea que publica ese mensaje).
- R1. Miembros = `conversacion.destinatarios(ticket, canal)` (quienes intervienen directamente y pertenecen a ese
  canal). La supervisión solo por alcance sigue leyendo en Órbita. Solo la cuenta de Órbita agrega o quita gente.
- R2. Aislamiento: en cada sincronización se retira del espacio a cualquier persona que Órbita no haya puesto.
- R3. Quien pierde la relación con el ticket sale en la siguiente sincronización (tras cada evento del ticket y en
  el barrido periódico). Con el ticket cerrado, los miembros se congelan (el espacio queda de consulta).
- D2. Lo escrito en Órbita se publica COMO SU AUTOR si es miembro; si no (supervisión, fuera de la lista
  permitida), lo publica la cuenta de Órbita con el nombre delante.
- R4. Lo escrito en Chat entra solo si su autor es un miembro que Órbita reconoce y PUEDE ESCRIBIR en ese canal en
  ese momento (`conversacion.puede_escribir`); si no, se registra como RECHAZADO y no se importa.
- R5. Append-only: ediciones y borrados en Chat no cambian Órbita; un adjunto se registra con su marca; el texto se
  recorta a `conversacion.LONGITUD_MAXIMA`.
- R6. Sin duplicados ni ecos: `messageId` propio por comentario (`client-orbita-c<id>`) y `MensajeChat.nombre` único.
- R7. Nunca altera la operación: todo lo que habla con Google corre en Celery tras el commit; si Google falla, la
  conversación en Órbita sigue igual y la bandeja de salida reintenta.
- D4. Ticket cerrado, resuelto o cancelado (no se puede escribir en Órbita) → el espacio queda en SOLO LECTURA con un
  mensaje final; al reabrirse, se vuelve a permitir escribir.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core import google_chat as gc
from apps.tickets import conversacion
from apps.tickets.models import ComentarioTicket, EspacioChatTicket, MensajeChat, MiembroEspacioChat, Ticket

logger = logging.getLogger(__name__)

MAX_INTENTOS = 5
MARCA_ADJUNTO = "[adjunto en Google Chat]"
MARCA_RECORTE = "… [recortado]"
RENOVAR_ANTES = timedelta(hours=24)


def activo():
    return bool(getattr(settings, "GOOGLE_CHAT_ACTIVO", False)) and gc.configurado()


def _permitido(email):
    permitidos = getattr(settings, "GOOGLE_CHAT_PERMITIDOS", []) or []
    return bool(email) and (not permitidos or email.lower() in permitidos)


def _abierto(ticket):
    return ticket.estado in conversacion.ESTADOS_ABIERTOS


def _enlace(ticket, canal):
    ruta = reverse("tickets:espacio", args=[ticket.pk])
    return f"{settings.APP_BASE_URL}{ruta}?panel=conversacion&canal={canal}"


def _nombre_espacio(ticket, canal):
    detalle = getattr(ticket, "detalle_servicio", None)
    servicio = detalle.servicio.nombre if detalle is not None else ""
    partes = [ticket.codigo or f"Ticket {ticket.pk}", servicio, conversacion.ETIQUETAS.get(canal, canal)]
    return " · ".join(p for p in partes if p)[:128]


def _encolar(tarea, *args):
    try:
        tarea.apply_async(args, retry=False)
    except Exception:  # noqa: BLE001 — sin broker: lo retoma el reconciliador.
        logger.warning("No se pudo encolar %s%s; lo retomará el reconciliador.", tarea.name, args)


# --- Puntos de entrada desde el dominio --------------------------------------------------------------------------

def al_registrar(comentario):
    """Lo llama `operaciones.registrar_comentario` (dentro de su transacción) por cada mensaje escrito en Órbita:
    deja el mensaje en la bandeja de salida y lo encola al confirmar."""
    if comentario.origen != ComentarioTicket.Origen.ORBITA or not activo():
        return None
    mensaje = MensajeChat.objects.create(comentario=comentario, direccion=MensajeChat.Direccion.SALIDA)

    def encolar():
        from apps.tickets.tasks import publicar_en_google_chat

        _encolar(publicar_en_google_chat, mensaje.pk)

    transaction.on_commit(encolar)
    return mensaje


def al_ocurrir(eventos):
    """Consumidor de los Domain Events (`notificaciones.eventos`, tras el commit): un ticket con espacios en Chat
    al que le pasó algo (tomar, asignar, actividades, aprobaciones, gacetas, cerrar, reabrir…) vuelve a calcular sus
    miembros y su estado (R3, D4)."""
    if not activo():
        return
    ids = {e.ticket_id for e in eventos if e.ticket_id}
    if not ids:
        return
    from apps.tickets.tasks import sincronizar_ticket_google_chat

    con_espacio = EspacioChatTicket.objects.filter(ticket_id__in=ids).exclude(nombre="").values_list("ticket_id", flat=True)
    for ticket_id in set(con_espacio):
        _encolar(sincronizar_ticket_google_chat, ticket_id)


# --- Espacio y miembros ----------------------------------------------------------------------------------------

def _miembros_deseados(ticket, canal):
    """R1: las personas del canal que intervienen directamente, con correo, activas y permitidas en el entorno."""
    propia = gc.cuenta()
    return {
        u.pk: u for u in conversacion.destinatarios(ticket, canal)
        if u.is_active and u.email and _permitido(u.email) and u.email.lower() != propia
    }


def asegurar_espacio(ticket, canal):
    """El espacio del canal, creándolo si aún no existe (D3). Idempotente: el `requestId` de Google y el lock de
    la fila impiden un segundo espacio para el mismo canal."""
    espacio, _creado = EspacioChatTicket.objects.get_or_create(ticket=ticket, canal=canal)
    if espacio.nombre:
        return espacio
    with transaction.atomic():
        espacio = EspacioChatTicket.objects.select_for_update().get(pk=espacio.pk)
        if espacio.nombre:
            return espacio
        datos = gc.crear_espacio(_nombre_espacio(ticket, canal), f"orbita-t{ticket.pk}-{canal.lower()}")
        espacio.nombre = datos["name"][:120]
        espacio.uri = (datos.get("spaceUri") or "")[:300]
        espacio.ultimo_importado_en = timezone.now()
        espacio.save(update_fields=["nombre", "uri", "ultimo_importado_en", "actualizado_en"])
    _auditar(espacio, {"accion": "espacio creado", "canal": canal})
    _preparar(espacio)
    return espacio


def _preparar(espacio):
    """Lo que sigue a crear el espacio; cada paso que falle queda en `error` y lo retoma el reconciliador."""
    ticket = espacio.ticket
    errores = []
    try:
        gc.restringir_miembros(espacio.nombre)
    except gc.ErrorChat as error:
        errores.append(f"permisos: {error}")
    try:
        gc.publicar(
            espacio.nombre,
            f"Conversación «{conversacion.ETIQUETAS.get(espacio.canal)}» del ticket {ticket.codigo}. Lo que se "
            f"escriba aquí también queda en Órbita, y lo que se escriba en Órbita llega aquí. Solo entra lo que "
            f"escriba quien participa en el ticket.\n{_enlace(ticket, espacio.canal)}",
            gc.id_cliente(f"orbita-inicio-{espacio.pk}"),
        )
    except gc.ErrorChat as error:
        errores.append(f"bienvenida: {error}")
    espacio.estado = EspacioChatTicket.Estado.ACTIVO
    espacio.error = "; ".join(errores)[:300]
    espacio.save(update_fields=["estado", "error", "actualizado_en"])
    sincronizar_miembros(espacio)
    suscribir(espacio)
    ajustar_estado(espacio)


def sincronizar_miembros(espacio):
    """R1–R3: deja en el espacio exactamente a las personas del canal. Con el ticket cerrado no cambia nada."""
    ticket = Ticket.objects.get(pk=espacio.ticket_id)
    if not espacio.nombre or not _abierto(ticket):
        return
    deseados = _miembros_deseados(ticket, espacio.canal)
    actuales = {m.usuario_id: m for m in MiembroEspacioChat.objects.filter(espacio=espacio)}
    for usuario_id, usuario in deseados.items():
        fila = actuales.get(usuario_id)
        if fila is not None and fila.activo:
            continue
        fila = fila or MiembroEspacioChat(espacio=espacio, usuario=usuario)
        try:
            datos = gc.agregar_miembro(espacio.nombre, usuario.email)
        except gc.ErrorChat as error:
            fila.activo, fila.error = False, str(error)[:300]
        else:
            fila.membresia, fila.usuario_chat = datos["membresia"][:200], datos["usuario"][:120]
            fila.activo, fila.error = True, ""
        fila.save()
    for usuario_id, fila in actuales.items():
        if fila.activo and usuario_id not in deseados:
            try:
                gc.quitar_miembro(fila.membresia)
            except gc.ErrorChat as error:
                fila.error = str(error)[:300]
                fila.save(update_fields=["error", "actualizado_en"])
                continue
            fila.activo, fila.error = False, ""
            fila.save(update_fields=["activo", "error", "actualizado_en"])
    _retirar_ajenos(espacio)
    espacio.sincronizado_en = timezone.now()
    espacio.save(update_fields=["sincronizado_en", "actualizado_en"])


def _retirar_ajenos(espacio):
    """R2: nadie que Órbita no haya puesto queda en el espacio (salvo administradores: la cuenta de Órbita)."""
    try:
        remotos = gc.listar_miembros(espacio.nombre)
    except gc.ErrorChat as error:
        logger.warning("No se pudieron listar los miembros de %s: %s", espacio.nombre, error)
        return
    conocidos = set(
        MiembroEspacioChat.objects.filter(espacio=espacio, activo=True).values_list("usuario_chat", flat=True)
    )
    for membresia in remotos:
        if membresia.get("role") == "ROLE_MANAGER":
            continue
        if (membresia.get("member") or {}).get("name") not in conocidos:
            try:
                gc.quitar_miembro(membresia["name"])
            except gc.ErrorChat as error:
                logger.warning("No se pudo retirar a un ajeno de %s: %s", espacio.nombre, error)


def ajustar_estado(espacio):
    """D4: solo lectura mientras no se pueda escribir en Órbita; vuelve a abrirse al reabrir el ticket."""
    ticket = Ticket.objects.get(pk=espacio.ticket_id)
    abierto = _abierto(ticket)
    if not espacio.nombre:
        return
    if not abierto and espacio.estado != EspacioChatTicket.Estado.SOLO_LECTURA:
        texto = (
            f"El ticket {ticket.codigo} quedó {ticket.get_estado_display().lower()}. Esta conversación queda de "
            f"consulta: lo que se escriba aquí ya no llega a Órbita."
        )
        nuevo = EspacioChatTicket.Estado.SOLO_LECTURA
    elif abierto and espacio.estado == EspacioChatTicket.Estado.SOLO_LECTURA:
        texto = f"El ticket {ticket.codigo} se reabrió: pueden volver a escribir aquí."
        nuevo = EspacioChatTicket.Estado.ACTIVO
    else:
        return
    marca = timezone.now().strftime("%Y%m%d%H%M%S")
    try:
        gc.publicar(espacio.nombre, texto, gc.id_cliente(f"orbita-estado-{espacio.pk}-{marca}"))
    except gc.ErrorChat as error:
        logger.warning("No se pudo avisar el cambio de estado en %s: %s", espacio.nombre, error)
    try:
        gc.solo_lectura(espacio.nombre, nuevo == EspacioChatTicket.Estado.SOLO_LECTURA)
        espacio.error = ""
    except gc.ErrorChat as error:
        # Sin el permiso del espacio, R4 sigue rechazando lo que se escriba con el ticket cerrado.
        espacio.error = f"solo lectura: {error}"[:300]
    espacio.estado = nuevo
    espacio.save(update_fields=["estado", "error", "actualizado_en"])
    _auditar(espacio, {"accion": "estado del espacio", "estado": nuevo})


def sincronizar_ticket(ticket_id):
    for espacio in EspacioChatTicket.objects.filter(ticket_id=ticket_id).exclude(nombre=""):
        ajustar_estado(espacio)
        sincronizar_miembros(espacio)


def _auditar(espacio, datos):
    from apps.core.auditoria import registrar_evento
    from apps.core.models import RegistroAuditoria

    try:
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=espacio, origen=RegistroAuditoria.Origen.SISTEMA,
            datos_nuevos={"google_chat": espacio.nombre, **datos},
        )
    except Exception:  # noqa: BLE001 — la auditoría de un efecto externo no tumba el efecto.
        logger.exception("No se pudo auditar el espacio de Chat %s", espacio.pk)


# --- Salida: Órbita → Chat -------------------------------------------------------------------------------------

def publicar_pendiente(mensaje_id):
    """Publica UN mensaje de la bandeja de salida (D2). Idempotente: un envío resuelto no se repite y reintentar
    no duplica en Chat."""
    with transaction.atomic():
        mensaje = MensajeChat.objects.select_for_update(of=("self",)).select_related(
            "comentario__autor", "comentario__ticket").filter(pk=mensaje_id).first()
        if (
            mensaje is None or mensaje.direccion != MensajeChat.Direccion.SALIDA
            or mensaje.estado not in (MensajeChat.Estado.PENDIENTE, MensajeChat.Estado.FALLIDO)
            or mensaje.intentos >= MAX_INTENTOS
        ):
            return mensaje
        mensaje.intentos += 1
        mensaje.save(update_fields=["intentos"])
    if not activo():
        return mensaje
    comentario = mensaje.comentario
    autor = comentario.autor
    if not _abierto(comentario.ticket) and not EspacioChatTicket.objects.filter(
        ticket=comentario.ticket, canal=comentario.canal).exclude(nombre="").exists():
        # D3 + D4: un ticket que ya no admite mensajes no estrena espacio.
        mensaje.estado, mensaje.error, mensaje.intentos = MensajeChat.Estado.FALLIDO, "El ticket ya no está abierto.", MAX_INTENTOS
        mensaje.save(update_fields=["estado", "error", "intentos"])
        return mensaje
    try:
        espacio = asegurar_espacio(comentario.ticket, comentario.canal)
        miembro = MiembroEspacioChat.objects.filter(espacio=espacio, usuario=autor, activo=True).first()
        if miembro is None and _abierto(comentario.ticket):
            sincronizar_miembros(espacio)  # quizá acaba de ganar la relación con el ticket
            miembro = MiembroEspacioChat.objects.filter(espacio=espacio, usuario=autor, activo=True).first()
        id_mensaje = gc.id_cliente(f"orbita-c{comentario.pk}")
        if miembro is not None:
            nombre = gc.publicar(espacio.nombre, comentario.contenido, id_mensaje, como=autor.email)
        else:
            quien = autor.nombre_visible
            nombre = gc.publicar(espacio.nombre, f"{quien} (desde Órbita): {comentario.contenido}", id_mensaje)
    except gc.ErrorChat as error:
        mensaje.estado, mensaje.error = MensajeChat.Estado.FALLIDO, str(error)[:300]
        mensaje.save(update_fields=["estado", "error"])
        return mensaje
    except Exception as error:  # noqa: BLE001
        logger.exception("Error publicando en Google Chat el mensaje %s", mensaje_id)
        mensaje.estado, mensaje.error = MensajeChat.Estado.FALLIDO, type(error).__name__[:300]
        mensaje.save(update_fields=["estado", "error"])
        return mensaje
    mensaje.espacio, mensaje.nombre = espacio, nombre[:200]
    mensaje.estado, mensaje.error, mensaje.enviado_en = MensajeChat.Estado.ENVIADO, "", timezone.now()
    try:
        mensaje.save(update_fields=["espacio", "nombre", "estado", "error", "enviado_en"])
    except IntegrityError:  # el barrido lo vio primero como entrada: se conserva como enviado sin nombre
        mensaje.nombre = ""
        mensaje.save(update_fields=["espacio", "nombre", "estado", "error", "enviado_en"])
    return mensaje


# --- Entrada: Chat → Órbita ------------------------------------------------------------------------------------

def _texto_de(datos):
    texto = (datos.get("text") or datos.get("argumentText") or "").strip()
    if datos.get("attachment") or datos.get("attachments"):
        texto = f"{texto}\n{MARCA_ADJUNTO}".strip()
    if len(texto) > conversacion.LONGITUD_MAXIMA:
        texto = texto[: conversacion.LONGITUD_MAXIMA - len(MARCA_RECORTE)] + MARCA_RECORTE
    return texto


def _avanzar(espacio, datos):
    creado = parse_datetime(datos.get("createTime") or "")
    if creado is not None and (espacio.ultimo_importado_en is None or creado > espacio.ultimo_importado_en):
        espacio.ultimo_importado_en = creado
        espacio.save(update_fields=["ultimo_importado_en", "actualizado_en"])


def importar_mensaje(espacio, datos):
    """R4–R6: un mensaje escrito en Chat entra a la Conversación como si se hubiera escrito en Órbita (mismo
    aviso, misma difusión por WebSocket), o queda RECHAZADO. Devuelve el `MensajeChat` (o `None` si se ignora)."""
    from apps.tickets.operaciones import registrar_comentario

    nombre = str(datos.get("name") or "")[:200]
    remitente = datos.get("sender") or {}
    _avanzar(espacio, datos)
    if not nombre or (datos.get("clientAssignedMessageId") or "").startswith("client-orbita"):
        return None  # lo publicó Órbita (eco)
    if remitente.get("type") != "HUMAN" or MensajeChat.objects.filter(nombre=nombre).exists():
        return None
    miembro = MiembroEspacioChat.objects.select_related("usuario").filter(
        espacio=espacio, usuario_chat=remitente.get("name", "")).first()
    texto = _texto_de(datos)
    if not texto:
        return None
    try:
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=espacio.ticket_id)
            usuario = miembro.usuario if miembro is not None else None
            if usuario is None or not usuario.is_active or not conversacion.puede_escribir(usuario, ticket, espacio.canal):
                motivo = "Quien escribió no participa en este canal del ticket." if usuario is None or not usuario.is_active else (
                    "El ticket no admite mensajes o la persona ya no pertenece a este canal.")
                return MensajeChat.objects.create(
                    espacio=espacio, nombre=nombre, direccion=MensajeChat.Direccion.ENTRADA,
                    estado=MensajeChat.Estado.RECHAZADO, error=motivo,
                )
            comentario = registrar_comentario(
                ticket, usuario, texto, canal=espacio.canal, origen=ComentarioTicket.Origen.GOOGLE_CHAT
            )
            mensaje = MensajeChat.objects.create(
                comentario=comentario, espacio=espacio, nombre=nombre, direccion=MensajeChat.Direccion.ENTRADA,
                estado=MensajeChat.Estado.IMPORTADO, enviado_en=timezone.now(),
            )
            serializado = conversacion.serializar(comentario)
            transaction.on_commit(lambda: conversacion.difundir(ticket.pk, serializado))
            return mensaje
    except IntegrityError:
        return None  # otro proceso lo importó a la vez


def barrer(espacio):
    """Respaldo (y modo sin Pub/Sub): lee los mensajes del espacio posteriores al último importado."""
    if not espacio.nombre:
        return 0
    desde = espacio.ultimo_importado_en or espacio.creado_en
    importados = 0
    for datos in gc.listar_mensajes(espacio.nombre, desde):
        if importar_mensaje(espacio, datos) is not None:
            importados += 1
    return importados


def suscribir(espacio):
    """Tiempo real (opcional): suscribe la cuenta de Órbita a los mensajes nuevos del espacio."""
    tema = (getattr(settings, "GOOGLE_CHAT_PUBSUB_TEMA", "") or "").strip()
    if not tema or not espacio.nombre:
        return
    try:
        datos = gc.suscribir(espacio.nombre, tema)
    except gc.ErrorChat as error:
        espacio.error = f"suscripción: {error}"[:300]
        espacio.save(update_fields=["error", "actualizado_en"])
        return
    if datos:
        espacio.suscripcion = datos.get("name", "")[:200]
        espacio.suscripcion_vence = parse_datetime(datos.get("expireTime") or "")
        espacio.save(update_fields=["suscripcion", "suscripcion_vence", "actualizado_en"])


def renovar(espacio):
    if not (getattr(settings, "GOOGLE_CHAT_PUBSUB_TEMA", "") or "").strip():
        return
    if not espacio.suscripcion:
        suscribir(espacio)
        return
    if espacio.suscripcion_vence and espacio.suscripcion_vence > timezone.now() + RENOVAR_ANTES:
        return
    try:
        datos = gc.renovar_suscripcion(espacio.suscripcion)
    except gc.ErrorChat:
        espacio.suscripcion = ""
        espacio.save(update_fields=["suscripcion", "actualizado_en"])
        suscribir(espacio)
        return
    espacio.suscripcion_vence = parse_datetime(datos.get("expireTime") or "")
    espacio.save(update_fields=["suscripcion_vence", "actualizado_en"])


def recibir_eventos():
    """Consume la suscripción PULL de Pub/Sub: por cada mensaje nuevo en un espacio de Órbita, lo lee y lo
    importa. Confirma (ack) solo lo procesado; lo demás vuelve a entregarse."""
    suscripcion = (getattr(settings, "GOOGLE_CHAT_PUBSUB_SUSCRIPCION", "") or "").strip()
    if not suscripcion:
        return 0
    procesados, confirmar = 0, []
    for ack_id, tipo, contenido in gc.recibir(suscripcion):
        if tipo != gc.EVENTO_CREADO:
            confirmar.append(ack_id)  # recordatorios de vencimiento y otros: los cubre el reconciliador
            continue
        nombre = ((contenido.get("message") or {}).get("name") or "")
        espacio = EspacioChatTicket.objects.filter(nombre="/".join(nombre.split("/")[:2])).first() if nombre else None
        if espacio is None:
            confirmar.append(ack_id)
            continue
        try:
            importar_mensaje(espacio, gc.obtener_mensaje(nombre))
        except gc.ErrorChat as error:
            logger.warning("No se pudo leer %s: %s", nombre, error)
            continue
        confirmar.append(ack_id)
        procesados += 1
    gc.confirmar(suscripcion, confirmar)
    return procesados


# --- Tareas periódicas -----------------------------------------------------------------------------------------

def importar_recientes():
    """Cada minuto: eventos de Pub/Sub si está configurado; si no, barrido de los espacios de tickets abiertos."""
    if not activo():
        return 0
    if (getattr(settings, "GOOGLE_CHAT_PUBSUB_SUSCRIPCION", "") or "").strip():
        return recibir_eventos()
    total = 0
    for espacio in EspacioChatTicket.objects.filter(estado=EspacioChatTicket.Estado.ACTIVO).exclude(nombre="").order_by("-actualizado_en")[:200]:
        try:
            total += barrer(espacio)
        except gc.ErrorChat as error:
            logger.warning("No se pudo barrer %s: %s", espacio.nombre, error)
    return total


def reconciliar():
    """Cada 15 minutos: bandeja de salida, estado y miembros de cada espacio, suscripciones y barrido de respaldo."""
    if not activo():
        return {}
    limite = timezone.now() - timedelta(minutes=2)
    reenviados = 0
    for mensaje_id in MensajeChat.objects.filter(
        direccion=MensajeChat.Direccion.SALIDA, estado__in=(MensajeChat.Estado.PENDIENTE, MensajeChat.Estado.FALLIDO),
        intentos__lt=MAX_INTENTOS, creado_en__lte=limite,
    ).values_list("pk", flat=True)[:200]:
        publicar_pendiente(mensaje_id)
        reenviados += 1
    revisados = 0
    for espacio in EspacioChatTicket.objects.exclude(nombre="").select_related("ticket")[:500]:
        try:
            if espacio.estado == EspacioChatTicket.Estado.PENDIENTE:
                _preparar(espacio)
            else:
                ajustar_estado(espacio)
                sincronizar_miembros(espacio)
                if espacio.estado == EspacioChatTicket.Estado.ACTIVO:
                    renovar(espacio)
                    barrer(espacio)
            revisados += 1
        except gc.ErrorChat as error:
            espacio.error = str(error)[:300]
            espacio.save(update_fields=["error", "actualizado_en"])
        except Exception:  # noqa: BLE001 — un espacio no detiene el resto
            logger.exception("Error reconciliando el espacio de Chat %s", espacio.pk)
    return {"reenviados": reenviados, "espacios": revisados}


# --- Panel ------------------------------------------------------------------------------------------------------

def enlaces_para(usuario, ticket):
    """`{canal: uri}` de los espacios de Chat del ticket en los que la persona está (para «Abrir en Google Chat»)."""
    if not activo():
        return {}
    return dict(
        MiembroEspacioChat.objects.filter(espacio__ticket=ticket, usuario=usuario, activo=True)
        .exclude(espacio__uri="").values_list("espacio__canal", "espacio__uri")
    )
