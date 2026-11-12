"""Apoyo de las pruebas automáticas (no lo usa la aplicación).

Versión 1: las cuentas son correos y el único ingreso es Google, así que las pruebas crean personas con
`correo_de_prueba(alias)` y abren su sesión con `client.force_login(persona(alias))` (sin contraseña). El dominio de
prueba no es corporativo: nadie podría ingresar con él a una instalación real."""

from django.contrib.auth import get_user_model

DOMINIO_PRUEBAS = "pruebas.orbita"


def correo_de_prueba(alias):
    alias = str(alias)
    return alias if "@" in alias else f"{alias}@{DOMINIO_PRUEBAS}"


def persona(referencia):
    """La persona de la prueba por su correo o por el alias con el que se creó (parte local del correo)."""
    return get_user_model().objects.por_referencia(referencia).get()
