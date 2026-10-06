# Órbita

Plataforma interna empresarial para centralizar tickets, servicios, procesos, tareas, aprobaciones, gacetas, conocimiento, SLA, notificaciones, analítica y trazabilidad. **No** es una aplicación tradicional de tickets: es el punto central desde el cual los empleados solicitan servicios y ejecutan procesos organizacionales.

## Concepto central

`Ticket` es la unidad universal de radicación y seguimiento. Dos tipos iniciales:
- `SERVICIO`: el usuario necesita atención, soporte, una entrega o gestión ofrecida por un área.
- `PROCESO`: el usuario o el sistema inicia un flujo organizacional previamente configurado.

Un ticket puede originarse manualmente o automáticamente (schedule/workflow/sistema). La interfaz usa siempre "Ticket", nunca "Solicitud"/"Request" salvo que sea técnicamente inevitable.

## Principio arquitectónico: configuración antes que código

Crear o modificar servicios, formularios, workflows, aprobaciones, SLA, procesos recurrentes y gacetas **no debe requerir modificar código, crear migraciones específicas ni redesplegar**. Django construye los motores genéricos; PostgreSQL almacena su configuración.

## Desarrollo dirigido por Casos de Uso (CU)

El roadmap está gobernado por un catálogo oficial de Casos de Uso. **Nada se implementa "porque parece conveniente"** — cada incremento debe declarar explícitamente los CU-IDs que satisface. Antes de tocar código para cualquier sprint:

1. Identificar los CU asignados (con su descripción exacta — no reinterpretar).
2. Identificar actores.
3. Identificar precondiciones.
4. Identificar reglas de negocio.
5. Identificar modelos afectados.
6. Identificar permisos y alcances.
7. Identificar eventos/auditoría.
8. Identificar componentes existentes reutilizables.
9. Determinar si aplica un patrón de diseño ya definido.
10. Confirmar qué queda explícitamente fuera del incremento.

No implementar CU de sprints futuros anticipadamente. Si un CU parece requerir otro CU aún no asignado, detenerse y explicar la dependencia antes de escribir esa funcionalidad. Preparar arquitectura significa no bloquear una extensión futura — **no** significa implementarla anticipadamente.

Patrones de diseño atados a su sprint disparador (no introducir antes): `State` → Tickets (Sprint 2, incrementos 2.3+); `Strategy` (tipos de etapa de workflow) → Workflow Engine (Sprint 3); `Memento` → versiones de Gaceta (Sprint 6); `Domain Events`/`Observer` → introducido en Sprint 2 (incremento 2.3, consumidor real: historial funcional del Ticket) y ampliado en Sprint 7 (SLA/Notificaciones); `Command`/`Factory` solo cuando la complejidad real lo justifique.

### Orden oficial de sprints

0. Sistema, Organización, Roles/Alcances y Auditoría base.
1. Catálogo de Servicios + Constructor de Formularios.
2. Tickets — creación, radicación y gestión completa. Dividido en incrementos verificables: 2.1 Modelo base y borradores; 2.2 Radicación y respuestas; 2.3 Atención y asignación (State); 2.4 Comunicación, adjuntos y solicitud de información; 2.5 Resolución, cierre, reapertura y trazabilidad.
3. Workflow Engine y Tareas.
4. Aprobaciones.
5. Procesos.
6. Gacetas y versionamiento (Memento).
7. SLA, Domain Events, Notificaciones y Procesos Recurrentes.
8. Mi Portal, búsqueda y Conocimiento.
9. Encuestas.
10. Analítica y Control.

El plan detallado y aprobado de cada sprint en curso vive en `.claude/plans/` (o se solicita al equipo si no está disponible). El plan de Sprint 0 fijó decisiones estructurales importantes — ver sección siguiente.

## Roles y permisos

Roles iniciales: Usuario, Ejecutor, Aprobador, Gestor de Servicios, Gestor de Procesos, Administrador de Área, Administrador de Unidad, Analista, Auditor, Administrador de Plataforma.

