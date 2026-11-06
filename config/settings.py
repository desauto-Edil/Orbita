"""
Configuración única de Órbita (Sprint 0, incremento 0.1).

Decisión confirmada: un solo settings.py leído por variables de entorno,
sin separar base/development/production todavía (se revisita cuando exista
una necesidad concreta de producción real).
"""

import sys
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    # Servidor ASGI (Conversación del ticket en tiempo real). Va primero: así `runserver` sirve HTTP y WebSocket.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django_celery_beat",
    "channels",
    "apps.core",
    "apps.catalogo",
    "apps.tickets",
    "apps.tareas",
    "apps.workflows",
    "apps.aprobaciones",
    "apps.gacetas",
    "apps.notificaciones",
]

if DEBUG:
    INSTALLED_APPS += [
        "django_extensions",
        "debug_toolbar",
    ]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if DEBUG:
    MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
    INTERNAL_IPS = ["127.0.0.1"]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.navegacion",
                "apps.core.context_processors.sistema",
                "apps.notificaciones.context_processors.notificaciones",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL"),
}

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")

# Capa de canales (Django Channels) para la Conversación del ticket en tiempo real. Redis solo DISTRIBUYE los avisos
# entre procesos; los mensajes se guardan en PostgreSQL (`ComentarioTicket`). Las pruebas usan la capa en memoria
# para no depender de un Redis compartido.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [env("CHANNELS_REDIS_URL", default=REDIS_URL)]},
    }
}
if len(sys.argv) > 1 and sys.argv[1] == "test":
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

AUTH_USER_MODEL = "core.Usuario"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "core:login"
LOGIN_REDIRECT_URL = "core:inicio"
LOGOUT_REDIRECT_URL = "core:login"

# 2.C — el shell renderiza `messages` con clases `alert--<tag>`; se remapea
# ERROR a "danger" para reutilizar los tokens/clases ya existentes
# (--danger, .alert--danger, .badge--danger) en vez de introducir "error"
# como una excepción de nomenclatura aislada.
from django.contrib.messages import constants as message_constants  # noqa: E402

MESSAGE_TAGS = {
    message_constants.ERROR: "danger",
}

LANGUAGE_CODE = "es"
# Zona horaria OPERATIVA de la organización (4.G1): «el día 25» de un Proceso programado y toda
# fecha local se interpretan con ella (`timezone.localdate()`), nunca con un desfase UTC escrito a
# mano. Se sigue configurando por entorno (`.env` ya la fija); el valor por defecto es el de la
# organización para que un entorno sin la variable no programe en UTC. Los timestamps se
# almacenan en UTC (`USE_TZ`) como siempre.
TIME_ZONE = env("TIME_ZONE", default="America/Bogota")
CELERY_TIMEZONE = TIME_ZONE
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        # CompressedManifestStaticFilesStorage exige un manifiesto generado
        # por `collectstatic` — no aplica a `runserver`/pruebas en DEBUG
        # (nadie corre `collectstatic` en ese flujo). Mismo patrón ya usado
        # para django_extensions/debug_toolbar: una sola condición sobre
        # DEBUG en el único settings.py, sin separar development/production.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Google: credenciales independientes para identidad y correo.
# Gmail permanece pendiente; el acceso Google usa únicamente los scopes de identidad.
APP_BASE_URL = env("APP_BASE_URL", default="http://127.0.0.1:8000").rstrip("/")
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
GOOGLE_OAUTH_REDIRECT_URI = env(
    "GOOGLE_OAUTH_REDIRECT_URI", default=f"{APP_BASE_URL}/auth/google/callback/"
)
GOOGLE_OAUTH_ALLOWED_DOMAINS = env.list(
    "GOOGLE_OAUTH_ALLOWED_DOMAINS", default=["edilandina.com", "gammaingenieros.com"]
)
GOOGLE_OAUTH_SCOPES = ("openid", "email", "profile")

GMAIL_SERVICE_ACCOUNT_EMAIL = env("GMAIL_SERVICE_ACCOUNT_EMAIL", default="")
GMAIL_SERVICE_ACCOUNT_CLIENT_ID = env("GMAIL_SERVICE_ACCOUNT_CLIENT_ID", default="")
GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID = env("GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID", default="")
# El PEM se guarda en una sola línea del .env con saltos de línea literales \\n.
GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY = env(
    "GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY", default=""
).replace("\\n", "\n")
GMAIL_SENDER_EMAIL = env("GMAIL_SENDER_EMAIL", default="")
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)
# Notificaciones (Sprint 7, adelanto): el correo de los avisos solo sale con el interruptor encendido (apagado por
# defecto para que desarrollo y pruebas no escriban a personas reales) y, si se define, todo correo se redirige a un
# buzón de pruebas (el destinatario real queda registrado en `EnvioCorreo`).
NOTIFICACIONES_CORREO_ACTIVO = env.bool("NOTIFICACIONES_CORREO_ACTIVO", default=False)
CORREO_REDIRIGIR_A = env("CORREO_REDIRIGIR_A", default="")
GMAIL_AUTH_MODE = env("GMAIL_AUTH_MODE", default="service_account")
GMAIL_REFRESH_TOKEN = env("GMAIL_REFRESH_TOKEN", default="")
GMAIL_OAUTH_REDIRECT_URI = env(
    "GMAIL_OAUTH_REDIRECT_URI", default=f"{APP_BASE_URL}/auth/gmail/callback/"
)
