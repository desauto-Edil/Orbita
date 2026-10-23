# Evolución del modelo configurable de Procesos — plan en cuatro fases

Estado: arquitectura y decisiones funcionales APROBADAS (propuesta «Evolución del
modelo configurable de procesos», §1–§12). Este plan reemplaza la secuencia F0–F8
de la propuesta por cuatro fases. No se avanza de fase sin autorización explícita.

Reglas transversales (todas las fases):

1. Nada específico por área ni por proceso: todo es configuración genérica.
2. Sin ejemplos, datos semilla de demostración ni registros ficticios.
3. Un solo formulario, un solo motor (`apps.workflows.motor`), un solo sistema de
   aprobaciones (`apps.aprobaciones`) y de tareas (`apps.tareas`).
4. Se conserva el versionamiento (la configuración se congela por referencia en
   `InstanciaWorkflow.configuracion_ejecucion_version`) y la trazabilidad
   (append-only + `RegistroAuditoria`).
5. Cada incremento declara sus CU-ID (ver «Trazabilidad CU»).
6. Pruebas por incremento y suite completa en serie al cerrar la fase.

## Decisiones aprobadas que gobiernan el plan

- Recorridos independientes por ítem = subinstancias (`InstanciaWorkflow` hija)
  ejecutadas por el MISMO motor (opción A). No hay segundo motor ni ticket por ítem.
- Varias colecciones por Proceso, empezando por la colección por defecto.
- `TAREAS_ELEMENTOS` y la revisión ELEMENTOS/`RevisionElemento` se conservan para lo
  existente y dejan de ofrecerse en configuraciones nuevas cuando exista el sustituto.
- TODOS conserva su semántica (rechazo/devolución cierra el esquema).
- Precedencia: RECHAZADA > EXCLUIDA > DEVUELTA > APROBADA.
- Roles como aprobadores: fuera de V1.
- Paralelismo general: GRUPO_ACTIVIDADES + actividades concurrentes en el bloque de ítems.
- Recorridos de ítem sin fases propias.
- Gacetas en una app nueva `apps.gacetas`.
- D3: un Flujo de la biblioteca es una PLANTILLA DE FASES; los bloques viven en la
  configuración de cada Servicio/Proceso (ya implementado en el código; las pruebas
  heredadas de D2 se alinean en la Fase 1).
- Recurrentes: el ticket se genera automáticamente y su diligenciamiento inicial
  ocurre DENTRO del workflow (Fase 2).

## FASE 1 — Estabilización y configuración

Objetivo: base estable y configuración ampliada, sin cambiar la ejecución.

1.1 Regresiones y suite
- Regresión B: `entregable_revisado` llegaba al camino LEGACY de Studio.
- Regresión A: pruebas D2 del Diseñador/Studio alineadas con D3 (fases).
- Bug D3: `duplicar_flujo` no conservaba el modo del flujo.
- Suite completa en serie en verde como línea base.

1.2 Actividades
- Configuración de ACTIVIDAD: descripción operativa, fecha prevista y fecha límite
  (relativas en días hábiles con `tickets/tiempos.py`), evidencia obligatoria y
  lista de verificación.
- `Tarea.fecha_prevista`; `tareas.ItemVerificacionTarea` (lista de verificación)
  exigida por `completar_tarea` en los ítems obligatorios.

1.3 Colecciones de ítems
- `DefinicionParrilla` 1:1 → N por Proceso con `clave` estable; `ParrillaTicket`
  1:1 → FK única por (ticket, colección). Compatibilidad: «colección por defecto»
  y todos los usos de `ticket.parrilla` canalizados por `parrilla.parrilla_de`.
- Tipos de columna: ARCHIVO (vía `Adjunto.elemento`) y EQUIPO.
- `VersionElemento` (foto de valores + archivos + huella) y control de alcance:
  `cerrar_alcance`, foto del alcance original, `DecisionAlcanceElemento`
  (EXCLUIDO/RECHAZADO con justificación), altas extraordinarias autorizadas y
  alcance efectivo calculado.

1.4 Compatibilidad
- Configuraciones, tickets y flujos existentes se leen y ejecutan sin cambios.

Fuera de la Fase 1: subinstancias, GRUPO_ACTIVIDADES, gacetas, finalización,
espacio de trabajo unificado.

