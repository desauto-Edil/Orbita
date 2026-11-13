#!/bin/sh
# Respaldo de Órbita en el servidor (Versión 1, etapa 8): base de datos + archivos subidos.
#   ./deploy/respaldar.sh            (desde la carpeta del proyecto en el VPS)
# Programarlo a diario con cron, p. ej.:   15 2 * * *  cd /opt/orbita && ./deploy/respaldar.sh >> respaldos/respaldo.log 2>&1
# Deja los archivos en ./respaldos (permisos 600) y conserva los últimos DIAS_RETENCION días. COPIA los respaldos FUERA
# del servidor (otro proveedor o almacenamiento cifrado): un respaldo que vive solo en el VPS no sirve si se pierde el
# VPS. Y prueba la restauración de vez en cuando (ver deploy/DESPLIEGUE.md).
set -eu

COMPOSE="docker compose -f docker-compose.prod.yml"
DESTINO="${DESTINO:-./respaldos}"
DIAS_RETENCION="${DIAS_RETENCION:-14}"
FECHA="$(date +%Y%m%d-%H%M%S)"

umask 077
mkdir -p "$DESTINO"

# Base de datos: formato personalizado (pg_restore), hecho por el superusuario de mantenimiento.
$COMPOSE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$DESTINO/orbita-db-$FECHA.dump"

# Archivos subidos (adjuntos, evidencias, piezas, logos).
$COMPOSE exec -T web tar -C /app -czf - media > "$DESTINO/orbita-media-$FECHA.tar.gz"

# Retención: solo los respaldos de Órbita de esta carpeta y más antiguos que DIAS_RETENCION.
find "$DESTINO" -maxdepth 1 -type f \( -name 'orbita-db-*.dump' -o -name 'orbita-media-*.tar.gz' \) \
     -mtime +"$DIAS_RETENCION" -exec rm -f {} \;

echo "$(date -Iseconds) respaldo listo: $DESTINO/orbita-db-$FECHA.dump y orbita-media-$FECHA.tar.gz"
