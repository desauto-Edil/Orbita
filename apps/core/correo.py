"""Envío Gmail con OAuth del buzón o delegación. Lo usan la prueba de envío y el correo de las notificaciones
(`apps.notificaciones.correo`); este módulo no decide qué ni a quién se avisa."""

import base64
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
import requests


class ErrorCorreo(Exception):
    """Diagnóstico apto para consola: sin claves, tokens ni respuestas completas."""


def credenciales():
    if settings.GMAIL_AUTH_MODE == "oauth":
        requeridas = ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_SENDER_EMAIL")
        faltantes = [nombre for nombre in requeridas if not getattr(settings, nombre, "").strip()]
        if faltantes:
            raise ErrorCorreo("Faltan variables: " + ", ".join(faltantes))
        return Credentials(
            token=None, refresh_token=settings.GMAIL_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=settings.GMAIL_SCOPES,
        )
    if settings.GMAIL_AUTH_MODE != "service_account":
        raise ErrorCorreo("GMAIL_AUTH_MODE debe ser oauth o service_account.")
    requeridas = (
        "GMAIL_SERVICE_ACCOUNT_EMAIL", "GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY", "GMAIL_SENDER_EMAIL"
    )
    faltantes = [nombre for nombre in requeridas if not getattr(settings, nombre, "").strip()]
    if faltantes:
        raise ErrorCorreo("Faltan variables: " + ", ".join(faltantes))
    try:
        validate_email(settings.GMAIL_SENDER_EMAIL)
        info = {
            "type": "service_account",
            "client_email": settings.GMAIL_SERVICE_ACCOUNT_EMAIL,
            "private_key": settings.GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        if settings.GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID:
            info["private_key_id"] = settings.GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID
        return service_account.Credentials.from_service_account_info(
            info, scopes=settings.GMAIL_SCOPES, subject=settings.GMAIL_SENDER_EMAIL
        )
    except (ValueError, TypeError, ValidationError):
        raise ErrorCorreo("Revisa el remitente y la clave privada PEM de la cuenta de servicio en .env.") from None


def autorizar():
    credentials = credenciales()
    try:
        with requests.Session() as session:
            transport = Request(session=session)
            credentials.refresh(lambda *args, **kwargs: transport(*args, **{**kwargs, "timeout": 15}))
    except GoogleAuthError as exc:
        # Solo se expone un código conocido, nunca el texto íntegro del proveedor.
        texto = str(exc)
        if settings.GMAIL_AUTH_MODE == "oauth":
            mensaje = "Google rechazó la autorización del buzón. Vuelve a conectar Gmail desde /auth/gmail/."
        elif "unauthorized_client" in texto:
            mensaje = "Workspace no autorizó la delegación. Revisa el client_id de la cuenta de servicio y el scope gmail.send."
        elif "invalid_grant" in texto:
            mensaje = "Google rechazó la credencial o el buzón delegado. Revisa la clave, el remitente y la delegación Workspace."
        elif "invalid_client" in texto:
            mensaje = "Google no reconoce la cuenta de servicio o su clave."
        else:
            mensaje = "No se pudo autorizar Gmail; revisa conectividad y delegación Workspace."
        raise ErrorCorreo(mensaje) from None
    except requests.RequestException:
        raise ErrorCorreo("No se pudo conectar con Google para autorizar Gmail.") from None
    return credentials


class ErrorCorreoIncierto(ErrorCorreo):
    """Se pidió el envío a Google pero no se pudo confirmar el resultado: NO se debe reintentar (podría duplicarse)."""


def _validar_destinatario(destinatario):
    try:
        validate_email(destinatario)
    except ValidationError:
        raise ErrorCorreo("El destinatario no es una dirección de correo válida.") from None


def construir(destinatario, asunto, texto, *, html=None, imagenes=(), remitente_nombre=""):
    """Mensaje MIME: texto plano y, si hay `html`, su alternativa HTML con las `imagenes` incrustadas
    (`[(cid, bytes, maintype, subtype)]`, referenciadas en el HTML como `cid:<cid>`)."""
    _validar_destinatario(destinatario)
    mensaje = EmailMessage()
    remitente = settings.GMAIL_SENDER_EMAIL
    mensaje["From"] = formataddr((remitente_nombre, remitente)) if remitente_nombre else remitente
    mensaje["To"] = destinatario
    mensaje["Subject"] = asunto
    mensaje["Date"] = formatdate(localtime=False)
    mensaje["Message-ID"] = make_msgid()
    mensaje.set_content(texto)
    if html:
        mensaje.add_alternative(html, subtype="html")
        if imagenes:
            parte_html = mensaje.get_payload()[-1]
            for cid, datos, maintype, subtype in imagenes:
                parte_html.add_related(datos, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
    return mensaje


def enviar_mensaje(mensaje):
    """Envía un mensaje ya construido por Gmail API. Devuelve el id de Gmail. `ErrorCorreoIncierto` cuando la
    petición salió pero no se pudo confirmar (no reintentar); `ErrorCorreo` cuando falló antes o Google la rechazó."""
    credentials = autorizar()
    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode("ascii")
    # Una sola petición; no reintentar envíos cuyo resultado sea incierto.
    try:
        response = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {credentials.token}"},
            json={"raw": raw}, timeout=20, allow_redirects=False,
        )
    except requests.RequestException:
        raise ErrorCorreoIncierto(
            "No se pudo confirmar el resultado del envío. Revisa Enviados antes de repetir para evitar duplicados."
        ) from None
    if response.status_code != 200:
        if response.status_code == 403:
            detalle = "Revisa que Gmail API esté habilitada y que Workspace permita gmail.send."
        elif response.status_code == 401:
            detalle = "Google rechazó la autorización del buzón."
        elif response.status_code == 429:
            detalle = "Se alcanzó un límite de envío de Google."
        else:
            detalle = "Comprueba la configuración de Gmail y el buzón remitente."
        raise ErrorCorreo(f"Gmail respondió HTTP {response.status_code}. {detalle}")
    try:
        identificador = response.json()["id"]
    except (ValueError, KeyError):
        raise ErrorCorreoIncierto("Google respondió sin identificador; revisa Enviados antes de repetir.") from None
    return identificador


def enviar_prueba(destinatario):
    mensaje = construir(
        destinatario,
        "Órbita — prueba de envío Gmail",
        "Este es un correo de prueba de Órbita para verificar el envío mediante Gmail API.\n"
        "No corresponde a un ticket ni activa notificaciones automáticas.\n",
    )
    return enviar_mensaje(mensaje)
