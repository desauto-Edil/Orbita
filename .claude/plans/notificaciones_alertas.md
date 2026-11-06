# Notificaciones y alertas — plan de trabajo

Estado: IMPLEMENTADO sin commit (2026-11-05; N.1–N.5). Decisiones D1–D4 tomadas con los valores por defecto.
Además (usuario): todos los correos llevan el logo (el del modo claro).
Estado anterior: PROPUESTO (2026-11-05). Adelanto autorizado del Sprint 7 («SLA, Domain Events, Notificaciones»),
como se hizo con los Procesos programados (4.G1). No se avanza de incremento sin autorización.
Después de este plan sigue el diseño visual.

## Trazabilidad CU (catálogo oficial, «Documentación conceptual-ORBITA.xlsx»)

| CU | Nombre | Requerimientos / reglas | Cobertura en este plan |
|---|---|---|---|
| CU-035 | Gestionar notificaciones del usuario | RQF-105 | Completa (N.1) |
| CU-036 | Emitir notificación por evento | RQF-104, RQF-106, RQF-108, RN-032 | Completa (N.1–N.3) |
| CU-037 | Configurar preferencias de notificación | RQF-107 | Completa (N.2) |
| CU-034 | Monitorear y ejecutar SLA | RQF-101, RQF-103 (alertas, vencimientos, escalamientos) | PARCIAL (N.4–N.5): sin pausas (RQF-102) |
| CU-033 | Gestionar política de SLA | RQF-099, RN-030 | PARCIAL: umbrales y recordatorios; sin calendario laboral ni pausas |
| — | RQF-125 (portal con notificaciones) | | Campana del header; actividad reciente en Inicio queda fuera |

## Decisiones ya tomadas (usuario)

1. Notificación = algo que YA pasó (evento de dominio). Alerta = situación de tiempo evaluada por el sistema;
   termina sola cuando la situación desaparece.
2. Catálogo base de eventos y alertas: el propuesto (solicitante, atención, tareas/aprobaciones, gacetas;
   próximo a vencer, vencido, sin tomar, recordatorios, procesos detenidos, fallo de programación).
3. Correo = RESUMEN MODERADO de la acción + UN enlace: una frase (quién hizo qué), 3–5 datos clave
   (ticket, servicio/proceso, etapa/actividad, fecha), extracto corto de la observación cuando hay que
   corregir. Nunca respuestas del formulario, adjuntos ni la conversación completa. Sin listas de rutas.
4. Configurable por Servicio / Proceso manual / Proceso programado: sección «Notificaciones» del Studio
   (qué etapa notificar, a quién, cómo y los recordatorios de pendientes).
5. Gmail ya está configurado en `.env` y probado (`apps/core/correo.py`, `GMAIL_AUTH_MODE`).
6. Frecuencia mensual de los procesos programados: correcta por ahora (fuera de este plan).

## Decisiones por confirmar (propuesta por defecto entre paréntesis)

- D1. Umbral «próximo a vencer»: (porcentaje del tiempo objetivo, 80 % por defecto; editable).
- D2. Escalamiento al vencer: (incremento propio N.5, después de validar N.4).
- D3. Resumen diario por correo: (fuera de V1; la agrupación por operación cubre los lotes).
- D4. Preferencias personales en Mi perfil: (sí, por grupo; lo que exige acción no se apaga).

## Reglas transversales

1. **La notificación nunca altera la operación** (RN-032): se publica el evento dentro de la transacción y se
   procesa con `transaction.on_commit`; si la operación se deshace, no hay aviso; si el aviso falla, la
   operación ya quedó hecha.
2. **Un solo punto de publicación por evento, sin duplicar reglas** (RQF-108): se reutilizan los puntos que ya
   registran historia (`tickets.historial.registrar`, `tareas.operaciones._registrar_historial`, decisiones
   de `apps.aprobaciones`, operaciones de `apps.gacetas`, entrada a fase/bloque en `workflows.motor`). Llamadas
   explícitas, sin señales de Django (mismo criterio de la auditoría).