Estado (2026-10-15): implementada, pendiente de aprobación para avanzar.
- Hecho: 1.1 (regresiones A y B + defectos D3 encontrados: copia de flujo sin modo,
  eliminar fase/conexión sin auditoría válida, vista previa de flujo en Studio),
  1.2, 1.3 (sin columna EQUIPO), 1.4.
- Pasa a la Fase 2: columna EQUIPO (va con los responsables por columna de los
  recorridos), fechas de actividad desde una columna o el formulario, cierre de alcance
  automático al llegar a un bloque, y sacar los ítems excluidos/rechazados del agregado
  de la revisión por conjunto y de las tareas por elemento (hoy solo salen del alcance
  efectivo).

## FASE 2 — Motor de ejecución y aprobaciones

- `InstanciaWorkflow.instancia_padre`, `RecorridoColeccion`, `BloqueOperativo.recorrido`
  (XOR con fase), bloque RECORRIDO_ITEMS (motivo ITEMS) y `continuar_por_items`.
- Contexto `item.*` / `items.*` en `ResolutorVariables` y actores por columna.
- GRUPO_ACTIVIDADES y actividades generales concurrentes.
- `CondicionConfigurada` reutilizable (salida de recorrido, ingreso, cierre).
- Aprobaciones: CUALQUIERA/TODOS en ítems y bloques, EXCLUIDA, decisiones permitidas,
  justificación al aprobar.
- Guardado progresivo del formulario de inicio existente (mismo formulario).
- Inicio manual o recurrente con el mismo motor; el recurrente se genera solo y su
  diligenciamiento inicial es un paso del workflow (sustituto del solicitante,
  receptor de entrega configurable).
- Orden de locks: tareas/aprobaciones → hija → padre → ticket → colección;
  cancelación en cascada.

Estado (2026-10-15): implementada, SIN EJECUTAR pruebas ni migraciones (las ejecuta el usuario). Pendiente
de validación funcional y aprobación para avanzar.
- Hecho:
  - Subinstancias (`InstanciaWorkflow.instancia_padre/ejecucion_padre/elemento/bloque_rama`) ejecutadas por el
    MISMO motor; bloques contenedores `GRUPO_ACTIVIDADES` y `RECORRIDO_ITEMS` (`BloqueOperativo.contenedor` +
    `rama` ITEM/GENERAL, en lugar del `recorrido` XOR fase del borrador del plan); orquestación en
    `apps/workflows/recorridos.py`; motivos de espera RECORRIDO y GRUPO; resultados publicados
    `recorridos.<clave>.*` / `grupos.<clave>.*`; `item.<clave>` en el resolutor.
  - Responsables desde columna (`COLUMNA`: persona o equipo del ítem); tipo de columna EQUIPO.
  - Aprobaciones: decisión EXCLUIDA (solo si el esquema lo permite: aprobación de un ítem), precedencia
    RECHAZADA > EXCLUIDA > DEVUELTA > APROBADA, justificación al aprobar. En un recorrido por ítem, rechazar o
    excluir saca al ítem del alcance (origen APROBACION) y termina SOLO su recorrido; devolver vuelve atrás
    dentro de su recorrido.
  - Condiciones: condición de ingreso por columna, incorporar ítems nuevos, esperar el cierre del alcance; las
    condiciones de salida se expresan con una DECISION sobre `recorridos.<clave>.*` (no se creó
    `CondicionConfigurada`).
  - Pendientes de la Fase 1: columna EQUIPO; fechas de actividad desde `formulario.<clave>` o `item.<clave>`;
    cierre automático del alcance al llegar a un bloque (`apps/workflows/efectos.py`); ítems excluidos o
    rechazados fuera del agregado de revisión y de las tareas por elemento (su tarea abierta se cancela);
    historial de versiones del ítem (`tickets:elemento_versiones`).
  - Datos de inicio en la ejecución (`Servicio/Ticket.datos_inicio_en_ejecucion`, `apps/tickets/datos_inicio.py`):
    guardar avance / completar diligenciamiento sobre el MISMO formulario; siempre en procesos programados
    (se levantó la restricción de campos obligatorios de 4.G1); opcional en procesos manuales; una actividad
    puede exigir los datos completos; `ticket.datos_inicio_completos` en decisiones.
  - Studio y espacio de trabajo: configuración de todo lo anterior sin JSON; secciones «Datos de inicio» y
    «Recorridos» (con reintento autorizado) en el detalle y el espacio de trabajo; el ítem en la aprobación.