Los permisos combinan **ROL + ALCANCE + RELACIÓN CON EL OBJETO**. Alcances: `GLOBAL`, `AREA`, `UNIDAD`, `SERVICIO`, `PROCESO` (estos dos últimos solo cuando existan esos modelos). Ser solicitante/responsable de un ticket/tarea o participante de una gaceta **no** constituye un rol global.

## Autenticación

El login local de Django (usuario/contraseña) es **provisional**. Pendiente al cierre/integración final del proyecto:

- **2.UI.2 — Preparación SSO.**
- **2.UI.3 — OIDC real + integración con Intranet.**

Objetivo futuro: un usuario autenticado corporativamente entra desde la Intranet/un enlace directo, no vuelve a introducir credenciales, y Órbita resuelve la autorización local (roles/alcances) sobre esa identidad ya autenticada externamente. No se implementa nada de OAuth/OIDC/SSO hasta que se llegue explícitamente a esos incrementos.

## Decisiones estructurales de Sprint 0 (vigentes para todo el proyecto salvo que se revisen explícitamente)

- **Una sola app Django `apps.core`** para Sistema/Organización/Permisos/Auditoría — no `cuentas`/`organizacion`/`permisos`/`auditoria` separadas. Futuras apps de dominio (tickets, procesos, workflows...) se evalúan cuando lleguen a su sprint.
- **Un solo `requirements.txt`** y **un solo `config/settings.py`** configurados por variables de entorno — sin separar base/development/production hasta que exista una necesidad concreta de despliegue real.
- **Modelo organizacional sin jerarquía impuesta**: Área y UnidadNegocio son catálogos independientes conectados por tablas intermedias M2M con vigencia (no hay `unidad.area` como FK única). Mismo patrón para Usuario↔Área, Usuario↔Unidad, Equipo↔Área, Equipo↔Unidad — todas explícitas e independientes, nunca inferidas unas de otras.
- **Autorización sin GenericForeignKey en asignaciones de rol** (sí se usa en auditoría, justificado por ser un log append-only). Se resuelve con dos funciones separadas: validar una acción concreta, y resolver/filtrar visibilidad — nunca comparando código de rol en `if`.
- **Django Admin es la interfaz administrativa provisional** de Sprint 0 (Organización/Permisos), no la UX definitiva de Órbita. Usuarios, roles, permisos, asignaciones, áreas, unidades y categorías ya tienen interfaz propia en **Configuración** (ver Diseño); Django Admin queda como «Administración avanzada» (`is_staff`) para lo que aún no la tiene (equipos, auditoría, catálogo técnico).
- **Portal sin datos ficticios**: shell visual + empty states reales; se reemplazan por datos reales cuando el dominio correspondiente exista.

## Stack

Python, Django, PostgreSQL, Django ORM, Django Templates, HTML/CSS/JS, DRF solo cuando sea necesario, Redis, Celery Worker, Celery Beat, Docker/Docker Compose, Gunicorn (prod), Caddy (prod, no en dev). Sin React/Vue/microservicios/Kubernetes salvo necesidad técnica concreta discutida antes.

## Diseño

Identidad visual oficial (carbón + superficies cálidas + acento lima; la Fase visual V0 reorganizó el sistema pero conservó esta paleta): SaaS empresarial moderno y tecnológico — superficies claras ligeramente cálidas, controles tipo píldora/cápsula, iconografía lineal de una sola familia (sprite `templates/layout/icons.html`), tipografía Inter (con fallback del sistema, sin fuente externa), mucho espacio negativo, sombras muy suaves. Solo modo claro; los tokens están en capas (paleta primitiva → tokens semánticos → alias legados) para que un tema oscuro futuro solo redefina la capa semántica.

CSS propio (sin Bootstrap/Tailwind/build) con design tokens centralizados en `static/css/tokens.css` — nunca hardcodear color/radio/sombra por template; los componentes usan solo tokens semánticos (`--color-primary`, `--surface-*`, `--text-*`, `--border-*`, `--status-*`). Organización: `tokens.css` · `base.css` (reset/tipografía/accesibilidad) · `layout.css` (header + dock) · `components.css` (componentes reutilizables). El catálogo visual interno vive en `/sistema-visual/` (solo `is_staff`).

