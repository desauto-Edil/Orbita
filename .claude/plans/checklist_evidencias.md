# Checklist de evidencias, evidencia del equipo y seguimiento tras la gaceta

Estado: IMPLEMENTADO (2026-11-10), sin commit; migraciones y pruebas las corre el usuario. Requerimiento nuevo, CU por asignar (extiende CU de
formularios RQF-040/041, tareas RQF-073/076 y gacetas Sprint 6).

Decisiones del usuario: cada punto del checklist se marca con su prueba (archivo O enlace; sin prueba no se marca);
cualquiera del equipo sube la evidencia sin «Tomar», y la actividad tiene UN responsable que responde (recibe los
avisos de vencimiento). El proceso «Desarrollo» NO se toca.

## 1. Tipo de campo «Lista de evidencias» (CHECKLIST)

- `Campo.TipoCampo.CHECKLIST` («Lista de evidencias»), en el formulario de inicio y en las columnas de colecciones.
  Los puntos son `OpcionCampo` (mismo editor que LISTA/MULTILISTA, versionados con el campo).
- `EstrategiaChecklist` en `apps/catalogo/campos.py`: widget `checklist`; config `extensiones_permitidas`,
  `tamano_maximo_mb` (para las pruebas en archivo); exige al menos un punto al publicar.
- Almacenamiento: modelo nuevo `tickets.MarcaEvidencia` (ticket, elemento opcional, campo, punto = valor de la
  opción, enlace o archivo —exactamente uno, CheckConstraint—, nombre/tamaño/tipo, marcada_por/en; retiro LÓGICO
  retirada_por/en; nunca se borra). No pasa por `ValorElemento`/`RespuestaCampo` (como ARCHIVO).
- Completo = todos los puntos con una marca vigente. Lo usan `parrilla.completitud_de` (obligatoria/exigida) y la
  validación del formulario de inicio al radicar o completar datos de inicio.
- Operaciones `apps/tickets/evidencias.py`: `marcar_punto(…, archivo|enlace)` y `desmarcar_punto`, bajo los locks de
  siempre, auditadas, con la autorización de ESCRIBIR esa columna (ítem: `columnas_por_tarea` / `puede_editar_parrilla`
  / `puede_operar_coleccion`; inicio: quien diligencia el formulario). Descarga con la autorización de la ficha.
- **La evidencia ES el checklist**: si una actividad con `requiere_evidencia` es de un ítem cuya colección tiene una
  columna CHECKLIST, `tareas.operaciones.tiene_evidencia` la da por cumplida cuando ese checklist está completo y la
  actividad muestra el checklist (no «Adjuntar evidencia»). Sin columna CHECKLIST todo sigue como hoy.
- Studio › Formulario: el tipo aparece en el selector con su editor de puntos; previsualización.

## 2. Actividad del equipo: solo «Subir evidencia», un responsable que responde

- En el espacio, si la persona ya puede completar la actividad por ser del equipo, NO se ofrece «Tomar la actividad»
  (`espacio._accion_tarea`); sube la evidencia y completa directo. Tomar sigue existiendo para quien solo puede tomarla.
- ACTIVIDAD con `tipo_actor = EQUIPO` admite `responde_usuario_id` (Studio, opcional): se copia a
  `Tarea.responde_por` (migración en `tareas`). Los avisos y alertas de vencimiento de esa tarea van a esa persona en
  vez de a todo el equipo (`notificaciones.destinatarios.de_tarea`). No cambia quién puede actuar.

## 3. Tras la gaceta: una sola vista con el siguiente paso y las pruebas

- La galería de la gaceta (`espacio.galeria_gaceta`) sigue siendo tarjeta + visor para audiovisuales. Cada tarjeta
  aceptada suma: el paso siguiente del ítem (`recorrido_de_item`: p. ej. «Publicación · 15 nov»), sus fechas y, cuando
  existan, las pruebas (marcas del checklist: miniatura o enlace, quién y cuándo). Los interesados solo ven eso.
- El equipo, desde la misma tarjeta, abre la actividad del ítem y sube cada prueba en el visor.

## 4. Demo Imperandina (configuración, no código)

- Columna nueva CHECKLIST «Evidencia de publicación» (punto: «Publicada en el medio indicado»).
- «Publicación» pasa a ser del equipo Mercadeo BPO con `responde_usuario` = emm y `requiere_evidencia`; se retira el
  bloque aparte «Evidencia de publicación». Desarrollo de software no cambia.

## Migraciones previstas (las corre el usuario)

`catalogo` (choices de Campo), `tickets` (MarcaEvidencia), `tareas` (Tarea.responde_por), `core` (JefaturaUsuario).

## Orden de implementación

7 (Más) → 6 (retirar vistas) → 2 → 1 → 3 → 4 → 5. Cada bloque termina con los tests a correr.

## 5. Jefaturas y «Mi equipo»

- `core.JefaturaUsuario` (jefe, colaborador, vigente desde/hasta; una jefatura vigente por colaborador; nadie es su
  propio jefe ni se forman ciclos; retirar cierra la vigencia, nunca se borra; auditada).
- Configuración › Usuarios: al crear o editar a una persona, campo opcional «Jefe directo» (`usuarios.administrar`).
- Destino nuevo «Mi equipo» en el menú, visible SOLO para quien tiene colaboradores vigentes
  (`apps/core/navegacion.py`, por relación, no por rol).
