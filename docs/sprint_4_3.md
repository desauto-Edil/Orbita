# Sprint 4.3 — Ejecución configurable

Implementación de CU-010 (configuración del catálogo), CU-020
(definición/versionamiento y ejecución de Workflow) y la integración con
CU-014 (radicación), CU-021 (Tareas) y CU-024/025 (Aprobaciones).
No añade un segundo motor ni una representación empresarial persistida.

## Integración

- `Servicio.workflow` sigue siendo la asociación del catálogo. Puede ser
  compartida: configurar/publicar ese Workflow afecta las futuras ejecuciones
  de todas sus definiciones asociadas. No se impone propiedad exclusiva ni
  se añade una marca de Studio.
- `WorkflowVersion` y sus `Etapa`/`TransicionEtapa` son la definición.
  ACTIVIDAD usa TAREA; APROBACION conserva su tipo; ESPERA conserva su JSON;
  DECISION usa CONDICION y sus transiciones.
- `Ticket.instancia_workflow` sigue siendo el único vínculo persistente entre
  Ticket y ejecución. Tanto SERVICIO con Workflow como PROCESO lo utilizan
  al radicar; SERVICIO sin Workflow mantiene su atención simple.
- El formulario y las expectativas de entregables se congelan al crear
  borrador. La versión de ejecución se selecciona al radicar. No se rellenan
  históricos ni se cambian versiones ya utilizadas.

## Operaciones para el futuro Studio

`apps.catalogo.ejecucion` expone:

- `preparar_ejecucion(servicio, actor)`: reutiliza un borrador; si no existe,
  clona la versión activa. Para una definición vacía crea INICIO → FIN.
- `agregar_bloque(servicio, version, actor, tipo=..., nombre=..., ...)`:
  crea Etapa y su configuración en una misma transacción.
- `configurar_bloque`, `editar_bloque`, `eliminar_bloque`: configuración y
  metadatos sobre el borrador. Inicio y fin no se eliminan por esta vía.
- `conectar_bloques`: crea una transición o redirige la indicada mediante
  `conexion=`. `desconectar_bloques` elimina una conexión explícitamente.
- `publicar_ejecucion`: activa la versión y publica el catálogo mediante
  `activo`, en una transacción. Un fallo de validación/auditoría revierte ambas.

No hay orden lineal duplicado: el orden de ejecución está en las conexiones.
Las decisiones usan la prioridad y el fallback del motor, sin otro evaluador.
Solo se consultan las `variables` del contexto ya existente; no se incorpora
un traductor de formularios a condiciones. Las esperas admiten DURACION
(HORAS/DIAS) o FECHA ISO, y siguen usando la tarea Celery Beat existente.

Ejemplo programático, con una definición que ya tiene formulario activo:

```python
from apps.catalogo.ejecucion import (
    preparar_ejecucion, agregar_bloque, conectar_bloques, publicar_ejecucion,
)

version = preparar_ejecucion(servicio, configurador)
inicio = version.etapas.get(tipo="INICIO")
fin = version.etapas.get(tipo="FIN")
conexion = inicio.transiciones_salientes.get()
actividad = agregar_bloque(
    servicio, version, configurador, tipo="ACTIVIDAD", nombre="Preparar resultado",
    descripcion="Preparar el material solicitado.", tipo_actor="EQUIPO", equipo=equipo,
)
conectar_bloques(servicio, version, inicio, actividad, configurador, conexion=conexion)
conectar_bloques(servicio, version, actividad, fin, configurador)
publicar_ejecucion(servicio, version, configurador)
```

Para APROBACION se reciben `modo`, `politica` y `participantes`, la misma
lista de pares del editor: `[("USUARIO", usuario), ("SOLICITANTE", None)]`.
Toda aprobación conserva las tres salidas APROBADA/RECHAZADA/DEVUELTA;
RECHAZADA y DEVUELTA pueden apuntar a una actividad anterior. Al volver a
ella se crea otra ejecución y otra Tarea, preservando las anteriores.

## Actores y autorización

El configurador requiere los permisos existentes `catalogo.administrar` y
`workflows.administrar`, ambos con su alcance GLOBAL actual. Esto evita que
administrar el catálogo conceda implícitamente edición de un Workflow
compartido. No hay roles, permisos ni bypass nuevos.