3. **Destinatarios por RELACIÓN con el objeto, nunca por rol**, con las reglas `puede_*` existentes. Nunca a quien
   hizo la acción, nunca a usuarios inactivos, nunca al solicitante de un ticket programado (no lo tiene).
4. **Enlace solo si puede abrirlo**: si el destinatario (p. ej. una persona configurada) no puede consultar el
   objeto, la notificación informa sin enlace. Al abrir siempre se vuelve a autorizar.
5. **Configuración antes que código**: reglas por etapa con claves ESTABLES (`FaseWorkflow.clave`,
   `BloqueOperativo.clave`, `Campo.clave`); la configuración pertenece al Servicio/Proceso, nunca al Workflow
   compartido, y cada Ticket la CONGELA al nacer (como `Ticket.reglas_finalizacion`).
6. **Una alerta se emite una sola vez por umbral** (clave única) y un recordatorio respeta su máximo.
7. **Agrupar lo que llega en lote** (tareas por elemento, piezas de gaceta): un aviso por destinatario y operación.
8. **Correo seguro en desarrollo**: interruptor global `NOTIFICACIONES_CORREO_ACTIVO` (por entorno, apagado por
   defecto) y `CORREO_REDIRIGIR_A` (todo correo a un buzón de pruebas en dev/staging).
9. Sin datos ficticios; trazabilidad append-only; pruebas por incremento y suite completa al cerrar.

## Arquitectura

App nueva `apps.notificaciones` (dominio propio del Sprint 7; justifica app separada).

- `eventos.py` — Domain Events: `publicar(clave_evento, *, ticket, actor, objeto, datos)`; acumula por
  transacción y entrega al despachador en `on_commit`.
- `catalogo_eventos.py` — lista CERRADA de eventos: clave, grupo (Mis tickets · Mi trabajo · Gacetas · Sistema),
  destinatarios por defecto (relaciones), si exige acción (correo no apagable), plantilla del mensaje.
- `destinatarios.py` — resuelve relaciones (solicitante, responsable, equipo responsable, responsable de la
  actividad, aprobador en turno, revisores/coordinador de gaceta, persona/equipo específico, persona de un campo
  del formulario o de una columna del ítem).
- `mensajes.py` — compone título, frase, hechos (3–5) y extracto (≤ 280 caracteres, texto escapado).
- `despacho.py` — crea `Notificacion` por destinatario (agrupando) y encola correos según configuración congelada
  del Ticket + preferencias del usuario.
- `alertas.py` — evaluador periódico (Beat, cada hora, sembrado por migración como las demás tareas).
- Modelos:
  - `Notificacion` (destinatario, evento, grupo, título, resumen, hechos JSON, ticket opcional, enlace interno,
    actor nulo = Sistema, `clave_agrupacion`, creada_en, leida_en).
  - `EnvioCorreo` (outbox: notificación, destinatario, estado PENDIENTE/ENVIADO/FALLIDO/INCIERTO/OMITIDO,
    id de Gmail, intento, error resumido). Sin reintento ciego cuando el resultado es incierto (criterio de
    `correo.py`); se reintenta solo lo que falló antes de llegar a Google.
  - `Alerta` (tipo, objeto, umbral/ocurrencia, estado ACTIVA/RESUELTA, fechas) — historia de alertas,
    vencimientos y escalamientos (RQF-103).
  - `PreferenciaNotificacion` (usuario + grupo → correo sí/no).
  - `catalogo.ConfiguracionNotificaciones` (1:1 Servicio) + `catalogo.ReglaNotificacion` (cuándo · a quién ·
    canal · mensaje adicional) + política de recordatorios; congelada en `Ticket.notificaciones` (JSON).
  - Valores globales por defecto (umbral, horas sin tomar, recordatorios) en `ConfiguracionSistema` o fila única
    propia, editables en Configuración.
- `apps/core/correo.py` — se generaliza `enviar_prueba` en `enviar(destinatario, asunto, texto, html)`
  (multipart texto + HTML), reutilizando credenciales y diagnóstico existentes.

## Incrementos

