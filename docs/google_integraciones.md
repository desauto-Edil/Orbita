# Preparación de Google y Gmail

Estado: flujo de acceso Google y fotografía implementado, pendiente de instalar las
dependencias, aplicar la migración y probar con una cuenta real. El acceso con contraseña
sigue funcionando. El envío de notificaciones Gmail permanece pendiente.

## Credenciales en `.env`

No guardar los JSON en el repositorio. Copiar sus campos al `.env` local, que ya está
excluido de Git. `.env.example` contiene únicamente campos vacíos y valores no secretos.
Docker Compose ya entrega el `.env` a web, worker y beat. Recrear esos contenedores
después de modificar valores; un simple reinicio puede conservar las variables anteriores.

### Acceso: cliente OAuth de tipo Aplicación web

| Variable | Campo del JSON / valor |
| --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | `web.client_id` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `web.client_secret` |
| `GOOGLE_OAUTH_REDIRECT_URI` | `http://127.0.0.1:8000/auth/google/callback/` |
| `GOOGLE_OAUTH_ALLOWED_DOMAINS` | `edilandina.com,gammaingenieros.com` |
| `APP_BASE_URL` | `http://127.0.0.1:8000` |

Registrar la URI de retorno exacta en GCP. La ruta ya está implementada. Configurar audiencia y consentimiento en Google
para ambos dominios. Si pertenecen a organizaciones Workspace diferentes, confirmar
que la audiencia elegida permite a ambas ingresar; el listado local no configura GCP.

Reglas de la integración: validar tokens, emisor, audiencia, expiración, state,
nonce, correo verificado y dominio Workspace (`hd`). Permitir únicamente usuarios
locales activos y previamente registrados; rechazar correos ambiguos. Vincular de forma
controlada con `sub`, conservar roles locales y actualizar foto con respaldo de iniciales.
Los scopes de identidad son `openid email profile`; no solicitar Gmail a los empleados.

### Correo: cuenta de servicio con delegación Workspace

El JSON OAuth web no contiene la clave privada de la cuenta de servicio. Se necesitan
sus credenciales independientes y la autorización del administrador de Workspace.

| Variable | Campo del JSON / valor |
| --- | --- |
| `GMAIL_SERVICE_ACCOUNT_EMAIL` | `client_email` |
| `GMAIL_SERVICE_ACCOUNT_CLIENT_ID` | `client_id` numérico |
| `GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY_ID` | `private_key_id` |
| `GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY` | `private_key` |
| `GMAIL_SENDER_EMAIL` | `desarrollador.automatizacion@edilandina.com` |

La clave privada debe estar en una sola línea del `.env`, entre comillas simples,
manteniendo los caracteres literales `\n`. Settings los convierte a saltos de línea:

```dotenv
GMAIL_SERVICE_ACCOUNT_PRIVATE_KEY='-----BEGIN PRIVATE KEY-----\nCONTENIDO\n-----END PRIVATE KEY-----\n'
```

Habilitar delegación de dominio para la cuenta de servicio y autorizar en la consola
de administración de Workspace su `client_id` con el scope
`https://www.googleapis.com/auth/gmail.send`. El remitente debe ser un buzón Gmail
Workspace real y habilitado. La dirección `iam.gserviceaccount.com` no es el buzón.

## Siguiente implementación

- Completar la prueba real del cliente Gmail con el buzón configurado.
- Notificaciones de radicación, asignación, aprobación y cierre a los interesados del
  equipo establecido. Resolver destinatarios desde las relaciones reales del ticket;
  no mantener una lista fija de personas en `.env`.
- Despacho tras confirmar la transacción, procesamiento con Celery, resultados y reintentos
  con control de duplicados. El backend actual permanece en consola hasta integrar Gmail.

