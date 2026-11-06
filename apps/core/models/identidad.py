import re
from urllib.parse import urlsplit

from django.contrib.auth.models import AbstractUser
from django.db import models

from .base import RegistroBase


_TAMANO_FOTO_GOOGLE = re.compile(r"=s\d+(-c)?$")


class Usuario(AbstractUser):
    """Usuario de autenticación (AUTH_USER_MODEL). CU-001, CU-002, CU-003."""

    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True, editable=False)
    google_foto_url = models.URLField(max_length=2048, blank=True, editable=False)

    class Apariencia(models.TextChoices):
        """Preferencia personal de modo claro/oscuro (Fase visual, Mi perfil). SISTEMA sigue al
        dispositivo; se aplica con `<html data-apariencia>` y la capa oscura de `tokens.css`."""

        SISTEMA = "SISTEMA", "Automático"
        CLARO = "CLARO", "Claro"
        OSCURO = "OSCURO", "Oscuro"

    apariencia = models.CharField(max_length=10, choices=Apariencia.choices, default=Apariencia.SISTEMA)

    def foto_google(self, tamano):
        """URL de la foto de Google en `tamano` px. Google la entrega en 96 px (`…=s96-c`) y el mismo
        servidor la sirve en cualquier tamaño cambiando ese sufijo; mostrarla más grande sin pedirla
        así la pixela. Otro formato u otro host se devuelve tal cual."""
        url = self.google_foto_url
        if not url or not (urlsplit(url).hostname or "").endswith(".googleusercontent.com"):
            return url
        return _TAMANO_FOTO_GOOGLE.sub(f"=s{int(tamano)}-c", url)

    @property
    def foto_grande(self):
        """Foto para Mi perfil (136 px en pantalla, nítida hasta 3x)."""
        return self.foto_google(480)

    @property
    def foto_mediana(self):
        """Foto para listas de personas (56 px en pantalla, nítida hasta 3x)."""
        return self.foto_google(192)


class PerfilOrganizacional(RegistroBase):
    """Información organizacional del usuario, separada de la autenticación.

    RQF-015: gestionar usuarios y perfiles separando autenticación de
    información organizacional.
    """

    usuario = models.OneToOneField(
        Usuario, on_delete=models.CASCADE, related_name="perfil_organizacional"
    )
    cargo = models.CharField(max_length=150, blank=True)
    telefono = models.CharField(max_length=30, blank=True)

    def __str__(self):
        return f"Perfil de {self.usuario.get_username()}"
