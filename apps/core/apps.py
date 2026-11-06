from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = "Órbita Core"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from apps.core.accesos import registrar_acceso

        user_logged_in.connect(registrar_acceso, dispatch_uid="orbita_registrar_acceso")
