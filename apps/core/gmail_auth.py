"""Conexión administrativa de un único remitente Gmail; independiente del login."""

import os
from pathlib import Path
import re
import tempfile

from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods

from apps.core.correo import ErrorCorreo


def cliente():
    return OAuth().register(
        "gmail",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": " ".join(("openid", "email", *settings.GMAIL_SCOPES)),
            "code_challenge_method": "S256", "timeout": 10,
        },
    )


@sensitive_variables()
def guardar_token(refresh_token):
    """Actualiza solo modo/token; no imprime ni guarda el token en base de datos."""
    if not isinstance(refresh_token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/\-]+", refresh_token):
        raise ErrorCorreo("Google no devolvió un token de renovación válido. Vuelve a autorizar Gmail.")
    path = Path(settings.BASE_DIR) / ".env"
    original = path.read_text()
    updates = {"GMAIL_AUTH_MODE": "oauth", "GMAIL_REFRESH_TOKEN": refresh_token}
    lines = original.splitlines(keepends=True)
    for key in updates:
        if sum(line.startswith(key + "=") for line in lines) > 1:
            raise ErrorCorreo("Hay variables Gmail duplicadas en .env. Corrígelas antes de conectar.")
    found = set()
    output = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in updates:
            output.append(f"{key}={updates[key]}\n")
            found.add(key)
        else:
            output.append(line)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    for key in updates.keys() - found:
        output.append(f"{key}={updates[key]}\n")
    # Archivo temporal privado y reemplazo atómico, sin una copia adicional del secreto.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".env-gmail-", delete=False) as file:
            temporary = Path(file.name)
            file.write("".join(output))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    settings.GMAIL_AUTH_MODE = "oauth"
    settings.GMAIL_REFRESH_TOKEN = refresh_token


def exigir_administrador(request):
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def conectar(request):
    exigir_administrador(request)
    if request.method == "POST":
        if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET or not settings.GMAIL_SENDER_EMAIL:
            messages.error(request, "Configura las credenciales OAuth y GMAIL_SENDER_EMAIL en .env.")
            return redirect("core:gmail_conectar")
        try:
            return cliente().authorize_redirect(
                request, settings.GMAIL_OAUTH_REDIRECT_URI,
                access_type="offline", prompt="consent select_account",
                login_hint=settings.GMAIL_SENDER_EMAIL,
            )
        except Exception:
            messages.error(request, "No se pudo iniciar la conexión con Gmail. Inténtalo nuevamente.")
            return redirect("core:gmail_conectar")
    return render(request, "cuentas/gmail.html", {
        "remitente": settings.GMAIL_SENDER_EMAIL,
        "gmail_conectado": settings.GMAIL_AUTH_MODE == "oauth" and bool(settings.GMAIL_REFRESH_TOKEN),
    })


@never_cache
@login_required
@require_GET
@sensitive_variables()
def callback(request):
    exigir_administrador(request)
    try:
        # Authlib valida state, nonce y el ID token; no se confía en login_hint.
        token = cliente().authorize_access_token(request)
        claims = token["userinfo"]
        if claims.get("email_verified") is not True or claims.get("email", "").lower() != settings.GMAIL_SENDER_EMAIL.lower():
            raise ErrorCorreo("Autoriza exactamente el buzón remitente configurado en Órbita.")
        if settings.GMAIL_SCOPES[0] not in token.get("scope", "").split():
            raise ErrorCorreo("Debes aceptar el permiso de envío para conectar Gmail.")
        guardar_token(token.get("refresh_token"))
    except ErrorCorreo as exc:
        messages.error(request, str(exc))
    except OSError:
        messages.error(request, "No se pudo guardar la autorización en .env. Revisa los permisos del archivo.")
    except Exception:
        messages.error(request, "No se pudo validar la autorización Gmail. Inicia la conexión nuevamente.")
    else:
        messages.success(request, "Autorización Gmail guardada. Ya puedes ejecutar la prueba de envío.")
    return redirect("core:gmail_conectar")