- Vista general (`/mi-equipo/`): una fila por colaborador con tickets activos a su cargo, actividades y aprobaciones
  pendientes, vencidas/por vencer y un resumen de cada ticket (código, nombre, fase, siguiente acción, fecha). Los
  conteos salen de `bandeja.construir` / `trabajo.tickets_a_cargo` (sin reglas nuevas).
- Al elegir a una persona: su detalle (la misma bandeja de su Mi trabajo, en SOLO LECTURA, con el inspector).
- Permiso: el jefe puede CONSULTAR los tickets de sus colaboradores directos (solo lectura: espacio sin acciones,
  sin el canal Equipo de la conversación); no toma, reasigna ni decide. Solo jefatura directa (un nivel).

## 6. Retirar las vistas técnicas sueltas

Hay una sola vista de trabajo (el espacio del ticket); se retiran las PÁGINAS duplicadas, sus botones y plantillas:
- `tareas:lista`, `tareas:detalle`, `tareas:vista_previa` (`templates/tareas/lista.html`, `detalle.html`,
  `_vista_previa.html`), igual `aprobaciones:lista|detalle|vista_previa` y las pestañas técnicas
  `?tab=tareas|aprobaciones|revisiones` de Mi trabajo.
- Las URL de detalle quedan como REDIRECCIÓN al espacio del ticket para no romper enlaces de correos y notificaciones
  ya enviados; `bandeja`, `inicio`, `notificaciones.mensajes`, `espacio`, `trabajo.html`, `detalle.html` y
  `_recorridos.html` enlazan al espacio.
- Se CONSERVAN las operaciones POST (tomar, completar, verificar, comentar, evidencia, subtarea, decidir) que usa el
  espacio. Lo que hoy solo se hacía en el detalle de la tarea (asignar/reasignar/delegar una tarea y la persona o
  fecha de una subtarea) pasa al menú de la actividad en el espacio.
- No hay ruta para crear tareas sueltas, así que «Sin ticket asociado» desaparece de Mi trabajo.

## 7. Menú «Más» del espacio: Cancelar ticket y Reasignar

- «Más» ofrece «Cancelar ticket» (lo de hoy) y «Reasignar» (con `puede_reasignar`, ticket EN_ATENCION).
- Reasignar elige SOLO entre los miembros activos del equipo de trabajo del ticket (`equipo_responsable`; si el
  ticket no tiene equipo, los equipos activos del responsable actual). La regla vive en `operaciones.reasignar_ticket`
  (rechaza a quien no sea del equipo), no solo en el selector.

## Fuera de este incremento

Jefaturas de varios niveles (jefe del jefe).

## 8. Cancelación con aprobación, Reasignar claro y detalle dentro del espacio (2026-11-11)

Estado: IMPLEMENTADO (sin commit; migraciones `catalogo.0034`, `tickets.0035` y pruebas las corre el usuario).

Decisiones del usuario: «Cancelar ticket» lo siguen viendo el solicitante y el responsable (`puede_cancelar_ticket`),
pero la cancelación la ACEPTA un aprobador configurado en Studio (como la prórroga); mientras espera, el ticket SE
CONGELA (nadie avanza su trabajo). La vista `tickets:detalle` se retira: sus funciones pasan al espacio.

- Servicio: `cancelacion_con_aprobacion` + `cancelacion_aprobador_usuario|equipo` (Studio › Básico › Tiempos y
  prórrogas, `catalogo.operaciones.configurar_politica_cancelacion`, auditado); el Ticket lo CONGELA al nacer.
- `tickets.SolicitudCancelacion` (PENDIENTE → APROBADA | RECHAZADA | RETIRADA; una PENDIENTE por ticket; esquema de
  `apps.aprobaciones` con el aprobador congelado). `apps/tickets/cancelaciones.py`: `solicitar_cancelacion` (sin
  política → `cancelar_ticket` de siempre), `resolver_cancelacion_por_aprobacion` (aprobar = cancelar el ticket con
  `cancelar_ticket` por aprobación; rechazar = nada cambia), `retirar_cancelacion` (quien la pidió).
  `cancelar_ticket` rechaza cancelar directo un ticket que exige aprobación.
- CONGELADO = hay una solicitud PENDIENTE (`cancelaciones.exigir_sin_cancelacion_pendiente`): lo exigen las
  operaciones que avanzan el trabajo (tomar, iniciar, asignar, reasignar, resolver, entregar, solicitar información,
  prórroga, ítems y evidencias, datos de inicio, entregables, revisión por conjunto, completar actividades del
  flujo, decidir aprobaciones distintas de la cancelación y gacetas). La conversación sigue abierta. El espacio
  muestra el aviso y solo la decisión al aprobador.
- Reasignar: el menú dice «Reasignar el ticket», a quién lo tiene hoy y confirma a quién pasó (regla sin cambios).
- Detalle → espacio: `?vista=gestion` dentro del espacio (cabecera, recorrido y panel de siempre) con lo que solo
  vivía en el detalle: asignación, compromiso y prórrogas, entregables, entregas, resolución, solicitudes de
  información, archivos, datos de inicio, recorridos, gacetas y condiciones, y la gestión avanzada de colecciones.
  `tickets:detalle` redirige al espacio (enlaces ya enviados) y todas las vistas vuelven al espacio.
