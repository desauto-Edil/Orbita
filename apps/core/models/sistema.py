from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models

from .base import RegistroBase

NOMBRE_POR_DEFECTO = "Órbita"
EXTENSIONES_LOGO = ("png", "jpg", "jpeg", "webp")
TAMANO_MAXIMO_LOGO = 1024 * 1024  # 1 MB

# Firma (primeros bytes) de cada formato admitido. Se comprueba el contenido,
# no solo la extensión: el logo se sirve a cualquier visitante (también en la
# pantalla de inicio de sesión). SVG queda fuera a propósito: puede llevar
# scripts.
_FIRMAS_LOGO = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def validar_logo(archivo):
    if archivo.size > TAMANO_MAXIMO_LOGO:
        raise ValidationError("El logo no puede pesar más de 1 MB.")
    cabecera = archivo.read(12)
    archivo.seek(0)
    es_webp = cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WEBP"
    if not es_webp and not cabecera.startswith(_FIRMAS_LOGO):
        raise ValidationError("El archivo no es una imagen PNG, JPG o WebP válida.")


class ConfiguracionSistema(RegistroBase):
    """Identidad configurable de la plataforma (nombre y logo): una sola fila.

    «Configuración antes que código»: cambiar cómo se llama o qué logo muestra
    la plataforma no exige tocar templates ni redesplegar. `actual()` nunca
    escribe: mientras nadie haya guardado la configuración devuelve una
    instancia en memoria con los valores por defecto.
    """

    PK_UNICA = 1

    nombre = models.CharField(max_length=60, default=NOMBRE_POR_DEFECTO)
    logo = models.FileField(
        upload_to="sistema/",
        blank=True,
        validators=[FileExtensionValidator(EXTENSIONES_LOGO), validar_logo],
    )

    class Meta:
        verbose_name = "configuración del sistema"
        verbose_name_plural = "configuración del sistema"

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        self.pk = self.PK_UNICA
        super().save(*args, **kwargs)

    @classmethod
    def actual(cls):
        return cls.objects.filter(pk=cls.PK_UNICA).first() or cls(pk=cls.PK_UNICA)

    @property
    def inicial(self):
        return self.nombre[:1].upper()
