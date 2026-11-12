# Conversación del ticket en Google Chat (delegación de dominio) — plan de trabajo

Estado: APROBADO (2026-11-09; decisiones D1–D4 tomadas), IMPLEMENTADO G.0–G.4 sin commit (2026-11-09); falta la configuración en Google y la prueba real. Requerimiento NUEVO del usuario, sin CU en el catálogo oficial: amplía la
Conversación del ticket (CU-018 / RQF-057). **ID de CU por asignar** (lo define el equipo). No se avanza de
incremento sin autorización.

Antecedente (2026-11-09): se implementó y se RETIRÓ por completo la variante «app de Chat con autenticación de app»
(avisos por mensaje directo; exigía que cada persona agregara la app). Se cambia a DELEGACIÓN DE DOMINIO: Órbita
crea los espacios y nadie instala nada. Las NOTIFICACIONES (campana, correo, alertas, avisos por etapa) NO cambian:
Google Chat se usa SOLO para la conversación (crear los espacios y llevar en ellos la conversación del ticket).

## Idea

Cada canal de la Conversación de un ticket (EQUIPO e INTERESADOS, `ComentarioTicket.Canal`) puede tener SU espacio
de Google Chat, creado por Órbita, con las personas que pertenecen a ese canal. Lo que se escribe en Órbita aparece
en el espacio y lo que se escribe en el espacio aparece en Órbita. **PostgreSQL sigue siendo la fuente de verdad**
(`ComentarioTicket`, append-only): Chat es otra superficie del mismo canal, nunca otra autoridad de permisos.

## Checklist del proyecto (CLAUDE.md, pasos 1–10)

1. **CU**: nuevo, ID por asignar; extiende CU-018 (Conversación) y respeta CU-016 (Historial).
2. **Actores**: quienes pertenecen a cada canal (`conversacion.canales_de` / `destinatarios`); la cuenta remitente de
   Órbita (cuenta de Workspace que crea y administra los espacios); el administrador de Workspace (autoriza la
   delegación, fuera de Órbita).
3. **Precondiciones**: Google Workspace del dominio; API de Chat CONFIGURADA en el proyecto de Cloud (nombre,
   avatar, visibilidad: obligatorio para cualquier llamada, es un ajuste único del proyecto, no una instalación por
   persona); cuenta de servicio con delegación de dominio (ya existe para Gmail) con los scopes de Chat autorizados;
   `Usuario.email` = su cuenta de Workspace.
4. **Reglas de negocio**:
   - R1. La pertenencia al espacio la decide Órbita con la MISMA regla del canal (`conversacion.destinatarios`:
     quienes intervienen directamente). La supervisión solo por alcance sigue leyendo en Órbita, no entra al espacio.
   - R2. Aislamiento: el espacio del canal EQUIPO nunca tiene a quien solo es interesado (revisor de una gaceta,
     solicitante). Si la sincronización falla, se reintenta; nunca se agrega a alguien «por si acaso».
   - R3. Quien pierde la relación con el ticket sale del espacio en la siguiente sincronización.
   - R4. Escribir: en Órbita, `puede_escribir` como hoy; desde Chat, el mensaje entra a Órbita solo si su autor
     (por correo) puede escribir en ese canal en ese momento; si no, no se importa y queda registrado el rechazo.
   - R5. Append-only: ediciones y borrados hechos en Chat no cambian Órbita (se conserva lo recibido). Adjuntos de
     Chat: se registra el texto con la marca «[adjunto en Google Chat]». Texto > 2000 caracteres: se recorta con
     marca (la regla de `conversacion.validar_texto` no cambia).
   - R6. Idempotencia: cada `ComentarioTicket` sale con un `messageId` propio (`client-orbita-c<id>`); cada mensaje
     de Chat entra una sola vez (único por su `name` de Chat). Sin ecos: lo que Órbita publicó no se reimporta.
   - R7. Nunca altera la operación (RN-032): todo hacia Google ocurre tras el commit (Celery), con bandeja de salida
     y reintentos; si Google no responde, la conversación en Órbita funciona igual.
   - R8. Interruptor por entorno `GOOGLE_CHAT_ACTIVO` (apagado por defecto) y, en dev/staging, una lista cerrada
     de correos permitidos (nadie fuera de ella entra a un espacio de pruebas).
5. **Modelos** (una migración aditiva):
   - `tickets.EspacioChatTicket`: ticket + canal (único), `name` del espacio, `uri`, estado (CREANDO / ACTIVO /
     SOLO_LECTURA / ERROR), última sincronización de miembros, error.
   - `tickets.MensajeChat`: vínculo `ComentarioTicket` ↔ mensaje de Chat (`name` único), dirección (SALIDA /
     ENTRADA), estado de envío (PENDIENTE / ENVIADO / FALLIDO), intentos, error. Es la bandeja de salida y el
     registro anti-eco.
   - `ComentarioTicket` gana `origen` (ORBITA / GOOGLE_CHAT); nada se borra.