Referencias: [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect),
[OAuth web](https://developers.google.com/identity/protocols/oauth2/web-server),
[cuentas de servicio y delegación](https://developers.google.com/identity/protocols/oauth2/service-account),
[envío Gmail](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send).


## Activación y validación del acceso Google

1. Completar `GOOGLE_OAUTH_CLIENT_ID` y `GOOGLE_OAUTH_CLIENT_SECRET` en `.env`.
2. Registrar callback y configurar la audiencia de ambos dominios en GCP.
3. Reconstruir y recrear servicios, luego aplicar migraciones y correr pruebas:

```sh
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py test apps.core.tests_google apps.core.tests.IdentidadTests apps.core.tests.PerfilTests
```

4. Entrar por `http://127.0.0.1:8000/login/`. El botón se habilita cuando ambas
   credenciales están presentes. Iniciar con una cuenta registrada y activa, con correo
   coincidente y sin duplicados. La primera autenticación vincula su identificador `sub`;
   siguientes ingresos exigen la misma identidad. No se crean usuarios ni se cambian roles.
5. Comprobar foto en cabecera y perfil, cierre de sesión, ingreso por contraseña y rechazo
   de cuentas ajenas/no registradas/inactivas. Si falta la foto o no carga, quedan las iniciales.

Los tokens no se almacenan. Las pruebas automatizadas simulan el proveedor y verifican
las reglas locales; la validación real de firma, state y nonce corresponde a Authlib y
requiere además la prueba de integración con Google. No se ha realizado esa prueba real.

## Prueba de Gmail con cuenta de servicio

Está implementado el cliente `apps.core.correo` y el comando `probar_gmail`.
El backend general permanece en consola y todavía no se generan correos por eventos
de tickets. Las credenciales y los tokens no se imprimen ni se guardan en archivos.

Después de actualizar `.env`, reconstruir y recrear web para cargar dependencias y variables:

```sh
docker compose build web
docker compose up -d --no-deps --force-recreate web
```

Validar clave y delegación sin enviar:

```sh
docker compose exec web python manage.py probar_gmail
```

Solo con un destinatario autorizado, enviar una única prueba:

```sh
docker compose exec web python manage.py probar_gmail --destinatario desarrollador.automatizacion@edilandina.com
```

La respuesta satisfactoria confirma que Gmail aceptó el mensaje, no garantiza la
recepción en la bandeja de entrada. Revisar el buzón y spam. Un timeout no se reintenta
automáticamente: comprobar Enviados antes de repetir.

Pruebas locales con Google simulado, sin envío ni base de datos:

```sh
docker compose exec web python manage.py test apps.core.tests_correo --noinput
```

Si falla el formato PEM, copiar el campo `private_key` completo del JSON de la cuenta
de servicio. El secreto OAuth web, el ID del cliente y `private_key_id` no sustituyen esa
clave. Si falla `unauthorized_client`, autorizar en Workspace el ID numérico de la cuenta
de servicio con `https://www.googleapis.com/auth/gmail.send`.

## Alternativa local: OAuth del buzón sin delegación

1. En el mismo cliente OAuth web de GCP, conservar el callback de login y agregar
   `http://127.0.0.1:8000/auth/gmail/callback/` a las URI de redireccionamiento autorizadas.
2. En Google Auth Platform, configurar el permiso `https://www.googleapis.com/auth/gmail.send`
   en Acceso a los datos. Si la aplicación es externa en pruebas, agregar el remitente a
   sus usuarios de prueba. Las políticas del administrador Workspace pueden exigir su aprobación.
3. Entrar a Órbita como superusuario y abrir `http://127.0.0.1:8000/auth/gmail/`.
4. Pulsar Autorizar y seleccionar exactamente `GMAIL_SENDER_EMAIL`. Google solicita el
   permiso de envío, junto con identidad/correo para verificar quién está autorizando.
   No se solicitan permisos para leer o borrar mensajes, ni se envía correo en este paso.
5. El callback valida la identidad y el permiso, guarda `GMAIL_REFRESH_TOKEN` en `.env`
   y cambia `GMAIL_AUTH_MODE=oauth`. No imprime el token ni modifica las claves del login
   o de la cuenta de servicio. Después de conectar, recrear web y los workers existentes
   para que todos los procesos reciban la nueva configuración; no basta con reiniciar
   contenedores que ya recibieron variables anteriores mediante `env_file`.
6. Ejecutar `probar_gmail` y luego la prueba con destinatario autorizado indicada arriba.

Esta autorización es independiente del login; los demás usuarios no conceden permisos Gmail.
En proyectos externos en estado Prueba, los tokens de renovación con permisos Gmail suelen
caducar a los siete días; volver a autorizar cuando corresponda. Una cuenta con Gmail activo
puede autorizar OAuth aunque la delegación de la cuenta de servicio no funcione.

Referencia: [autorización Gmail del lado servidor](https://developers.google.com/workspace/gmail/api/auth/web-server)
y [caducidad de tokens](https://developers.google.com/identity/protocols/oauth2#expiration).