- Migraciones nuevas (sin aplicar): `workflows/0015_subinstancias`, `catalogo/0027_bloques_contenedores`,
  `tickets/0025_datos_inicio_y_equipo`, `aprobaciones/0003_exclusion_y_justificacion` (escritas a mano).
- Decisiones tomadas sin consulta (documentadas): rechazo de un ítem = salida del alcance RECHAZADO; un
  contenedor sin ítems en el alcance no bloquea (salvo «esperar cierre del alcance»); un recorrido detenido
  por un dato faltante se reintenta de forma explícita o se excluye el ítem.
- Pendiente para fases siguientes: decisiones permitidas más finas que «excluir sí/no», `ReglaFinalizacion`,
  gacetas (Fase 3), sustitución de TAREAS_ELEMENTOS/revisión ELEMENTOS en configuraciones nuevas.

## FASE 3 — Gacetas y finalización

- `catalogo.DefinicionGaceta` (N por Proceso) + `apps.gacetas` (GacetaTicket,
  ParticipacionItemGaceta, ObservacionGaceta, EsquemaAprobacionGaceta).
- Bloque GACETA en el recorrido del ítem (ingreso progresivo, motivo GACETA).
- Observaciones, EN_CORRECCION con continuidad, versiones (`VersionElemento`).
- Reconciliador de plazos en Beat; aceptación automática solo sin pendientes.
- Aprobar / devolver / rechazar / excluir (justificación en rechazar y excluir);
  cambio de alcance sin segunda aprobación; resolución definitiva ≠ cierre de periodo.
- `ReglaFinalizacion`, verificador, `ResolucionTicket.resumen_resultados`.

Estado (2026-10-15): implementada, SIN EJECUTAR pruebas ni migraciones (las ejecuta el usuario).
- Hecho:
  - `catalogo.DefinicionGaceta` (N por Proceso, `clave`, `version` que sube con cada cambio, contrato JSON en
    `apps/catalogo/gacetas.py`) y `catalogo.ReglaFinalizacion` (1:1 opcional). Studio › Salida: «Gacetas» y
    «Condiciones de cierre»; Flujo: bloque GACETA (solo si hay gacetas activas).
  - `apps.gacetas`: `GacetaTicket` (congelada al nacer el ticket; PENDIENTE → ABIERTA → PERIODO_CERRADO →
    RESUELTA / CANCELADA), `ParticipacionItemGaceta` (estado de gaceta ≠ estado operativo; única por
    gaceta+ítem), `RondaRevisionGaceta` (versión presentada + esquema de `apps.aprobaciones` + tarea de
    corrección) y `ObservacionGaceta` (con respuesta; nunca se borra).
  - Ingreso progresivo e idempotente: manual (coordinador), automático al cumplir la condición o al terminar el
    recorrido, y por el bloque GACETA. No depende solo del bloque.
  - Observación o DEVOLVER → EN_CORRECCION con Tarea al responsable (columna o responsable del ticket) y plazo;
    reenviar = nueva versión + nueva ronda en la MISMA gaceta. APROBAR / RECHAZAR / EXCLUIR (rechazar y excluir
    sacan del alcance con origen GACETA y cierran las demás gacetas del ítem); exclusión directa del
    coordinador sin otra aprobación.
  - Periodo vs resolución: cierre del periodo (manual o Beat al vencer, con aceptación automática solo de lo
    limpio) y resolución (IMPEDIR / EXCLUIR / RECHAZAR lo pendiente; automática opcional). Beat:
    `gacetas.reconciliar_vencimientos` cada 15 min (sembrada por migración).
  - Bloque GACETA (estrategia `GACETA_BLOQUE`, la etapa LEGACY GACETA sigue sin ejecutarse): abre, presenta y
    opcionalmente espera (motivo GACETA); publica `gacetas.<clave>.*`.
  - Reglas de finalización congeladas en el ticket (`Ticket.reglas_finalizacion`), evaluadas por
    `apps/tickets/finalizacion.py` en `resolver_ticket`; `ResolucionTicket.resumen_resultados` guarda la foto.
