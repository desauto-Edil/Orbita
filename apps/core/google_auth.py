"""CU-001/003: identidad Google para cuentas locales existentes; sin permisos Gmail."""

from urllib.parse import urlsplit

from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST

from apps.core.accesos import marcar_metodo
from apps.core.models import AccesoUsuario


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
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    if not configurado():
        messages.error(request, "El ingreso con Google aún no está configurado. Utiliza tu usuario y contraseña.")
        return redirect(settings.LOGIN_URL)
    try:
        return cliente().authorize_redirect(
            request, settings.GOOGLE_OAUTH_REDIRECT_URI, prompt="select_account"
        )
    except Exception:
        # No exponer tokens, secretos ni respuestas del proveedor en pantalla o logs.
        messages.error(request, "No se pudo conectar con Google. Inténtalo nuevamente.")
        return redirect(settings.LOGIN_URL)


@require_GET
def callback(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    if not configurado():
        messages.error(request, "El ingreso con Google aún no está configurado.")
        return redirect(settings.LOGIN_URL)
    try:
        # Authlib valida state, firma, emisor, audiencia, expiración y nonce.
        token = cliente().authorize_access_token(request)
        usuario = resolver_usuario(token["userinfo"])
    except (PermissionDenied, IntegrityError):
        messages.error(request, "Tu cuenta no está habilitada para ingresar con Google. Contacta al administrador de Órbita.")
        return redirect(settings.LOGIN_URL)
    except Exception:
        messages.error(request, "No se pudo validar el ingreso con Google. Vuelve a intentarlo desde el inicio de sesión.")
        return redirect(settings.LOGIN_URL)
    # No persistir access_token, refresh_token ni ID token: solo identidad y foto.
    marcar_metodo(request, AccesoUsuario.Metodo.GOOGLE)
    login(request, usuario, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(settings.LOGIN_REDIRECT_URL)
