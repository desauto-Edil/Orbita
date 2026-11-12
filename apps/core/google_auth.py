"""CU-001/003: identidad Google para cuentas locales existentes; sin permisos Gmail.

Versión 1 (despliegue): Google es la ÚNICA forma de ingresar. Solo entra quien el administrador ya
registró (por correo, en un dominio permitido); nunca se crean cuentas al primer ingreso. El destino
al que la persona quería llegar (`next`, p. ej. el enlace de un correo) se valida como ruta interna
al iniciar, se guarda en la sesión durante el viaje a Google y se usa al volver.

CONFIRMAR IDENTIDAD (etapa 4): con la sesión ya abierta, `reautenticar=1` vuelve a pasar por Google sin cerrar la
sesión; al volver solo se acepta la MISMA persona y se registra un acceso nuevo con Google (lo que exige la
administración avanzada, `apps.core.plataforma.ingreso_google_reciente`)."""

from urllib.parse import urlencode, urlsplit

from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, resolve_url
from django.views.decorators.http import require_GET, require_POST

from apps.core.accesos import marcar_metodo
from apps.core.backends import RUTA as RUTA_BACKEND
from apps.core.models import AccesoUsuario
from apps.core.redirecciones import destino_interno

# Clave de sesión con el destino validado mientras la persona está en Google.
SESION_DESTINO = "orbita.login_destino"
# Persona que pidió confirmar su identidad con la sesión abierta (`reautenticar`).
SESION_REAUTENTICAR = "orbita.reautenticar"


def _destino_final(destino):
    return destino or resolve_url(settings.LOGIN_REDIRECT_URL)


def _al_login(destino=""):
    """Vuelve al inicio de sesión conservando el destino para el siguiente intento."""
    url = resolve_url(settings.LOGIN_URL)
    return redirect(f"{url}?{urlencode({'next': destino})}" if destino else url)


def configurado():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def cliente():
    oauth = OAuth()
    return oauth.register(
        "google",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": " ".join(settings.GOOGLE_OAUTH_SCOPES),
            "code_challenge_method": "S256",
            "timeout": 10,
        },
    )


@transaction.atomic
def resolver_usuario(claims):
    """Solo recibe claims del ID token validado por Authlib, nunca datos del navegador."""
    sub = claims.get("sub")
    email = claims.get("email", "").strip().lower()
    dominio = claims.get("hd", "").lower()
    permitidos = {d.lower() for d in settings.GOOGLE_OAUTH_ALLOWED_DOMAINS}
    if (
        not isinstance(sub, str) or not sub or len(sub) > 255
        or claims.get("email_verified") is not True
        or dominio not in permitidos
        or email.rpartition("@")[2] not in permitidos
    ):
        raise PermissionDenied

    Usuario = get_user_model()
    # También se cuentan las cuentas inactivas para no resolver correos ambiguos.
    candidatos = list(Usuario.objects.select_for_update().filter(email__iexact=email)[:2])
    if len(candidatos) != 1 or not candidatos[0].is_active:
        raise PermissionDenied
    usuario = candidatos[0]
    if usuario.google_sub and usuario.google_sub != sub:
        raise PermissionDenied
    if Usuario.objects.filter(google_sub=sub).exclude(pk=usuario.pk).exists():
        raise PermissionDenied
    foto = claims.get("picture", "")
    if not isinstance(foto, str) or len(foto) > 2048:
        foto = ""
    partes = urlsplit(foto)
    if partes.scheme != "https" or not partes.hostname or partes.username or partes.password:
        foto = ""
    usuario.google_sub = sub
    usuario.google_foto_url = foto
    usuario.save(update_fields=["google_sub", "google_foto_url"])
    return usuario


@require_POST
def iniciar(request):
    destino = destino_interno(request, request.POST.get("next"))
    reautenticar = request.user.is_authenticated and request.POST.get("reautenticar") == "1"
    if request.user.is_authenticated and not reautenticar:
        return redirect(_destino_final(destino))
    if not configurado():
        messages.error(request, "El ingreso con Google aún no está configurado. Contacta al administrador de Órbita.")
        return _al_login(destino)
    if destino:
        request.session[SESION_DESTINO] = destino
    else:
        request.session.pop(SESION_DESTINO, None)
    if reautenticar:
        request.session[SESION_REAUTENTICAR] = request.user.pk
    else:
        request.session.pop(SESION_REAUTENTICAR, None)
    try:
        return cliente().authorize_redirect(
            request, settings.GOOGLE_OAUTH_REDIRECT_URI, prompt="select_account"
        )
    except Exception:
        # No exponer tokens, secretos ni respuestas del proveedor en pantalla o logs.
        messages.error(request, "No se pudo conectar con Google. Inténtalo nuevamente.")
        return _al_login(destino)


@require_GET
def callback(request):
    # Se revalida al volver: la sesión pudo cambiar de host o de esquema entre ida y vuelta.
    destino = destino_interno(request, request.session.pop(SESION_DESTINO, ""))
    reautenticando = request.session.pop(SESION_REAUTENTICAR, None)
    if request.user.is_authenticated and reautenticando != request.user.pk:
        return redirect(_destino_final(destino))
    if not configurado():
        messages.error(request, "El ingreso con Google aún no está configurado.")
        return _al_login(destino)
    try:
        # Authlib valida state, firma, emisor, audiencia, expiración y nonce.
        token = cliente().authorize_access_token(request)
        usuario = resolver_usuario(token["userinfo"])
    except (PermissionDenied, IntegrityError):
        messages.error(
            request,
            "Tu cuenta no está registrada o no está habilitada en Órbita. Pide al administrador que la cree con tu "
            "correo corporativo.",
        )
        return _al_login(destino)
    except Exception:
        messages.error(request, "No se pudo validar el ingreso con Google. Vuelve a intentarlo.")
        return _al_login(destino)
    if request.user.is_authenticated and usuario.pk != request.user.pk:
        # Confirmar la identidad no cambia de persona: con otra cuenta de Google no se confirma nada.
        messages.error(request, "Confirma tu identidad con la misma cuenta de Google con la que iniciaste sesión.")
        return redirect(_destino_final(destino))
    # No persistir access_token, refresh_token ni ID token: solo identidad y foto.
    marcar_metodo(request, AccesoUsuario.Metodo.GOOGLE)
    login(request, usuario, backend=RUTA_BACKEND)
    return redirect(_destino_final(destino))