- Migraciones nuevas (sin aplicar): `catalogo/0028`, `tickets/0026`, `workflows/0016`, `tareas/0006`
  (historial con actor Sistema), `gacetas/0001` y `gacetas/0002` (tarea periódica).
- Decisiones tomadas sin consulta: cualquier observación devuelve el ítem a corrección; el coordinador de la
  gaceta es quien edita los ítems; una gaceta nunca abierta no bloquea el cierre; por defecto el cierre exige
  gacetas resueltas y sin correcciones.
- Pendiente (Fase 4 o posterior): notificaciones (Sprint 7, no existen aún); navegación global hacia
  «Gacetas en revisión» (hoy desde el ticket, la aprobación y `/gacetas/`); rediseño del Studio.

## FASE 4 — Studio guiado, constructor visual e integración

Redefinida por el usuario (2026-10-16) en dos entregas: **4.A Studio guiado y arquitectura funcional** y **4.B
Constructor visual e integración final**. Cada una exige autorización expresa.

### 4.A — Studio guiado y arquitectura funcional (implementada 2026-10-16, sin ejecutar ni commit)

- Navegación guiada en las MISMAS cinco secciones (Básico · Formulario · Salida · Flujo · Publicar): Atrás /
  Continuar / Omitir (solo en secciones opcionales; son enlaces: no guardan, activan ni publican), progreso y
  cinco estados (Sin configurar · En progreso · Completa · Opcional · Requiere atención; `studio._estado_secciones`).
  Cambios sin guardar: confirmación explícita al salir (`static/js/studio.js`), sin autoguardado.
- Divulgación progresiva: controles dependientes genéricos (`data-mostrar-si`) y «Opciones avanzadas»; el
  servidor sigue validando y descarta lo que no aplica (modo de fechas de una actividad, parámetros por tipo).
- «Agregar actividad» → «¿Qué necesitas que suceda?»: capa de traducción INTENCIÓN → OPCIÓN → tipo de
  `BloqueOperativo` existente (`apps/catalogo/intenciones.py`); sin motor ni modelo nuevo. El alta por tipo
  (`nuevo-tipo`) sigue funcionando. Crear y editar ofrecen las mismas opciones (rutas de una aprobación al crearla,
  exclusión en el recorrido, cierre de alcance en todos los bloques).
- Dependencias entre secciones (`apps/catalogo/dependencias.py`): bloqueantes (tareas por elemento sin su paso,
  gaceta abierta por un bloque inexistente, ingreso «al terminar el recorrido» sin recorrido) impiden activar la
  configuración y publicar; advertencias (revisión o resultado obligatorio sin paso) no. La interfaz ofrece
  agregar el paso en la fase que el diseñador elija (nunca se crea solo).
- Versionamiento: `FaseWorkflow.clave` estable (migración `workflows/0017`, aditiva con relleno por linaje); al
  publicar otra versión de la plantilla la configuración del Servicio se RECONCILIA (`apps/catalogo/reconciliacion.py`)
  conservando bloques y rutas; fases desaparecidas = conflicto con decisión explícita (mover o quitar, auditado).
  La configuración activa sigue ejecutándose sobre SU versión de la plantilla hasta reconciliar
  (`operaciones.validar_ejecucion`, `motor._iniciar_workflow_por_configuracion`). «Crear copia» conserva la
  configuración activa. Publicar (`apps/catalogo/publicacion.py`) muestra lo activo y lo pendiente, simula la
  publicación sin efectos y «Publicar cambios» activa todo lo pendiente en una transacción (todo o nada).
- Gacetas: coordinador propio opcional (persona o equipo; sin él, quien atiende el ticket) y gaceta obligatoria
  para finalizar (una obligatoria nunca abierta impide resolver; ausente en configuraciones congeladas = opcional).
- Usabilidad: permisos reflejados en la UI, errores con el campo que falla, errores de gacetas en Salida, edición
  de opciones y reglas, entrega formal retirable («Sin entrega formal»), terminología Campo/Columna/Colección,
  responsables generales en Básico también en un Servicio, «Quien solicitó el ticket» no se ofrece en programados.
