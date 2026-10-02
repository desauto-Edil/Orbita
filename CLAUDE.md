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
- **Django Admin es la interfaz administrativa provisional** de Sprint 0 (Organización/Permisos), no la UX definitiva de Órbita.
- **Portal sin datos ficticios**: shell visual + empty states reales; se reemplazan por datos reales cuando el dominio correspondiente exista.

## Stack

Python, Django, PostgreSQL, Django ORM, Django Templates, HTML/CSS/JS, DRF solo cuando sea necesario, Redis, Celery Worker, Celery Beat, Docker/Docker Compose, Gunicorn (prod), Caddy (prod, no en dev). Sin React/Vue/microservicios/Kubernetes salvo necesidad técnica concreta discutida antes.

## Diseño

Identidad visual oficial (carbón + superficies cálidas + acento lima; la Fase visual V0 reorganizó el sistema pero conservó esta paleta): SaaS empresarial moderno y tecnológico — superficies claras ligeramente cálidas, controles tipo píldora/cápsula, iconografía lineal de una sola familia (sprite `templates/layout/icons.html`), tipografía Inter (con fallback del sistema, sin fuente externa), mucho espacio negativo, sombras muy suaves. Solo modo claro; los tokens están en capas (paleta primitiva → tokens semánticos → alias legados) para que un tema oscuro futuro solo redefina la capa semántica.

CSS propio (sin Bootstrap/Tailwind/build) con design tokens centralizados en `static/css/tokens.css` — nunca hardcodear color/radio/sombra por template; los componentes usan solo tokens semánticos (`--color-primary`, `--surface-*`, `--text-*`, `--border-*`, `--status-*`). Organización: `tokens.css` · `base.css` (reset/tipografía/accesibilidad) · `layout.css` (header + dock) · `components.css` (componentes reutilizables). El catálogo visual interno vive en `/sistema-visual/` (solo `is_staff`).

Paleta: carbón `#17181B` (dock, texto) · fondo cálido `#F3F1EA` · superficie `#FFFFFF` · texto secundario `#706E64` · borde `#E7E4DA` · **acento principal lima/chartreuse `#D7FF3E`** (selección, CTA primario, indicadores — nunca superficies grandes; siempre con texto oscuro encima y nunca como color de texto ni de foco sobre fondo claro) · success/warning/danger/info independientes. Secundarios pastel (violeta/durazno/celeste/rosa) reservados para categorización futura de dominios.

Glass selectivo: solo en dock, header, popovers, modales y toolbars flotantes — nunca en cards, tablas, formularios ni inputs.

Diseñador (D1): único destino de configuración, en Más › Gestión — unifica Studio y Workflows avanzados en dos secciones locales: **Flujos** (biblioteca de `Workflow`, definición reutilizable; la vista técnica queda como herramienta secundaria) y **Servicios** (Servicios y Procesos, configurados en Studio). Quién entra y qué ve cada quien lo decide `apps/core/disenador.py` por capacidades (`catalogo.administrar`, `workflows.administrar|consultar|vincular`), nunca por rol. El Diseñador unifica la experiencia, no sustituye el dominio. D2: un Flujo (Workflow real de la biblioteca) se crea, diseña (lienzo de bloques), versiona, publica y copia sin pasar por un Servicio (`apps/catalogo/flujos.py`, URLs `disenador/flujos/…`, operaciones `*_en_flujo` en `apps/catalogo/ejecucion.py`); Studio y el lienzo comparten los mismos manejadores de bloques mediante un «ancla» (Servicio o Flujo). Publicar un flujo nunca publica servicios; modificar/publicar un flujo en uso exige confirmación (No / Crear copia / Sí, y confirmación reforzada al publicar).

Navegación global: exactamente dos niveles — Header (identidad, búsqueda, notificaciones, menú de usuario) y Dock adaptativo (Inicio · Mis tickets · Trabajo · Más). La fuente única de qué destinos ve cada usuario es `apps/core/navegacion.py`, por permisos/capacidades reales, nunca por nombre de rol. Tabs/segmentos/filtros dentro de una pantalla son controles locales, no navegación global. Sin sidebar.

## UX

Ocultar complejidad técnica — nunca mostrar "WorkflowInstance", "transición", "Celery", "PlantillaProceso" en la interfaz. Lenguaje orientado a la acción: "Nuevo ticket", "Solicitar un servicio", "Iniciar un proceso", "Mis tickets", "Mi trabajo", "Necesita tu atención". Preferir cards/timeline/kanban/progreso/maestro-detalle sobre tablas administrativas cuando sea más apropiado.

Solicitar un servicio o proceso (V2): entrada directa desde Inicio/Explorar a un workspace (formulario + vista previa viva, abierta por defecto) → "Revisa tu solicitud" → "Enviar solicitud" (radicación real) → confirmación. No hay ficha previa, wizard ni catálogo aparte. La presentación vive en `apps/tickets/solicitud.py` y reutiliza el motor existente: el servidor (`validaciones.calcular_estados_efectivos`) decide qué campos son visibles/requeridos y el navegador solo lo aplica (`/tickets/<pk>/solicitud/estado/`); no se duplican reglas condicionales en JS.

## Calidad

No lógica específica por servicio. No permisos codificados mediante comparaciones dispersas de usuario/rol. No duplicar reglas de negocio. Nunca borrar trazabilidad histórica. No modificar decisiones arquitectónicas existentes sin explicar antes el impacto. Revisar modelos/servicios/componentes existentes antes de crear nuevos, para evitar duplicaciones.

**Trazabilidad**: toda implementación debe poder responder "¿qué Caso de Uso justifica este código?". Si no hay una respuesta concreta, cuestionar si el código debe existir todavía.
