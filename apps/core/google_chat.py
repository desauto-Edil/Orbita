"""Cliente de Google Chat con DELEGACIÓN DE DOMINIO (plan `.claude/plans/conversacion_google_chat.md`).

Órbita no es una «app» que cada persona instale: la cuenta de servicio con delegación
(`GOOGLE_CHAT_SERVICE_ACCOUNT_*`; si no se define, la de Gmail) actúa COMO una persona del dominio:

- como la CUENTA de Órbita (`GOOGLE_CHAT_CUENTA`) crea los espacios, administra sus miembros y sus permisos, lee
  los mensajes y se suscribe a sus eventos (es la administradora de cada espacio);
- como el AUTOR de un mensaje escrito en Órbita, lo publica en el espacio (decisión D2).

Scopes que el administrador de Workspace autoriza en la delegación (`GOOGLE_CHAT_SCOPES`): `chat.spaces`,
`chat.memberships` y `chat.messages`. Para recibir eventos en tiempo real, la cuenta de servicio (SIN suplantar a
nadie) consume la suscripción de Pub/Sub con el scope `pubsub`. Solo texto: con credenciales de usuario Chat no
admite tarjetas. Todo por REST con `google-auth[requests]` (sin dependencia nueva).

Ningún error de este módulo debe tumbar una operación de Órbita: quien lo llama corre en Celery y registra el
error en su bandeja de salida."""

import base64
import json
import re

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

API = "https://chat.googleapis.com/v1"
EVENTOS = "https://workspaceevents.googleapis.com/v1"
PUBSUB = "https://pubsub.googleapis.com/v1"
TIEMPO_ESPERA = 15  # segundos
EVENTO_CREADO = "google.workspace.chat.message.v1.created"
SCOPE_PUBSUB = ("https://www.googleapis.com/auth/pubsub",)

_sesiones = {}


class ErrorChat(Exception):
    def __init__(self, mensaje, codigo=None):
        super().__init__(mensaje)
        self.codigo = codigo


def cuenta():
    return (getattr(settings, "GOOGLE_CHAT_CUENTA", "") or "").strip().lower()


