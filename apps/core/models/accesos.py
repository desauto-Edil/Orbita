from django.conf import settings
from django.db import models


class AccesoUsuario(models.Model):
    """Inicio de sesión EXITOSO de una persona (CU-001 lo registra, CU-003 lo muestra en Mi perfil).

    Append-only: no se edita ni se borra. Solo guarda lo que la persona necesita para reconocer
    sus accesos (cuándo, cómo y desde qué navegador/sistema): ni la IP ni el User-Agent completo.
    La sesión se identifica por una huella SHA-256 de su clave (nunca la clave), solo para marcar
    «Esta sesión». Los intentos fallidos no se registran (decisión de producto).
    """

    class Metodo(models.TextChoices):
        CONTRASENA = "CONTRASENA", "Usuario y contraseña"
        GOOGLE = "GOOGLE", "Google"

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="accesos"
    )
    fecha = models.DateTimeField(auto_now_add=True, db_index=True)
    metodo = models.CharField(max_length=12, choices=Metodo.choices)
    navegador = models.CharField(max_length=40, blank=True)
    sistema_operativo = models.CharField(max_length=40, blank=True)
    es_movil = models.BooleanField(default=False)
    huella_sesion = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        verbose_name = "acceso de usuario"
        verbose_name_plural = "accesos de usuarios"
        ordering = ("-fecha", "-pk")
        indexes = [models.Index(fields=("usuario", "-fecha"), name="acceso_usuario_fecha")]

    def __str__(self):
        return f"{self.usuario} · {self.get_metodo_display()} · {self.fecha:%Y-%m-%d %H:%M}"
