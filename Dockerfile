# Órbita — imagen en etapas (Versión 1, etapa 8).
#   desarrollo  → `docker-compose.yml` (runserver, código montado como volumen).
#   produccion  → `docker-compose.prod.yml` (por defecto): sin compiladores, usuario sin privilegios, estáticos ya
#                 recopilados y servidor ASGI Daphne (HTTP + WebSocket de la Conversación).

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

# --- Dependencias: se compilan aquí y solo se copia el entorno virtual a las etapas finales ---
FROM base AS dependencias
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Desarrollo ---
FROM base AS desarrollo
COPY --from=dependencias /opt/venv /opt/venv
COPY . .
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# --- Producción (etapa final: la que se construye si no se indica otra) ---
FROM base AS produccion
RUN groupadd --system --gid 10001 orbita \
    && useradd --system --uid 10001 --gid orbita --home-dir /app --shell /usr/sbin/nologin orbita
COPY --from=dependencias /opt/venv /opt/venv
COPY --chown=orbita:orbita . .
# Los estáticos se recopilan al construir (WhiteNoise los sirve con su manifiesto). Los valores de este paso son
# solo para poder cargar la configuración: no quedan en la imagen ni conectan a nada.
RUN SECRET_KEY=solo-para-collectstatic DATABASE_URL=sqlite:////tmp/collectstatic.sqlite3 DEBUG=False \
    python manage.py collectstatic --noinput \
    && rm -f /tmp/collectstatic.sqlite3 \
    && mkdir -p /app/media \
    && chown -R orbita:orbita /app/media /app/staticfiles
USER orbita
EXPOSE 8000
CMD ["daphne", "--bind", "0.0.0.0", "--port", "8000", "--proxy-headers", "config.asgi:application"]
