"""Registro y lectura de accesos (CU-001 registra, CU-003 muestra en Mi perfil).

El registro cuelga de la señal `user_logged_in` de Django, así cubre el inicio con contraseña y
con Google sin tocar las vistas de login; Google marca su método con `marcar_metodo` antes de
llamar a `login()`. Del User-Agent solo se conserva un resumen legible (navegador, sistema y si es
móvil): nunca la cadena completa ni la IP.
"""

import hashlib

from apps.core.models import AccesoUsuario

ATRIBUTO_METODO = "_orbita_metodo_acceso"
LIMITE_PERFIL = 6

# Orden importa: Edge y Opera también dicen «Chrome», y Chrome también dice «Safari».
_NAVEGADORES = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SISTEMAS = (("iPhone", "iOS"), ("iPad", "iPadOS"), ("Android", "Android"), ("Windows", "Windows"), ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def marcar_metodo(request, metodo):
    setattr(request, ATRIBUTO_METODO, metodo)


def resumir_agente(agente):
    """(navegador, sistema operativo, es_movil) a partir del User-Agent; vacíos si no se reconoce."""
    agente = agente or ""
    navegador = next((nombre for marca, nombre in _NAVEGADORES if marca in agente), "")
    sistema = next((nombre for marca, nombre in _SISTEMAS if marca in agente), "")
    return navegador, sistema, "Mobile" in agente or sistema in ("iOS", "Android")


def huella_de_sesion(request):
    clave = getattr(getattr(request, "session", None), "session_key", None)
    return hashlib.sha256(clave.encode()).hexdigest() if clave else ""


def registrar_acceso(sender, request, user, **kwargs):
    """Receptor de `user_logged_in`. Django ya rotó la clave de sesión antes de la señal."""
    if request is None:
        return
    navegador, sistema, es_movil = resumir_agente(request.META.get("HTTP_USER_AGENT", ""))
    AccesoUsuario.objects.create(
        usuario=user,
        metodo=getattr(request, ATRIBUTO_METODO, AccesoUsuario.Metodo.CONTRASENA),
        navegador=navegador,
        sistema_operativo=sistema,
        es_movil=es_movil,
        huella_sesion=huella_de_sesion(request),
    )


def ultimos_accesos(usuario, request, limite=LIMITE_PERFIL):
    """Últimos accesos de la persona, marcando el de la sesión actual."""
    actual = huella_de_sesion(request)
    accesos = list(usuario.accesos.all()[:limite])
    for acceso in accesos:
        acceso.es_esta_sesion = bool(actual) and acceso.huella_sesion == actual
    return accesos