Paleta: carbón `#17181B` (dock, texto) · fondo cálido `#F3F1EA` · superficie `#FFFFFF` · texto secundario `#706E64` · borde `#E7E4DA` · **acento principal lima/chartreuse `#D7FF3E`** (selección, CTA primario, indicadores — nunca superficies grandes; siempre con texto oscuro encima y nunca como color de texto ni de foco sobre fondo claro) · success/warning/danger/info independientes. Secundarios pastel (violeta/durazno/celeste/rosa) reservados para categorización futura de dominios.

Glass selectivo: solo en dock, header, popovers, modales y toolbars flotantes — nunca en cards, tablas, formularios ni inputs.

Diseñador (D1): único destino de configuración, en Más › Gestión — unifica Studio y Workflows avanzados en dos secciones locales: **Flujos** (biblioteca de `Workflow`, definición reutilizable; la vista técnica queda como herramienta secundaria) y **Servicios** (Servicios y Procesos, configurados en Studio). Quién entra y qué ve cada quien lo decide `apps/core/disenador.py` por capacidades (`catalogo.administrar`, `workflows.administrar|consultar|vincular`), nunca por rol. El Diseñador unifica la experiencia, no sustituye el dominio. D2: un Flujo (Workflow real de la biblioteca) se crea, diseña (lienzo de bloques), versiona, publica y copia sin pasar por un Servicio (`apps/catalogo/flujos.py`, URLs `disenador/flujos/…`, operaciones `*_en_flujo` en `apps/catalogo/ejecucion.py`); Studio y el lienzo comparten los mismos manejadores de bloques mediante un «ancla» (Servicio o Flujo). Publicar un flujo nunca publica servicios; modificar/publicar un flujo en uso exige confirmación (No / Crear copia / Sí, y confirmación reforzada al publicar).

Configuración: destino de administración en Más › Administración (`/configuracion/`), con secciones locales General (nombre y logo del sistema, modelo `ConfiguracionSistema` de una sola fila, expuesto a los templates como `sistema`), Usuarios, Roles y permisos, Áreas, Unidades y Categorías. Qué sección ve cada quien lo decide `apps/core/configuracion.py` por capacidades (`sistema.configurar`, `usuarios.administrar`, `permisos.administrar`, `organizacion.administrar`, `catalogo.administrar`; `is_superuser` abre las del núcleo para poder otorgar el primer permiso), nunca por rol. No añade reglas de dominio: reutiliza los modelos de Sprint 0, nada se elimina (retirar es desactivar) y toda mutación se audita con `auditoria.auditar_guardado`. El nombre del sistema nunca se escribe fijo en un template: se usa `{{ sistema.nombre }}`.

Navegación global: exactamente dos niveles — Header (identidad, búsqueda, notificaciones, menú de usuario) y Dock adaptativo (Inicio · Mis tickets · Trabajo · Más). La fuente única de qué destinos ve cada usuario es `apps/core/navegacion.py`, por permisos/capacidades reales, nunca por nombre de rol. Tabs/segmentos/filtros dentro de una pantalla son controles locales, no navegación global. Sin sidebar.

## UX

Ocultar complejidad técnica — nunca mostrar "WorkflowInstance", "transición", "Celery", "PlantillaProceso" en la interfaz. Lenguaje orientado a la acción: "Nuevo ticket", "Solicitar un servicio", "Iniciar un proceso", "Mis tickets", "Mi trabajo", "Necesita tu atención". Preferir cards/timeline/kanban/progreso/maestro-detalle sobre tablas administrativas cuando sea más apropiado.

Trabajo: Cola en orden de llegada (el más antiguo primero, con su posición; los filtros Sin tomar / En atención / Míos son locales y no cambian la posición). Mi trabajo es una lista compacta y paginada; en pantallas amplias cada fila abre una vista previa lateral de solo lectura (fragmentos `tareas:vista_previa` / `aprobaciones:vista_previa`, misma autorización de objeto que el detalle) y sin JS o en móvil va directo al detalle. En el detalle, lo secundario (reasignar, delegar, historiales) va plegado. Estilos en `static/css/trabajo.css`.

