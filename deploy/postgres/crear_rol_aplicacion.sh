#!/bin/sh
# Se ejecuta UNA vez, al inicializar un volumen de PostgreSQL vacío (docker-entrypoint-initdb.d).
# Crea el rol de la aplicación SIN superusuario y le entrega la base de datos: Órbita migra y opera con él; el
# superusuario (POSTGRES_USER) queda solo para mantenimiento. Los valores llegan como variables de psql (nunca se
# pegan dentro del SQL), así una contraseña con caracteres especiales no rompe ni inyecta nada.
set -eu

psql -v ON_ERROR_STOP=1 \
     -v app_user="$ORBITA_DB_USER" \
     -v app_pass="$ORBITA_DB_PASSWORD" \
     -v base="$POSTGRES_DB" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER DATABASE :"base" OWNER TO :"app_user";
ALTER SCHEMA public OWNER TO :"app_user";
REVOKE ALL ON DATABASE :"base" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"base" TO :"app_user";
EOSQL
