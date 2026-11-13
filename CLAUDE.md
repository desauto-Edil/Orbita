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

## Autenticación y acceso (Versión 1, 2026-11-12)

- El ÚNICO ingreso es Google (`apps/core/google_auth.py`); solo entra quien el administrador registró por CORREO en un dominio permitido. El `next` viaja por la sesión. Cerrar sesión solo por POST.
- La cuenta ES el correo: `Usuario` (`AbstractBaseUser`, sin `PermissionsMixin`) no tiene `username`, contraseña, `is_staff`, `is_superuser`, grupos ni permisos de Django. Los compañeros ven `nombre_visible`. La sesión se firma con `clave_sesion` (`cerrar_sesiones()`); backend `apps.core.backends.CuentaOrbitaBackend`.
- Accesos SOLO por roles y permisos de Órbita. El primer administrador lo crea quien tiene acceso al servidor: `manage.py otorgar_administrador --correo …` (permiso `plataforma.administracion_avanzada`, GLOBAL).
- Administración avanzada (`apps/core/admin_sitio.py`): apagada salvo `ADMIN_HABILITADA` (contenedor `admin` en `127.0.0.1:8001`, túnel SSH), permiso de plataforma + ingreso con Google reciente, nada se borra, todo se audita.
- Pendiente para el cierre del proyecto: SSO/OIDC con la Intranet (2.UI.2 y 2.UI.3); no implementar antes.
- Detalle: `docs/orbita/01_acceso_y_despliegue.md`.

## Decisiones estructurales de Sprint 0 (vigentes para todo el proyecto salvo que se revisen explícitamente)

- **Una sola app Django `apps.core`** para Sistema/Organización/Permisos/Auditoría — no `cuentas`/`organizacion`/`permisos`/`auditoria` separadas. Futuras apps de dominio (tickets, procesos, workflows...) se evalúan cuando lleguen a su sprint.
- **Un solo `requirements.txt`** y **un solo `config/settings.py`** configurados por variables de entorno — sin separar base/development/production hasta que exista una necesidad concreta de despliegue real.
- **Modelo organizacional sin jerarquía impuesta**: Área y UnidadNegocio son catálogos independientes conectados por tablas intermedias M2M con vigencia (no hay `unidad.area` como FK única). Mismo patrón para Usuario↔Área, Usuario↔Unidad, Equipo↔Área, Equipo↔Unidad — todas explícitas e independientes, nunca inferidas unas de otras.
- **Autorización sin GenericForeignKey en asignaciones de rol** (sí se usa en auditoría, justificado por ser un log append-only). Se resuelve con dos funciones separadas: validar una acción concreta, y resolver/filtrar visibilidad — nunca comparando código de rol en `if`.
- **Django Admin es la interfaz administrativa provisional** de Sprint 0 (Organización/Permisos), no la UX definitiva de Órbita. Usuarios, roles, permisos, asignaciones, áreas, equipos, unidades y categorías ya tienen interfaz propia en **Configuración** (ver Diseño); Django Admin queda como «Administración avanzada» para lo que aún no la tiene (auditoría, catálogo técnico); desde la Versión 1 ya no es un destino del menú ni lo abre `is_staff` (ver Autenticación).
- **Portal sin datos ficticios**: shell visual + empty states reales; se reemplazan por datos reales cuando el dominio correspondiente exista.

## Stack y despliegue

Python, Django 5.2 LTS, PostgreSQL, Django Templates, HTML/CSS/JS propio, Redis, Celery Worker + Beat, Django Channels (WebSocket de la Conversación), Docker Compose; en producción Daphne detrás de Caddy (HTTPS). Sin React/Vue/microservicios/Kubernetes salvo necesidad técnica discutida antes. Producción: `docker-compose.prod.yml` (solo Caddy publica puertos; `db` y `redis` en red interna, Redis con contraseña, rol de PostgreSQL sin superusuario), `deploy/caddy/Caddyfile` (CSP EXIGIDA: nunca `<script>` en línea ni `onclick=`/`onsubmit=` en plantillas; el código va en `static/js/` y las confirmaciones usan `data-confirmar-formulario` / `data-confirmar-envio`), guía en `deploy/DESPLIEGUE.md`. En desarrollo `runserver` sirve HTTP y WebSocket (`daphne` primero en `INSTALLED_APPS`).

## Diseño

