"""Solo desarrollo: cuándo se muestra la barra de depuración de Django (`DEBUG_TOOLBAR_CONFIG`)."""

from django.conf import settings


def mostrar_barra(request):
    return settings.DEBUG and getattr(settings, "DEBUG_TOOLBAR_VISIBLE", False)