Solicitar un servicio o proceso (V2): entrada directa desde Inicio/Explorar a un workspace (formulario + vista previa viva, abierta por defecto) → "Revisa tu solicitud" → "Enviar solicitud" (radicación real) → confirmación. No hay ficha previa, wizard ni catálogo aparte. La presentación vive en `apps/tickets/solicitud.py` y reutiliza el motor existente: el servidor (`validaciones.calcular_estados_efectivos`) decide qué campos son visibles/requeridos y el navegador solo lo aplica (`/tickets/<pk>/solicitud/estado/`); no se duplican reglas condicionales en JS.

Compromiso temporal (Sprint 4.x, 4.A1): el tiempo objetivo de atención pertenece al Servicio/Proceso (`tiempo_objetivo_cantidad/unidad/habiles`), nunca al Workflow, y cada Ticket lo congela al crear su borrador. Al radicar se fija `fecha_objetivo_original` (inmutable) y `fecha_objetivo_vigente` (la única que mueven las prórrogas). Todo cálculo de fechas vive en `apps/tickets/tiempos.py` (hábil = lunes a viernes, sin festivos; sustituible por un calendario sin tocar el resto). No es el módulo SLA del Sprint 7: sin alertas, escalamiento ni métricas.

Prórrogas (4.A2): pertenecen al Ticket (`ProrrogaTicket`, `apps/tickets/prorrogas.py`), nunca al Workflow, la Tarea ni el Bloque, y solo mueven `fecha_objetivo_vigente`. La política (NO_PERMITE / SIN_APROBACION / CON_APROBACION, más un aprobador usuario o equipo) se configura en el Servicio/Proceso y se congela en el Ticket. CON_APROBACION reutiliza `apps.aprobaciones` sin acoplarlo a Workflow (`ProrrogaTicket.esquema_aprobacion`): el aprobador decide en la pantalla de Aprobaciones y esa decisión pasa por `resolver_prorroga_por_aprobacion`. A lo sumo una prórroga PENDIENTE por Ticket; no toca `EntregaTicket.vence_en`.

Ticket General (4.C1): para pedir algo no catalogado existe UN Servicio interno (`Servicio.es_ticket_general`, único por constraint) que reutiliza Ticket/TicketServicio, el formulario versionado y el ciclo normal; no hay Ticket sin Servicio, modelo paralelo ni Workflow artificial. Está activo pero nunca es un Servicio catalogado: `servicios_visibles_para` lo excluye (única autoridad del catálogo, explorador, búsqueda y frecuentes) y `servicios_accesibles_para` lo incluye solo para su entrada propia (`crear_borrador_ticket_general`, `/tickets/general/`). `ConfiguracionTicketGeneral` (fila única) guarda solo si está habilitado; formulario, tiempo objetivo, prórroga, entrega y visibilidad se editan en el Studio del Servicio interno, sin duplicarse. Se administra desde Diseñador › Servicios con `catalogo.administrar` (`apps/catalogo/ticket_general.py`). Un servicio con tickets no puede dejar de ser (ni pasar a ser) el interno: el origen de un ticket se deduce de `TicketServicio.servicio`. Destinos y enrutamiento (4.C2): cada Ticket General se dirige a un `DestinoTicketGeneral` configurado (Área, Equipo o Persona reales, un solo destino por objeto, activar/desactivar y nunca borrar; `apps/catalogo/destinos_ticket_general.py`). DESTINO ≠ RESPONSABLE: el destino es a quién va la solicitud; el responsable inicial (un usuario o un equipo, nunca un Área) es lo que se asigna al Ticket al radicar. El destino de reserva ("No estoy seguro") vive solo en `ConfiguracionTicketGeneral.destino_predeterminado`; sin él, elegir destino es obligatorio. Al radicar, `apps/tickets/direccionamiento.py` resuelve y valida el destino bajo lock, deja el responsable en el Ticket y fija la foto en `DireccionamientoTicket` (inmutable; el historial lo registra como DIRECCIONADO). Direccionar NO inicia la atención: el ticket sigue RADICADO hasta `tomar_ticket` (equipo) o `iniciar_atencion_ticket` (ya dirigido a una persona); `asignar_ticket` no cambió. `TicketContextoAtencion` (alcance de quien supervisa) y destino son conceptos distintos y no se mezclan. Tiempo objetivo, prórroga y entrega siguen viniendo solo del Servicio interno (sin overrides por destino).