- Preparación 4.B: `apps/catalogo/flujo_api.py` (fases, actividades, conexiones —secuencia vs ruta—, destinos
  válidos, validar conexión, crear/editar/reordenar, dependencias, guardar) y `mover_bloque_operativo`. Sin
  coordenadas persistentes. Los cambios de motor que la 4.B necesitaría para ir más allá están en el docstring de
  `flujo_api` (bifurcación por fase, rutas en bloques que hoy no las tienen).
- Pruebas: `apps/catalogo/tests_fase4a.py` y `apps/gacetas/tests_fase4a.py` (sin ejecutar).

### 4.B — Constructor visual e integración final (implementada 2026-10-19, sin ejecutar ni commit)

- Sistema visual: `docs/design/ORBITA_DESIGN.md` (adapta Geniestudio/Passionfroot sin marcas ni degradados);
  fondos neutros grises (`--palette-gray-*`) en lugar de crema, lima y carbón intactos.
- Application Shell del Studio (`templates/catalogo/studio.html`, `static/css/studio.css`): cabecera compacta
  (nombre, tipo, estado, estado de guardado, Salir), cinco secciones con UN estado cada una, área de trabajo
  estable y barra inferior (Atrás · Guardar · Omitir · Continuar). Sin scroll de página en escritorio; termina por
  encima del dock (`--dock-reserve`). Estados de guardado reales (Sin cambios · Cambios sin guardar · Guardando ·
  Guardado · Error al guardar), sin autoguardado.
- Básico por grupos (Identidad · Inicio · Responsables · Visibilidad · Tiempos), campos asociados al mismo
  formulario con `form=` (una sola operación de guardado). Formulario como constructor (estructura · campos ·
  propiedades; Campo/Columna/Colección distinguidos; `versionamiento.mover_campo`, solo borrador, auditado).
  Salida por categorías (Resultados · Revisiones · Gacetas · Finalización · Entrega), gaceta en seis pasos,
  selección múltiple (`data-multiselect`), condiciones de cierre agrupadas.
- Lienzo (`apps/catalogo/lienzo.py`, `templates/catalogo/_lienzo.html`, `static/js/lienzo.js`): fases en columnas,
  tarjetas, fondo de puntos, zoom/pan, contracción, inspector derecho, leyenda. Conexiones = SOLO las de
  `flujo_api.conexiones` (secuencia, rutas de aprobación/decisión, finalizar); las conexiones de fase que el motor
  no sigue se dibujan punteadas «no se ejecuta». No se simulan bifurcaciones por fase ni rutas de otros bloques.
  Posición visual (`catalogo.PosicionVisualBloque`, servicio + clave estable; migración 0029) ≠ orden de ejecución;
  no se audita. Cambiar de fase = operación funcional `cambiar_fase_bloque_operativo` (borrador, confirmación,
  auditada, contenedores con sus pasos).
- Revisión y publicación unificada: cabecera compacta, UNA lista (Listo · Requiere atención · Opcional) con
  explicación y «Corregir en…», tabla de versiones, una sola acción «Publicar cambios».
- Pruebas: `apps/catalogo/tests_fase4b.py` (sin ejecutar). Pendiente: validación visual por el usuario.

## Trazabilidad CU

CU-ID conocidos en el repositorio (`docs/sprint_4_3.md`): CU-010 configuración del
catálogo, CU-014 radicación, CU-020 definición/versionamiento/ejecución de Workflow,
CU-021 Tareas, CU-024/CU-025 Aprobaciones.

| Fase | CU-ID con respaldo | Pendiente de catálogo oficial |
|---|---|---|
| 1 | CU-010, CU-020, CU-021 | CU de colecciones/alcance (Procesos, Sprint 5); versiones de ítem (Memento, Sprint 6) |
| 2 | CU-014, CU-020, CU-021, CU-024, CU-025 | CU de Procesos Recurrentes (Sprint 7, adelanto como 4.G1) |
| 3 | CU-024, CU-025 | CU de Gacetas y versionamiento (Sprint 6) |
| 4.A | CU-010, CU-020 | CU del Diseñador/Studio guiado (pendiente de catálogo oficial) |
| 4.B | — | CU del constructor visual (pendiente) |

El catálogo oficial de CU no está en el repositorio: los CU-ID «pendientes» deben
confirmarse con el equipo antes de cerrar cada fase.