def configurado():
    return bool(
        cuenta()
        and (getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL", "") or "").strip()
        and (getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY", "") or "").strip()
    )


def faltantes():
    """Qué variables faltan para usar Google Chat (para mensajes de error claros)."""
    faltan = []
    if not cuenta():
        faltan.append("GOOGLE_CHAT_CUENTA (la cuenta de Workspace que crea y administra los espacios)")
    if not (getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL", "") or "").strip():
        faltan.append("GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL")
    if not (getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY", "") or "").strip():
        faltan.append("GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY")
    return faltan


@sensitive_variables()
def _credenciales(subject, scopes):
    from google.oauth2 import service_account

    if not configurado():
        raise ErrorChat("Falta configurar: " + "; ".join(faltantes()) + ".")
    info = {
        "type": "service_account",
        "client_email": settings.GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL,
        "private_key": settings.GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    if getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY_ID", ""):
        info["private_key_id"] = settings.GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY_ID
    try:
        credenciales = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    except (ValueError, TypeError):
        raise ErrorChat(
            "Revisa la clave privada PEM de la cuenta de servicio de Chat (GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY: "
            "entre comillas simples, en una línea, con los \\n)."
        ) from None
    return credenciales.with_subject(subject) if subject else credenciales


def _sesion(subject=None, *, pubsub=False):
    """Sesión autorizada como `subject` (por defecto la cuenta de Órbita); `pubsub=True`: la cuenta de servicio
    misma, sin suplantar a nadie. Se reutiliza por proceso (los tokens se renuevan solos)."""
    from google.auth.transport.requests import AuthorizedSession

    clave = ("pubsub", "") if pubsub else ("chat", (subject or cuenta()).lower())
    if clave not in _sesiones:
        if pubsub:
            _sesiones[clave] = AuthorizedSession(_credenciales(None, SCOPE_PUBSUB))
        else:
            _sesiones[clave] = AuthorizedSession(_credenciales(clave[1], tuple(settings.GOOGLE_CHAT_SCOPES)))
    return _sesiones[clave]


def _pedir(metodo, url, *, subject=None, pubsub=False, permitidos=(), **kwargs):
    """Una petición; devuelve `(codigo, json)`. Los códigos de `permitidos` no son error (p. ej. 409)."""
    import requests
    from google.auth.exceptions import RefreshError

    try:
        respuesta = _sesion(subject, pubsub=pubsub).request(metodo, url, timeout=TIEMPO_ESPERA, **kwargs)
    except RefreshError:
        quien = "la cuenta de servicio" if pubsub else (subject or cuenta())
        raise ErrorChat(
            f"Google no autorizó la delegación para {quien}: revisa que el administrador haya autorizado los scopes "
            "de Chat para el client_id de la cuenta de servicio y que la persona exista en Workspace."
        ) from None
    except requests.RequestException as error:
        raise ErrorChat(f"Google no respondió ({type(error).__name__}).") from None
    try:
        datos = respuesta.json() if respuesta.content else {}
    except ValueError:
        datos = {}
    if respuesta.status_code >= 400 and respuesta.status_code not in permitidos:
        detalle = ((datos.get("error") or {}).get("message") or "")[:200] if isinstance(datos, dict) else ""
        raise ErrorChat(f"Google respondió {respuesta.status_code}. {detalle}".strip(), codigo=respuesta.status_code)
    return respuesta.status_code, datos


def id_cliente(texto):
    """`messageId` válido para Chat: «client-» + minúsculas, números y guiones (máx. 63)."""
    limpio = re.sub(r"[^a-z0-9-]", "-", (texto or "").lower())
    return f"client-{limpio}"[:63]


def _usuario(email):
    return {"name": f"users/{email}", "type": "HUMAN"}


# --- Espacios ---------------------------------------------------------------------------------------------------

def crear_espacio(nombre, id_solicitud):
    """Crea un espacio con nombre como la cuenta de Órbita (queda como su administradora). Idempotente por
    `id_solicitud`: repetirlo devuelve el mismo espacio."""
    _codigo, datos = _pedir("POST", f"{API}/spaces:setup", json={
        "space": {"spaceType": "SPACE", "displayName": (nombre or "Órbita")[:128]},
        "requestId": id_solicitud,
    })
    if not str(datos.get("name", "")).startswith("spaces/"):
        raise ErrorChat("Google Chat no devolvió el espacio creado.")
    return datos


def _permiso(espacio, campo, *, miembros):
    """Cambia quién puede hacer `campo` (p. ej. publicar) en el espacio: administradores siempre; miembros según
    `miembros`."""
    cuerpo = {"permissionSettings": {campo: {"managersAllowed": True, "membersAllowed": bool(miembros)}}}
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", campo).lower()
    try:
        _pedir("PATCH", f"{API}/{espacio}", params={"updateMask": f"permissionSettings.{campo}"}, json=cuerpo)
    except ErrorChat as error:
        if error.codigo != 400:
            raise
        _pedir("PATCH", f"{API}/{espacio}", params={"updateMask": f"permission_settings.{snake}"}, json=cuerpo)


def restringir_miembros(espacio):
    """Solo la cuenta de Órbita agrega o quita personas (R2: la pertenencia la decide Órbita)."""
    _permiso(espacio, "manageMembersAndGroups", miembros=False)


def solo_lectura(espacio, activar):
    """D4: con el ticket cerrado o cancelado nadie más que la cuenta de Órbita publica."""
    _permiso(espacio, "postMessages", miembros=not activar)


# --- Miembros ---------------------------------------------------------------------------------------------------

def obtener_miembro(espacio, email):
    _codigo, datos = _pedir("GET", f"{API}/{espacio}/members/{email}")
    return datos


def agregar_miembro(espacio, email):
    """`{"membresia": "spaces/…/members/…", "usuario": "users/<id>"}` de la persona en el espacio (si ya estaba,
    la que tiene)."""
    codigo, datos = _pedir("POST", f"{API}/{espacio}/members", json={"member": _usuario(email)}, permitidos=(409,))
    if codigo == 409:
        datos = obtener_miembro(espacio, email)
    return {"membresia": datos.get("name", ""), "usuario": (datos.get("member") or {}).get("name", "")}


def quitar_miembro(membresia):
    _pedir("DELETE", f"{API}/{membresia}", permitidos=(404,))


def listar_miembros(espacio):
    """Membresías humanas del espacio: `[{"name", "role", "member": {"name", "type"}}]`."""
    miembros, pagina = [], None
    while True:
        params = {"pageSize": 1000, "filter": 'member.type = "HUMAN"'}
        if pagina:
            params["pageToken"] = pagina
        _codigo, datos = _pedir("GET", f"{API}/{espacio}/members", params=params)
        miembros += datos.get("memberships", [])
        pagina = datos.get("nextPageToken")
        if not pagina:
            return miembros


# --- Mensajes ---------------------------------------------------------------------------------------------------

def publicar(espacio, texto, id_mensaje, *, como=None):
    """Publica `texto` en el espacio como `como` (por defecto la cuenta de Órbita). Idempotente por `id_mensaje`:
    si ya existía, devuelve su nombre. Devuelve el `name` del mensaje."""
    codigo, datos = _pedir(
        "POST", f"{API}/{espacio}/messages", subject=como, params={"messageId": id_mensaje},
        json={"text": texto[:4000]}, permitidos=(409,),
    )
    if codigo == 409:
        return f"{espacio}/messages/{id_mensaje}"
    return str(datos.get("name", ""))[:200]


def obtener_mensaje(nombre):
    _codigo, datos = _pedir("GET", f"{API}/{nombre}")
    return datos


def listar_mensajes(espacio, desde):
    """Mensajes del espacio creados DESPUÉS de `desde` (datetime con zona), en orden cronológico."""
    filtro = f'createTime > "{desde.isoformat()}"'
    mensajes, pagina = [], None
    while True:
        params = {"pageSize": 100, "filter": filtro, "orderBy": "createTime asc"}
        if pagina:
            params["pageToken"] = pagina
        _codigo, datos = _pedir("GET", f"{API}/{espacio}/messages", params=params)
        mensajes += datos.get("messages", [])
        pagina = datos.get("nextPageToken")
        if not pagina or len(mensajes) >= 500:
            return mensajes


# --- Eventos en tiempo real (Workspace Events → Pub/Sub) -------------------------------------------------------

def buscar_suscripcion(espacio):
    filtro = f'event_types:"{EVENTO_CREADO}" AND target_resource="//chat.googleapis.com/{espacio}"'
    _codigo, datos = _pedir("GET", f"{EVENTOS}/subscriptions", params={"filter": filtro})
    suscripciones = datos.get("subscriptions", [])
    return suscripciones[0] if suscripciones else None


def suscribir(espacio, tema):
    """Suscribe la cuenta de Órbita a los mensajes nuevos del espacio, entregados al tema de Pub/Sub. Sin el
    recurso en el evento (así dura hasta 7 días): el mensaje se lee después por su nombre. Devuelve la
    suscripción (o `None` si Google aún la está creando; el reconciliador la busca después)."""
    _pedir("POST", f"{EVENTOS}/subscriptions", permitidos=(409,), json={
        "targetResource": f"//chat.googleapis.com/{espacio}",
        "eventTypes": [EVENTO_CREADO],
        "notificationEndpoint": {"pubsubTopic": tema},
        "payloadOptions": {"includeResource": False},
    })
    return buscar_suscripcion(espacio)


def renovar_suscripcion(nombre):
    """Extiende la suscripción al máximo permitido (`ttl` 0s). Devuelve la suscripción."""
    _pedir("PATCH", f"{EVENTOS}/{nombre}", params={"updateMask": "ttl"}, json={"ttl": "0s"})
    _codigo, datos = _pedir("GET", f"{EVENTOS}/{nombre}")
    return datos


def recibir(suscripcion, maximo=100):
    """Mensajes pendientes de la suscripción PULL de Pub/Sub: `[(ack_id, tipo, datos_json)]`."""
    _codigo, datos = _pedir("POST", f"{PUBSUB}/{suscripcion}:pull", pubsub=True, json={"maxMessages": maximo})
    recibidos = []
    for item in datos.get("receivedMessages", []):
        mensaje = item.get("message") or {}
        atributos = mensaje.get("attributes") or {}
        try:
            contenido = json.loads(base64.b64decode(mensaje.get("data") or b"") or b"{}")
        except (ValueError, TypeError):
            contenido = {}
        recibidos.append((item.get("ackId", ""), atributos.get("ce-type", ""), contenido))
    return recibidos


def confirmar(suscripcion, ack_ids):
    if ack_ids:
        _pedir("POST", f"{PUBSUB}/{suscripcion}:acknowledge", pubsub=True, json={"ackIds": list(ack_ids)})