`apps.workflows.actores.resolver_actor` devuelve usuario/equipo reales:

| Actor | Resolución al entrar en la etapa |
|---|---|
| USUARIO | FK de usuario configurada |
| EQUIPO | FK de equipo configurada, sin expandir miembros |
| SOLICITANTE | `Ticket.solicitante` |
| RESPONSABLE_TICKET | `Ticket.usuario_responsable` vigente |

`Equipo` no tiene un líder único en el modelo organizacional. Por eso
RESPONSABLE_EQUIPO no está soportado ni se infiere por rol o antigüedad.

Los actores dinámicos requieren un Ticket. El resolutor usa la relación
canónica y lee el Ticket bajo su lock de reasignación. Durante el inicio,
antes de que `iniciar_workflow` retorne a radicación, utiliza el `ticket_id`
que esa operación ya enviaba en `datos_iniciales`; comprueba que sea un
borrador aún sin instancia y asociado al mismo Workflow. Una vez vinculada,
la instancia no depende de ese JSON para resolver al Ticket.

Si falta el responsable individual, no se elige un miembro del equipo:
se produce un error de ejecución. Durante la radicación eso revierte toda
la operación. El borrador normal aún no tiene responsable: un flujo que
necesite RESPONSABLE_TICKET debe permitir la asignación existente antes de
llegar a ese bloque, por ejemplo mediante una primera actividad de equipo.
No se añade recuperación de instancias ERROR ni una espera nueva por asignación.

Una vez creada la Tarea/Aprobación, su asignación queda en su propio dominio.
Reasignar el Ticket cambia la resolución de etapas futuras; no reescribe las
tareas ni las aprobaciones ya creadas. Un equipo aprobador representa una
participación que puede decidir un miembro autorizado, no unanimidad.

Las policies de ejecución de Tareas/Aprobaciones y la policy individual de
escritura de Entregables V1 se conservan. Ser actor de etapa no concede
escritura de los entregables finales de otro responsable.

## Atomicidad, auditoría e inmutabilidad

La configuración empresarial verifica pertenencia y relee BORRADOR bajo
el lock de Workflow compartido con creación de versiones y activación.
ACTIVA/HISTORICA no son editables aunque el llamador conserve un objeto
obsoleto. La creación de versiones serializa también el número siguiente.

La radicación conserva su lock de Ticket: inicio, instancia, tareas y
auditoría revierten si falla. No hay segunda instancia ante doble radicación.

El editor y el versionamiento siguen registrando sus propios eventos. La
capa empresarial solo añade el evento de asociación Servicio → Workflow.
No añade otro evento equivalente por encima de cada operación inferior.

La migración `workflows.0008_actores_dinamicos` amplía dos campos de tipo y
sus restricciones. No transforma datos ni toca modelos de Ticket, Tarea,
Aprobación o Entregables. Se escribió manualmente y no se ejecutó.

## Extensión futura de ENTREGA

Satisfacer un entregable, aprobar internamente, entregar y cerrar son
operaciones diferentes. Ninguna provoca automáticamente las otras.
Una futura operación de dominio de entrega podrá integrarse con las
ejecuciones y el mecanismo de espera externa/continuación existentes;
la elección concreta del bloque corresponderá a ese incremento. No se
reserva ahora un nuevo `Etapa.Tipo` ni se representa entrega como aprobación.

Studio visual, confirmación/observaciones del solicitante, cierre automático,
versiones/revisión de entregables, anotaciones, Kanban, timeline, recurrencia
y campos tabulares/calendario permanecen fuera de alcance. Se conserva la
deuda anterior de Dock/Más, clonación histórica desde UI y alcances de
Tareas/Aprobaciones.

## Validación pendiente del usuario

Se escribieron pruebas en los tres archivos `tests.py` existentes y se
realizó revisión estática. No se ejecutaron tests, migraciones ni comandos
que inicialicen Django.

```bash
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python manage.py migrate
docker compose exec web python manage.py check
docker compose exec web python manage.py test apps.catalogo apps.tickets apps.workflows
docker compose exec web python manage.py test apps.tareas apps.aprobaciones apps.core
docker compose exec web python manage.py auditar_catalogo
```
