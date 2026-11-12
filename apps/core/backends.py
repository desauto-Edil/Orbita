"""Backend de sesión de Órbita (Versión 1).

No autentica a nadie con usuario y contraseña (no existen): el único ingreso es Google (`apps.core.google_auth`, que
llama a `login()` con este backend). Solo recupera a la persona de su sesión, y únicamente si sigue ACTIVA.
Reemplaza a `ModelBackend`, que dependía de la contraseña y de los grupos/permisos de Django."""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend

RUTA = "apps.core.backends.CuentaOrbitaBackend"


class CuentaOrbitaBackend(BaseBackend):
    def get_user(self, user_id):
        usuario = get_user_model()._default_manager.filter(pk=user_id).first()
        return usuario if usuario is not None and usuario.is_active else None
