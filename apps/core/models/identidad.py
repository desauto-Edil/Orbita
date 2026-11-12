import re
import secrets
from urllib.parse import urlsplit

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import _user_has_module_perms, _user_has_perm
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.core.temas import OPCIONES as OPCIONES_TEMA

from .base import RegistroBase


_TAMANO_FOTO_GOOGLE = re.compile(r"=s\d+(-c)?$")


def normalizar_correo(correo):
    """El correo ES la identidad de la cuenta: sin espacios y en minúsculas (Google lo entrega así)."""
    return (correo or "").strip().lower()


def nueva_clave_sesion():
    return secrets.token_hex(32)


class UsuarioManager(BaseUserManager):
    """Versión 1: una cuenta es un CORREO (lo crea el administrador antes del primer ingreso). Sin contraseña, sin
    `create_superuser`: el primer administrador lo da `manage.py otorgar_administrador`."""

    use_in_migrations = True

    def create_user(self, email, **campos):
        email = normalizar_correo(email)
        if not email:
            raise ValueError("El correo es obligatorio: es el identificador de la cuenta.")
        usuario = self.model(email=email, **campos)
        usuario.save(using=self._db)
        return usuario

    def get_by_natural_key(self, email):
        return self.get(email__iexact=normalizar_correo(email))

    def por_referencia(self, referencia):
        """Personas que coinciden con `referencia` en herramientas de consola (demos, pruebas de integración): un
        correo completo, o solo su parte local (`lau` → `lau@…`). Quien llama exige que haya exactamente una."""
        referencia = normalizar_correo(referencia)
        if not referencia:
            return self.none()
        if "@" in referencia:
            return self.filter(email__iexact=referencia)
        return self.filter(email__istartswith=f"{referencia}@")


class Usuario(AbstractBaseUser):
    """Usuario de autenticación (AUTH_USER_MODEL). CU-001, CU-002, CU-003.

    Versión 1 (despliegue, 2026-11-12): el identificador único es el CORREO y lo que ven los compañeros es el NOMBRE
    (`nombre_visible`). No hay usuario, contraseña, `is_staff`, `is_superuser`, grupos ni permisos de Django: el
    único ingreso es Google (`apps.core.google_auth`) y los accesos son roles y permisos de Órbita
    (`apps.core.autorizacion`). La sesión se firma con `clave_sesion` (en lugar del hash de la contraseña):
    cambiarla (`cerrar_sesiones`) cierra todas las sesiones abiertas de la persona."""

    password = None  # el ingreso es solo con Google: no hay contraseña que guardar

    email = models.EmailField("correo", unique=True, error_messages={"unique": "Ya existe una persona con ese correo."})
    first_name = models.CharField("nombres", max_length=150, blank=True)
    last_name = models.CharField("apellidos", max_length=150, blank=True)
    is_active = models.BooleanField(
        "cuenta activa", default=True,
        help_text="Una cuenta inactiva no puede ingresar; su historial se conserva. Desactivar en lugar de borrar.",
    )
    date_joined = models.DateTimeField("fecha de alta", default=timezone.now)
    clave_sesion = models.CharField(max_length=64, default=nueva_clave_sesion, editable=False)

    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True, editable=False)
    google_foto_url = models.URLField(max_length=2048, blank=True, editable=False)

    class Apariencia(models.TextChoices):
        """Preferencia personal de modo claro/oscuro (Fase visual, Mi perfil). SISTEMA sigue al
        dispositivo; se aplica con `<html data-apariencia>` y la capa oscura de `tokens.css`."""

        SISTEMA = "SISTEMA", "Automático"
        CLARO = "CLARO", "Claro"
        OSCURO = "OSCURO", "Oscuro"

    apariencia = models.CharField(max_length=10, choices=Apariencia.choices, default=Apariencia.SISTEMA)
    # Presencia (Conversación del ticket): la última vez que la persona tenía Órbita abierta. Lo escribe solo
    # `apps.core.presencia` (como mucho cada 30 s); «en línea» = visto en los últimos 2 minutos. No se audita.
    visto_en = models.DateTimeField(null=True, blank=True, editable=False)
    # Versión 1 (wizard de bienvenida): color PERSONAL de acento, solo para esta persona (vacío = el del sistema,
    # `ConfiguracionSistema.tema`). Misma lista curada (`apps/core/temas.py`): nunca un color libre.
    tema = models.CharField(max_length=20, choices=OPCIONES_TEMA, blank=True, default="")
    # Cuándo terminó (u omitió) la bienvenida. Vacío = se le muestra al entrar (`apps.core.bienvenida`).
    bienvenida_completada_en = models.DateTimeField(null=True, blank=True, editable=False)

    objects = UsuarioManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "persona"
        verbose_name_plural = "personas"
        constraints = [
            models.UniqueConstraint(Lower("email"), name="uq_usuario_correo_sin_mayusculas"),
        ]

    def __str__(self):
        nombre = self.get_full_name()
        return f"{nombre} ({self.email})" if nombre else self.email

    def save(self, *args, **kwargs):
        self.email = normalizar_correo(self.email)
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self.email = normalizar_correo(self.email)

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name

    @property
    def nombre_visible(self):
        """Lo que ven los compañeros: el nombre; si la cuenta aún no lo tiene, su correo."""
        return self.get_full_name() or self.email

    # --- Sin contraseña (el único ingreso es Google) ---

    def set_password(self, raw_password):
        raise ValueError("Órbita no usa contraseñas: el ingreso es con Google.")

    def check_password(self, raw_password):
        return False

    def set_unusable_password(self):
        pass

    def has_usable_password(self):
        return False

    def _get_session_auth_hash(self, secret=None):
        """Firma de la sesión (Django la compara en cada petición): sale de `clave_sesion`, no de una contraseña."""
        return salted_hmac(
            "apps.core.Usuario.sesion", self.clave_sesion, secret=secret, algorithm="sha256"
        ).hexdigest()

    def cerrar_sesiones(self):
        """Invalida TODAS las sesiones abiertas de la persona (la próxima petición de cada una pide ingresar)."""
        self.clave_sesion = nueva_clave_sesion()
        self.save(update_fields=["clave_sesion"])

    # --- Permisos de modelo (solo los usa la administración avanzada; ver `apps.core.admin_sitio`) ---

    def has_perm(self, perm, obj=None):
        return self.is_active and _user_has_perm(self, perm, obj)

    def has_perms(self, perm_list, obj=None):
        return all(self.has_perm(perm, obj) for perm in perm_list)

    def has_module_perms(self, app_label):
        return self.is_active and _user_has_module_perms(self, app_label)

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
        return f"Perfil de {self.usuario.nombre_visible}"
