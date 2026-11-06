"""Correo de los avisos (CU-036, RQF-106): resumen moderado de la acción + UN botón «Ver en Órbita», SIEMPRE con el
logo de la plataforma (el del modo claro; sin logo, la inicial del nombre). El logo viaja INCRUSTADO en el mensaje
(`cid:`), así se ve aunque el servidor de Órbita no sea accesible desde fuera.

Seguridad del entorno:
- `NOTIFICACIONES_CORREO_ACTIVO` (por entorno, apagado por defecto): sin él no se envía nada (queda OMITIDO).
- `CORREO_REDIRIGIR_A`: en desarrollo/staging todo correo va a ese buzón (el destinatario real queda registrado).

Un envío INCIERTO (salió hacia Google pero no se confirmó) nunca se reintenta: evitaría duplicados."""

import logging
import mimetypes

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.notificaciones.models import EnvioCorreo

logger = logging.getLogger(__name__)

CID_LOGO = "logo-orbita"
MAX_INTENTOS = 3


def activo():
    return bool(getattr(settings, "NOTIFICACIONES_CORREO_ACTIVO", False))


def encolar(notificaciones):
    """Encola el envío de cada notificación con correo. Si Celery no responde, el envío queda PENDIENTE y el
    reconciliador (`notificaciones.reintentar_correos`) lo retoma."""
    from apps.notificaciones.tasks import enviar_correo

    if not activo():
        # Con el correo apagado no hay nada que encolar: queda registrado como omitido, sin pasar por Celery.
        EnvioCorreo.objects.filter(notificacion__in=notificaciones, estado=EnvioCorreo.Estado.PENDIENTE).update(
            estado=EnvioCorreo.Estado.OMITIDO, error="El envío de correo está desactivado en este entorno.")
        return
    for notificacion in notificaciones:
        envio_id = EnvioCorreo.objects.filter(notificacion=notificacion).values_list("pk", flat=True).first()
        if envio_id is None:
            continue
        try:
            enviar_correo.apply_async((envio_id,), retry=False)
        except Exception:  # noqa: BLE001 — sin broker: lo retoma el reconciliador.
            logger.warning("No se pudo encolar el correo %s; se reintentará.", envio_id)


def _logo():
    """`(bytes, maintype, subtype)` del logo del modo claro, o `None`."""
    from apps.core.models import ConfiguracionSistema

    sistema = ConfiguracionSistema.actual()
    if not sistema.logo:
        return None
    try:
        with sistema.logo.open("rb") as archivo:
            datos = archivo.read()
    except (FileNotFoundError, OSError, ValueError):
        return None
    tipo = mimetypes.guess_type(sistema.logo.name)[0] or "image/png"
    maintype, _, subtype = tipo.partition("/")
    return datos, maintype, subtype or "png"


def contenido(notificacion):
    """`(asunto, texto, html, imagenes)` del correo de una notificación."""
    from apps.core.models import ConfiguracionSistema

    sistema = ConfiguracionSistema.actual()
    ticket = notificacion.ticket
    codigo = ticket.codigo if ticket is not None and ticket.consecutivo else ""
    asunto = f"{codigo} · {notificacion.titulo}" if codigo else notificacion.titulo
    # El botón pasa por «abrir» (marca la notificación como leída y vuelve a autorizar al llegar al objeto).
    enlace = f"{settings.APP_BASE_URL}{reverse('notificaciones:abrir', args=[notificacion.pk])}" if notificacion.enlace else ""
    logo = _logo()
    destinatario = notificacion.destinatario
    contexto = {
        "sistema": sistema,
        "notificacion": notificacion,
        "nombre": destinatario.first_name or destinatario.get_full_name() or destinatario.get_username(),
        "enlace": enlace,
        "logo_cid": CID_LOGO if logo else "",
        "motivo": _motivo(notificacion),
        "preferencias": f"{settings.APP_BASE_URL}/perfil/#notificaciones",
    }
    texto = render_to_string("correo/notificacion.txt", contexto)
    html = render_to_string("correo/notificacion.html", contexto)
    imagenes = [(CID_LOGO, *logo)] if logo else []
    return asunto, texto, html, imagenes


def _motivo(notificacion):
    """«Recibes este correo porque…» según el grupo del aviso."""
    from apps.notificaciones.models import Grupo

    return {
        Grupo.TICKETS: "participas en este ticket",
        Grupo.ATENCION: "atiendes este ticket o perteneces al equipo que lo atiende",
        Grupo.TRABAJO: "tienes una actividad o una aprobación en este proceso",
        Grupo.GACETAS: "participas en la revisión de esta gaceta",
        Grupo.ALERTAS: "eres responsable de algo que requiere atención",
    }.get(notificacion.grupo, "participas en este ticket")


def enviar(envio_id):
    """Envía UN correo pendiente. Idempotente: un envío ya resuelto (enviado, incierto u omitido) no se repite."""
    from apps.core import correo as gmail
    from apps.core.models import ConfiguracionSistema

    with transaction.atomic():
        envio = EnvioCorreo.objects.select_for_update(of=("self",)).select_related(
            "notificacion__destinatario", "notificacion__ticket__detalle_servicio__servicio").filter(pk=envio_id).first()
        if envio is None or envio.estado not in (EnvioCorreo.Estado.PENDIENTE, EnvioCorreo.Estado.FALLIDO):
            return envio
        if envio.intentos >= MAX_INTENTOS:
            return envio
        if not activo():
            envio.estado, envio.error = EnvioCorreo.Estado.OMITIDO, "El envío de correo está desactivado en este entorno."
            envio.save(update_fields=["estado", "error"])
            return envio
        envio.intentos += 1
        envio.save(update_fields=["intentos"])
    destino = (getattr(settings, "CORREO_REDIRIGIR_A", "") or "").strip() or envio.correo
    try:
        asunto, texto, html, imagenes = contenido(envio.notificacion)
        if destino != envio.correo:
            asunto = f"[Para {envio.correo}] {asunto}"
        mensaje = gmail.construir(destino, asunto, texto, html=html, imagenes=imagenes,
                                  remitente_nombre=ConfiguracionSistema.actual().nombre)
        identificador = gmail.enviar_mensaje(mensaje)
    except gmail.ErrorCorreoIncierto as error:
        envio.estado, envio.error = EnvioCorreo.Estado.INCIERTO, str(error)[:300]
    except gmail.ErrorCorreo as error:
        envio.estado, envio.error = EnvioCorreo.Estado.FALLIDO, str(error)[:300]
    except Exception as error:  # noqa: BLE001 — antes de llegar a Google: se puede reintentar.
        logger.exception("Error preparando el correo %s", envio_id)
        envio.estado, envio.error = EnvioCorreo.Estado.FALLIDO, (type(error).__name__)[:300]
    else:
        envio.estado, envio.mensaje_id, envio.error = EnvioCorreo.Estado.ENVIADO, identificador[:120], ""
        envio.enviado_en = timezone.now()
    envio.save(update_fields=["estado", "mensaje_id", "error", "enviado_en"])
    return envio
