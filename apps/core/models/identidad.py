from django.contrib.auth.models import AbstractUser
from django.db import models

from .base import RegistroBase


class Usuario(AbstractUser):
    """Usuario de autenticación (AUTH_USER_MODEL). CU-001, CU-002, CU-003."""


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
