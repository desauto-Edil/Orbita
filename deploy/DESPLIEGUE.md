# Despliegue de Órbita en un VPS (Versión 1)

Servidor sin VPN interna (Hostinger). Solo Caddy queda expuesto a internet (80/443). PostgreSQL y Redis viven en una red
interna de Docker, sin puertos publicados y sin salida a internet; Redis exige contraseña y la aplicación usa un rol de
PostgreSQL **sin** superusuario. La administración avanzada solo se enciende a mano y se entra por túnel SSH.

Archivos: `Dockerfile` (etapa `produccion`), `docker-compose.prod.yml`, `deploy/caddy/Caddyfile`,
`deploy/postgres/crear_rol_aplicacion.sh`, `deploy/env.produccion.example`, `deploy/respaldar.sh`.

## 1. Preparar el servidor (una vez)

1. **Usuario de despliegue y SSH solo con llave**: crea un usuario (p. ej. `orbita`) con `sudo`, copia tu llave
   pública (`ssh-copy-id`) y en `/etc/ssh/sshd_config` deja `PasswordAuthentication no` y `PermitRootLogin no`;
   reinicia `sshd` **sin cerrar** la sesión actual hasta comprobar que entras con la llave.
2. **Firewall en dos capas**:
   - Panel de Hostinger (hPanel › VPS › Firewall): permite solo 22, 80 y 443 (TCP) y 443 (UDP).
   - En el servidor: `ufw default deny incoming`, `ufw allow 22/tcp`, `ufw allow 80/tcp`, `ufw allow 443`, `ufw enable`.
   - Ojo: Docker publica puertos SALTÁNDOSE UFW. Por eso `docker-compose.prod.yml` solo publica los de Caddy y el
     `admin` en `127.0.0.1`. Nunca agregues `ports:` a `db`, `redis` o `web`.
3. **Actualizaciones automáticas de seguridad**: `apt install unattended-upgrades fail2ban` y
   `dpkg-reconfigure unattended-upgrades`. `fail2ban` con la cárcel `sshd` por defecto.
4. **Docker** (Engine + plugin Compose) desde el repositorio oficial de Docker. Agrega el usuario al grupo `docker`.
5. **DNS**: un registro `A` (y `AAAA` si hay IPv6) del dominio de Órbita a la IP del VPS. Caddy pide el certificado
   solo cuando el dominio ya apunta al servidor.
6. **Snapshots** del VPS en Hostinger antes de cada actualización importante.

## 2. Primera instalación

```bash
git clone <repositorio> /opt/orbita && cd /opt/orbita
cp deploy/env.produccion.example .env && chmod 600 .env
# Completa .env: secretos con `openssl rand -hex 32`, dominio, cliente OAuth de producción y cuenta de servicio.
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d db redis
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml run --rm web python manage.py check --deploy
docker compose -f docker-compose.prod.yml run --rm web python manage.py otorgar_administrador --correo persona@edilandina.com --nombre "Nombre Apellido"
docker compose -f docker-compose.prod.yml up -d
```

`check --deploy` debe quedar sin advertencias (si avisa de `SECRET_KEY`, genera una más larga).
Comprueba en el navegador: `https://<dominio>` → ingreso con Google; `https://<dominio>/admin/` y `/salud/` → 404.

### Llevar los datos de desarrollo (opcional)

En el equipo de desarrollo: `docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' > orbita.dump`
y copia también la carpeta `media/`. En el servidor, con la base recién creada y ANTES de `migrate`:

```bash
docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --role="$ORBITA_DB_USER"' < orbita.dump
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
```

Los archivos van al volumen `media`: `docker compose -f docker-compose.prod.yml cp media/. web:/app/media/` y luego
`docker compose -f docker-compose.prod.yml exec -u root web chown -R orbita:orbita /app/media` (la aplicación no corre como root).
Revisa después en Configuración › Usuarios que solo queden activas las cuentas reales.

## 3. Administración avanzada (solo cuando hace falta)

```bash
docker compose -f docker-compose.prod.yml --profile admin up -d admin   # en el servidor
ssh -L 8001:127.0.0.1:8001 orbita@<servidor>                            # en tu equipo
```

Abre `http://127.0.0.1:8001/admin/`, ingresa con Google (registra `http://127.0.0.1:8001/auth/google/callback/` en el
cliente OAuth de producción) y al terminar: `docker compose -f docker-compose.prod.yml --profile admin stop admin`.

## 4. Respaldos

`./deploy/respaldar.sh` deja en `./respaldos` la base (`.dump`) y los archivos (`.tar.gz`) y conserva 14 días.
Prográmalo en `crontab -e`:

```
15 2 * * *  cd /opt/orbita && ./deploy/respaldar.sh >> respaldos/respaldo.log 2>&1
```

**Cópialos fuera del VPS** (otro proveedor o almacenamiento cifrado, p. ej. `rclone` con un remoto cifrado) y
**prueba la restauración** al menos una vez al trimestre en un servidor aparte:
`pg_restore -U "$POSTGRES_USER" -d <base_vacia> --no-owner --role=<rol_app> orbita-db-<fecha>.dump`.

## 5. Actualizar

```bash
./deploy/respaldar.sh
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml up -d
```

## 6. Vigilar

- `docker compose -f docker-compose.prod.yml ps`: `web`, `db` y `redis` deben estar `healthy`.
- Registros: `docker compose -f docker-compose.prod.yml logs -f --tail=200 web celery_worker celery_beat`
  (rotan solos: 5 archivos de 10 MB por servicio).
- Correos que no salieron: en la administración avanzada, Envíos de correo en estado FALLIDO o INCIERTO.
- Espacio en disco (`df -h`) y el tamaño de `respaldos/`.
- La política de contenido (CSP) del Caddyfile se EXIGE: si algo de la página dejara de cargar, la consola del
  navegador dice qué regla lo bloqueó. Nunca agregues `<script>` ni `onclick=` en las plantillas: el código va en
  `static/js/` (las confirmaciones usan `data-confirmar-formulario` / `data-confirmar-envio`).

## 7. Secretos

Antes de la primera puesta en producción, **rota** lo que vivió en equipos de desarrollo: `SECRET_KEY`, el secreto
del cliente OAuth (crea un cliente exclusivo de producción) y la llave de la cuenta de servicio (crea una nueva y
borra las viejas en GCP). El `.env` del servidor queda con `chmod 600` y nunca entra al repositorio ni a la imagen
(`.gitignore` y `.dockerignore` lo excluyen, igual que los respaldos `*.sql`/`*.dump`).