CSS propio (sin Bootstrap/Tailwind/build) con design tokens centralizados en `static/css/tokens.css` — nunca hardcodear color/radio/sombra por template; los componentes usan solo tokens semánticos (`--color-primary`, `--surface-*`, `--text-*`, `--border-*`, `--status-*`). Organización: `tokens.css` · `base.css` (reset/tipografía/accesibilidad) · `layout.css` (header + dock) · `components.css` (componentes reutilizables). El catálogo visual interno vive en `/sistema-visual/` (solo `is_staff`).

Acento curado: predeterminado Cielo (Lima, Menta, Ámbar, Coral y Lavanda disponibles); el administrador elige el del sistema y cada persona el suyo (`Usuario.tema`). El acento nunca en superficies grandes, siempre con texto oscuro encima, nunca como color de texto ni de foco. Ningún CSS fuera de `tokens.css` escribe un color fijo (lo vigila una prueba). Modo claro/oscuro por persona.

Glass selectivo: solo en dock, header, popovers, modales y toolbars flotantes — nunca en cards, tablas, formularios ni inputs.

Navegación global: UNA barra flotante superior (Inicio · Mis tickets · Trabajo · Más); qué ve cada quien lo decide `apps/core/navegacion.py` por permisos, nunca por rol. Sin sidebar. Detalle (identidad, Diseñador, bienvenida, Mi perfil, Configuración): `docs/orbita/02_diseno_y_navegacion.md` y `docs/design/ORBITA_DESIGN.md`.

## UX

Ocultar complejidad técnica — nunca mostrar "WorkflowInstance", "transición", "Celery", "PlantillaProceso" en la interfaz. Lenguaje orientado a la acción: "Nuevo ticket", "Solicitar un servicio", "Iniciar un proceso", "Mis tickets", "Mi trabajo", "Necesita tu atención". Preferir cards/timeline/kanban/progreso/maestro-detalle sobre tablas administrativas cuando sea más apropiado.

## Calidad

No lógica específica por servicio. No permisos codificados mediante comparaciones dispersas de usuario/rol. No duplicar reglas de negocio. Nunca borrar trazabilidad histórica. No modificar decisiones arquitectónicas existentes sin explicar antes el impacto. Revisar modelos/servicios/componentes existentes antes de crear nuevos, para evitar duplicaciones.

**Trazabilidad**: toda implementación debe poder responder "¿qué Caso de Uso justifica este código?". Si no hay una respuesta concreta, cuestionar si el código debe existir todavía.

## Mapa del conocimiento del proyecto

El detalle de cada incremento vive fuera de este archivo (se trasladó tal cual el 2026-11-13). Antes de tocar un área, lee su documento:

| Documento | Qué contiene |
|---|---|
| `docs/orbita/01_acceso_y_despliegue.md` | Ingreso con Google, cuenta = correo, permiso de plataforma, administración avanzada, stack y despliegue |
| `docs/orbita/02_diseno_y_navegacion.md` | Identidad visual, tokens, Diseñador (D1–D3), bienvenida, Mi perfil, Configuración, navegación |
| `docs/orbita/03_experiencia_operativa.md` | Trabajo y Cola, espacio unificado del ticket (UX-1 a UX-5), solicitar un servicio |
| `docs/orbita/04_dominio_tickets_sprint4.md` | Sprint 4.x: tiempos, prórrogas, Ticket General, buscador, variables, entregables, programados, parrilla, conjuntos, cancelación, tareas por elemento |
| `docs/orbita/05_modelo_configurable_procesos.md` | Modelo configurable de Procesos, fases 1 a 4.B (colecciones, recorridos, gacetas, finalización, Studio) |
| `docs/orbita/06_conversacion_notificaciones_y_ajustes.md` | Conversación e historial, notificaciones y alertas, presencia, evidencias, jefaturas, Google Chat (pospuesto), histórico, correcciones del 2026-11-12 |
| `docs/design/ORBITA_DESIGN.md` | Sistema visual |
| `.claude/plans/*.md` | Planes aprobados de cada iniciativa |
| `deploy/DESPLIEGUE.md` | Puesta en producción en el VPS |

Reglas que nunca se rompen (resumen; el porqué está en los documentos):
- Configuración antes que código; nada se borra (retirar = desactivar); toda mutación se audita.
- En una GACETA nadie aprueba: es un espacio de comentarios con silencio positivo.
- Un ítem ENVIADO A REVISIÓN o APROBADO es de solo lectura; la única forma de corregirlo es que lo DEVUELVAN (sin excepciones).
- El Ticket es uno: un Workflow completado no resuelve ni cierra el Ticket.
- Autorización siempre por `puede_*` / permisos; nunca comparando roles ni `is_staff`/`is_superuser` (no existen).