Buscador "¿Qué necesitas?" (4.D): la barra del hero de Inicio describe una necesidad en lenguaje natural y se resuelve por REGLAS deterministas (sin IA, embeddings ni servicios externos; no se guarda ni audita el texto buscado). Vive en `apps/catalogo/busqueda.py` (`buscar_servicios_por_necesidad`): candidatos = `servicios_visibles_para` (nunca `Servicio.objects.all()`) con categoría activa y formulario con versión activa, así que lo restringido, apagado o el Servicio interno del Ticket General no se cargan ni filtran sus términos. Puntaje por campo (término configurado > nombre > categoría > descripción/instrucciones), pesos, umbral (`UMBRAL_RELEVANCIA`) y máximo de resultados centralizados en ese módulo; texto normalizado en `apps/catalogo/normalizacion.py` (sin acentos/puntuación, palabras vacías, raíz aproximada). Los términos pertenecen al Servicio/Proceso (`TerminoServicio`, se administran en Studio › Básico con `catalogo.administrar` y se auditan; no son campos del formulario). La vista `core:necesidad` solo valida, busca y presenta; nunca crea tickets. El Ticket General no puntúa: se ofrece aparte ("Crear ticket general") con la misma disponibilidad de 4.C1/4.C2 (`inicio.ticket_general_disponible`). Explorar, categorías y frecuentes no cambian.

Variables y resultados del motor (4.B0): una DECISION evalúa datos que la ejecución ya conoce, con tres referencias punteadas además de las variables planas de siempre (que no cambian): `ticket.<dato>` (estado, tipo, origen, es_general, solicitante, responsable, equipo_responsable, radicado_en, fecha_objetivo, fecha_objetivo_original; lectura en vivo, sin volcar el modelo), `formulario.<clave>` (respuesta del formulario CONGELADO del Ticket, con su tipo lógico) y `aprobaciones.<clave_bloque>.resultado` (APROBADA/RECHAZADA/DEVUELTA, publicado por el motor al cerrar la aprobación, adicional a `resultado_aprobacion`, que sigue eligiendo la ruta). Las referencias usan CLAVES ESTABLES, nunca etiqueta, nombre ni pk: `Campo.clave` (única por `FormularioVersion`) y `BloqueOperativo.clave` (única por `ConfiguracionEjecucionVersion`); se generan una vez desde etiqueta/nombre con sufijo determinista ante colisión (`apps/catalogo/claves.py`), se copian siempre al clonar la versión y solo se cambian explícitamente mientras es BORRADOR. La lectura vive en `apps/workflows/variables.py` (`ResolutorVariables`) y la escritura de resultados en `contexto.publicar_resultado_bloque` (namespace `resultados_bloques` del mismo contexto JSON; nunca otro modelo). Variable INEXISTENTE (centinela interno, nunca a JSON) ≠ valor NULL: ninguna condición se cumple frente a una inexistente (tampoco `DISTINTO_DE`/`ESTA_VACIO`), así un Workflow compartido por Servicios con formularios distintos cae en su fallback sin error. La comparación es tipada (bool, Decimal/número, fecha, fecha-hora, texto, lista). `formulario.*` y `ticket.*` aplican a ambos modos; `aprobaciones.*` solo a PLANTILLA_FASES (`BloqueOperativo`). 4.B1 (ENTREGABLE) suma su ámbito (`entregables.<clave>.satisfecho`) en `contexto.AMBITOS_BLOQUE` sin otro refactor.

## Calidad

No lógica específica por servicio. No permisos codificados mediante comparaciones dispersas de usuario/rol. No duplicar reglas de negocio. Nunca borrar trazabilidad histórica. No modificar decisiones arquitectónicas existentes sin explicar antes el impacto. Revisar modelos/servicios/componentes existentes antes de crear nuevos, para evitar duplicaciones.

**Trazabilidad**: toda implementación debe poder responder "¿qué Caso de Uso justifica este código?". Si no hay una respuesta concreta, cuestionar si el código debe existir todavía.