### N.1 — Eventos y centro de notificaciones (CU-035, CU-036 in-app; RQF-104, 105, 108; RN-032)
- App, `Notificacion`, `eventos`/`despacho`/`destinatarios`/`mensajes` y catálogo base de eventos.
- Publicación desde los puntos de historia existentes (tickets, tareas, aprobaciones, gacetas, entregas,
  solicitud de información, prórrogas, procesos programados).
- Campana del header habilitada con contador de no leídas y popover con las últimas; página `/notificaciones/`
  (Todas / No leídas, filtro por grupo, marcar leída, marcar todas). Navegación desde `apps/core/navegacion.py`.
- Pruebas: no se notifica al actor; rollback = sin notificación; agrupación; programado sin solicitante;
  enlace solo si puede abrir.

### N.2 — Correo y preferencias (CU-036 correo, CU-037; RQF-106, 107)
- `correo.enviar` genérico, `EnvioCorreo` (outbox) y tarea Celery de envío; plantillas
  `templates/correo/notificacion.html|txt` con el formato acordado (asunto `TCK-000123 · <acción>`, frase,
  datos, botón «Ver en Órbita», pie «Recibes este correo porque…»).
- Interruptor global y redirección para dev/staging.
- Preferencias en Mi perfil por grupo (lo que exige acción no se apaga).
- Pruebas: interruptor apagado no envía; preferencia respetada; obligatorio no apagable; fallo de Gmail no
  afecta la operación; incierto no se reintenta.

### N.3 — Studio › Notificaciones (CU-036 «eventos configurados»)
- Sección nueva entre Flujo y Publicar (estado «Opcional» sin configurar), para Servicio y Proceso
  manual/programado:
  - A. Avisos base: por evento, Centro (siempre) / Correo (apagable salvo los que exigen acción).
  - B. Avisos por etapa: ¿Cuándo? (llega a una fase · actividad empieza · se completa · se devuelve/rechaza ·
    gaceta se abre/resuelve) · ¿A quién? · ¿Cómo? + mensaje adicional opcional.
  - C. Recordatorios (configuración; la ejecución llega en N.4).
- Programado: se ocultan las opciones del solicitante.
- Congelado en el Ticket al nacer; eventos de llegada a fase/bloque publicados por el motor.
- Publicar: una regla que apunta a una fase/actividad que ya no existe = «Requiere atención» (reconciliación
  por clave estable).
- Auditoría de cada cambio (`auditoria.auditar_guardado`).

### N.4 — Alertas y recordatorios (CU-034 parcial, CU-033 parcial; RQF-101, 103)
- Evaluador horario: ticket próximo a vencer / vencido (fecha objetivo vigente, `tickets.tiempos`), ticket sin
  tomar X horas, tarea próxima a vencer / vencida, aprobación pendiente hace X días, información sin respuesta,
  entrega por aceptar / aceptación automática próxima, plazos de gaceta, proceso detenido (espera de
  corrección), fallo de generación programada.
- Recordatorios con primer aviso, repetición y máximo; la alerta se resuelve sola al desaparecer la situación.
- Valores globales en Configuración; el Servicio/Proceso los puede cambiar; una actividad puede tener
  recordatorio propio en «Opciones avanzadas».
- Marca visible de «próximo a vencer» / «vencido» en Mi trabajo, Cola y Mis tickets (lectura de `Alerta`).

### N.5 — Escalamiento (CU-034; RQF-103) — sujeto a D2
- Al vencer (o tras N recordatorios sin atención): avisar a quien supervisa (alcance de atención existente),
  a una persona o a un equipo configurado. Solo notifica; la reasignación sigue siendo manual.

## Fuera de este plan

Calendario laboral configurable (festivos, horarios), pausas y reanudaciones del SLA (RQF-102, RN-031),
política SLA formal y métricas de cumplimiento (Analítica, Sprint 10), resumen diario (D3), actividad reciente
en Inicio (RQF-125 completo), canales distintos de Órbita y correo.

## Riesgos

- Límites de envío de Gmail Workspace → agrupación por operación, interruptor global, outbox con estado.
- Ruido de avisos → catálogo cerrado, sin aviso al actor, preferencias, una alerta por umbral.
- Coherencia con versiones → claves estables y configuración congelada en el Ticket.