6. **Permisos/alcances**: ninguno nuevo; todo sale de `conversacion.canales_de` / `puede_escribir`.
7. **Eventos/auditoría**: crear el espacio, cambiar sus miembros y pasarlo a solo lectura se auditan
   (`RegistroAuditoria`, actor Sistema). Un mensaje entrante publica el mismo `TICKET:COMENTARIO` (canal incluido)
   que uno escrito en Órbita, así campana y WebSocket siguen igual.
8. **Reutilizable**: `conversacion` (canales, destinatarios, validación, difusión por WebSocket),
   `operaciones.registrar_comentario`, credenciales con delegación de `apps/core/correo.py` (mismo patrón
   `subject=`), `google-auth[requests]` (sin dependencia nueva: Chat, Workspace Events y Pub/Sub por REST),
   Celery y Beat.
9. **Patrones**: bandeja de salida (como `EnvioCorreo`) y reconciliador periódico (como los de Beat existentes).
10. **Fuera**: notificaciones por Chat (siguen campana y correo), adjuntos reales en ambos sentidos, reacciones,
    hilos, edición/borrado sincronizados, espacios para quien no es del dominio, lectura de Chat → «leído» en Órbita.

## Cómo se autentica (delegación de dominio)

- Una cuenta de servicio con delegación (la de Gmail u otra) y scopes autorizados por el administrador:
  `chat.spaces` (crear y configurar espacios), `chat.memberships` (miembros), `chat.messages` (escribir y leer).
  Para recibir en tiempo real se suma Workspace Events (ver «Entrada»).
- Órbita actúa como la CUENTA REMITENTE (`GOOGLE_CHAT_CUENTA`, p. ej. una cuenta de avisos) para crear espacios y
  administrar miembros: es la administradora de cada espacio.
- Los mensajes escritos en Órbita se publican COMO SU AUTOR (la delegación lo permite: `subject=<correo del
  autor>`), así en Chat se ve quién escribió. Ver decisión D2.
- Mensajes con credenciales de usuario: SOLO TEXTO (sin tarjetas); el primer mensaje del espacio lleva el enlace al
  ticket en Órbita.

## Entrada (Chat → Órbita)

- Workspace Events API: una suscripción por espacio a `google.workspace.chat.message.v1.created`, entregada a un
  tema de Pub/Sub; Órbita la consume con una suscripción PULL desde Celery (no requiere URL pública). Las
  suscripciones vencen: el reconciliador las renueva.
- Respaldo: el reconciliador lista los mensajes recientes de cada espacio activo (`spaces.messages.list` desde el
  último importado) y recupera lo que el flujo de eventos no haya entregado. Solo con ese respaldo ya funciona
  (con retraso de minutos); Pub/Sub da el tiempo real.

## Incrementos

- **G.0 — Configuración y prueba** (sin modelos): settings (`GOOGLE_CHAT_ACTIVO`, cuenta remitente, scopes, lista
  permitida en pruebas), cliente `apps/core/google_chat.py` con delegación, comando `probar_google_chat` que crea un
  espacio de prueba con dos personas, publica como una de ellas, lee el mensaje y lo deja en solo lectura.
  Criterio: funciona contra el Workspace real sin que nadie instale nada.
- **G.1 — Espacios y miembros**: `EspacioChatTicket`; crear el espacio de un canal (D3) con sus miembros;
  sincronización de miembros (R1–R3) tras los eventos que cambian la relación con el ticket (tomar, asignar,
  tareas, aprobaciones, gacetas) y en un barrido periódico; enlace «Abrir en Google Chat» en el panel de la
  Conversación.
- **G.2 — Salida**: `MensajeChat`; cada mensaje escrito en Órbita se publica en su espacio (como su autor),
  idempotente y con reintentos.
- **G.3 — Entrada**: importar lo escrito en Chat (R4–R6), por eventos (Pub/Sub pull) y por el barrido de respaldo;
  difusión por WebSocket y aviso en la campana como cualquier mensaje.
- **G.4 — Cierre**: al resolver, cerrar o cancelar el ticket, mensaje final y espacio en solo lectura (D4); al
  reabrir, vuelve a permitir escribir.

## Riesgos a vigilar

- Dos dominios en el login (`edilandina.com`, `gammaingenieros.com`): si son Workspaces distintos, mezclar personas
  en un espacio exige permitir espacios externos; si no, solo entran las del dominio de la cuenta remitente.
- Cuotas de la API (escrituras por espacio y lecturas por proyecto): el barrido se limita a espacios activos.
- La delegación con `chat.messages` permite a Órbita escribir como cualquier persona del dominio: la credencial se
  guarda como la de Gmail y solo la usa el cliente de Chat.
- Si la sincronización de miembros va atrasada, alguien puede leer en Chat algo de un ticket del que ya salió (R3):
  la sincronización corre tras cada evento relevante y no solo en el barrido.

## Decisiones (usuario, 2026-11-09)

- D1. ESPEJO: Órbita sigue guardando y mostrando todo; Chat es otra superficie del mismo canal.
- D2. Lo escrito en Órbita se publica en Chat COMO SU AUTOR (delegación de dominio).
- D3. El espacio de un canal se crea con el PRIMER MENSAJE de ese canal (no hay espacios vacíos).
- D4. Al cerrar o cancelar el ticket, el espacio queda en SOLO LECTURA con un mensaje final; al reabrir, se vuelve
  a permitir escribir.
