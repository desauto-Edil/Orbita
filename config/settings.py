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
# `manage.py test`: las pruebas ajustan lo que solo sirve en el navegador (barra de depuración, canales, Google Chat).
EN_PRUEBAS = len(sys.argv) > 1 and sys.argv[1] == "test"
# La barra de depuración no se puede usar con las pruebas (Django las corre con DEBUG=False; su chequeo E001 lo impide).
BARRA_DEPURACION = DEBUG and not EN_PRUEBAS
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    # Servidor ASGI (Conversación del ticket en tiempo real). Va primero: así `runserver` sirve HTTP y WebSocket.
    "daphne",
    # Django Admin con el sitio protegido de Órbita (`apps.core.admin_sitio`): apagado salvo ADMIN_HABILITADA.
    "apps.core.admin_config.OrbitaAdminConfig",
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
    INSTALLED_APPS += ["django_extensions"]
if BARRA_DEPURACION:
    INSTALLED_APPS += ["debug_toolbar"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.presencia.PresenciaMiddleware",
    # Versión 1: la bienvenida de cinco pasos hasta que la persona la complete u omita (`apps.core.bienvenida`).
    "apps.core.bienvenida.BienvenidaMiddleware",
]

if BARRA_DEPURACION:
    MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
    INTERNAL_IPS = ["127.0.0.1"]
    # Apagada por defecto: en Docker la barra detecta la IP del host (192.168.65.1) y se activa sola, e instrumenta
    # con trazas cada consulta (varias veces más lento cada página). Se enciende con DEBUG_TOOLBAR=True en .env.
    DEBUG_TOOLBAR_VISIBLE = env.bool("DEBUG_TOOLBAR", default=False)
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": "config.debug.mostrar_barra"}

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
if EN_PRUEBAS:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

AUTH_USER_MODEL = "core.Usuario"

# Sin AUTH_PASSWORD_VALIDATORS: Órbita no tiene contraseñas (Versión 1, ingreso solo con Google).

LOGIN_URL = "core:login"
LOGIN_REDIRECT_URL = "core:inicio"
LOGOUT_REDIRECT_URL = "core:login"

# Versión 1: sin usuario ni contraseña. `CuentaOrbitaBackend` solo recupera a la persona de su sesión (la abre el
# ingreso con Google); el segundo da los permisos de modelo de la administración avanzada a quien tiene
# `plataforma.administracion_avanzada` (nunca eliminar).
AUTHENTICATION_BACKENDS = [
    "apps.core.backends.CuentaOrbitaBackend",
    "apps.core.admin_sitio.PermisosAdministracionAvanzada",
]

# Administración avanzada (etapa 4): APAGADA en el sitio público (toda ruta /admin/ responde 404). Solo la enciende
# el contenedor `admin`, publicado en 127.0.0.1 del servidor (túnel SSH). Exige un ingreso con Google de esta misma
# sesión de hace menos de ADMIN_REAUTENTICACION_MINUTOS.
ADMIN_HABILITADA = env.bool("ADMIN_HABILITADA", default=False)
ADMIN_REAUTENTICACION_MINUTOS = env.int("ADMIN_REAUTENTICACION_MINUTOS", default=30)

# Bienvenida (etapa 7): se muestra a quien aún no la completó. En las pruebas automáticas se apaga (las personas de
# prueba nacen sin completarla); las pruebas de la bienvenida la encienden con override_settings.
BIENVENIDA_OBLIGATORIA = env.bool("BIENVENIDA_OBLIGATORIA", default=True)
if EN_PRUEBAS:
    BIENVENIDA_OBLIGATORIA = False

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
# Conversación del ticket en Google Chat (plan `.claude/plans/conversacion_google_chat.md`; `apps/core/google_chat.py`):
# una cuenta de servicio con DELEGACIÓN DE DOMINIO (`GOOGLE_CHAT_SERVICE_ACCOUNT_*`, o la de Gmail si no se define) actúa como la cuenta de Órbita
# (`GOOGLE_CHAT_CUENTA`, crea y administra los espacios) y como el autor de cada mensaje. Apagado por defecto.
# `GOOGLE_CHAT_PERMITIDOS` (dev/staging): solo esos correos entran a los espacios. Pub/Sub es opcional: sin él, los
# mensajes escritos en Chat se recogen cada minuto.
GOOGLE_CHAT_ACTIVO = env.bool("GOOGLE_CHAT_ACTIVO", default=False)
# Cuenta de servicio PROPIA de Chat (con delegación de dominio); sin ella se usa la de Gmail.
GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL = env("GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL", default="") or GMAIL_SERVICE_ACCOUNT_EMAIL
GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY_ID = (
    env("GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY_ID", default="") or GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID
)
GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY = (
    env("GOOGLE_CHAT_SERVICE_ACCOUNT_PRIVATE_KEY", default="").replace("\\n", "\n") or GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY
)
GOOGLE_CHAT_CUENTA = env("GOOGLE_CHAT_CUENTA", default="")
GOOGLE_CHAT_PERMITIDOS = [c.strip().lower() for c in env.list("GOOGLE_CHAT_PERMITIDOS", default=[]) if c.strip()]
GOOGLE_CHAT_PUBSUB_TEMA = env("GOOGLE_CHAT_PUBSUB_TEMA", default="")
GOOGLE_CHAT_PUBSUB_SUSCRIPCION = env("GOOGLE_CHAT_PUBSUB_SUSCRIPCION", default="")
GOOGLE_CHAT_SCOPES = (
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.memberships",
    "https://www.googleapis.com/auth/chat.messages",
)
# Las pruebas nunca escriben a Google Chat aunque el `.env` lo active.
if EN_PRUEBAS:
    GOOGLE_CHAT_ACTIVO = False
GMAIL_AUTH_MODE = env("GMAIL_AUTH_MODE", default="service_account")
GMAIL_REFRESH_TOKEN = env("GMAIL_REFRESH_TOKEN", default="")
GMAIL_OAUTH_REDIRECT_URI = env(
    "GMAIL_OAUTH_REDIRECT_URI", default=f"{APP_BASE_URL}/auth/gmail/callback/"
)
