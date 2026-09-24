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

Identidad visual oficial (definitiva desde el incremento 0.5, sustituye toda paleta anterior): SaaS empresarial moderno y tecnológico — sidebar oscuro/carbón compacto con radios amplios, superficies claras ligeramente cálidas, tarjetas con radios amplios, controles tipo píldora/cápsula, iconografía lineal (líneas simples, sin dependencia externa), tipografía Inter, mucho espacio negativo, sombras muy suaves para dar sensación de capas. CSS propio (sin Bootstrap/Tailwind) con design tokens centralizados en `static/css/tokens.css` — nunca hardcodear color/radio/sombra por template.

Paleta: carbón `#17181B` (sidebar) · fondo cálido `#F3F1EA` · superficie `#FFFFFF` · texto `#17181B` · texto secundario `#706E64` · borde `#E7E4DA` · **acento principal lima/chartreuse `#D7FF3E`** (uso estratégico: selección, CTA primario, foco, indicadores — nunca superficies grandes) · success `#1F8F5F` · warning `#B9791A` · danger `#C23B3B` · info `#2569A6`. Secundarios pastel (violeta/durazno/celeste/rosa) reservados para categorización futura de dominios (Tickets, etc.), no para decisiones de jerarquía de UI.

## UX

Ocultar complejidad técnica — nunca mostrar "WorkflowInstance", "transición", "Celery", "PlantillaProceso" en la interfaz. Lenguaje orientado a la acción: "Nuevo ticket", "Solicitar un servicio", "Iniciar un proceso", "Mis tickets", "Mi trabajo", "Necesita tu atención". Preferir cards/timeline/kanban/progreso/maestro-detalle sobre tablas administrativas cuando sea más apropiado.

## Calidad

No lógica específica por servicio. No permisos codificados mediante comparaciones dispersas de usuario/rol. No duplicar reglas de negocio. Nunca borrar trazabilidad histórica. No modificar decisiones arquitectónicas existentes sin explicar antes el impacto. Revisar modelos/servicios/componentes existentes antes de crear nuevos, para evitar duplicaciones.

**Trazabilidad**: toda implementación debe poder responder "¿qué Caso de Uso justifica este código?". Si no hay una respuesta concreta, cuestionar si el código debe existir todavía.
