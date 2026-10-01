"""Autorización de Tareas — CU-021/022/023.

Mismo criterio ROL+ALCANCE+RELACIÓN CON EL OBJETO que el resto del proyecto
(`apps.core.autorizacion`), y misma separación de capas que
`apps/tickets/autorizacion.py` (su precedente más directo): consultar ≠
gestionar (supervisar asignación) ≠ ejecutar el trabajo (relación directa).

Solo 2 permisos (W.16/aprobado, evita granularidad sin frontera real):

- `tareas.consultar` — visibilidad amplia por alcance.
- `tareas.gestionar` — función supervisora: asignar/reasignar/delegar a un
  tercero, igual que `tickets.atender` cubre asignar/reasignar de Ticket.

Tomar/iniciar/completar/comentar/crear subtarea NO exigen un permiso nuevo
— exigen relación directa con la Tarea (ser el responsable actual, directo
o delegado vigente), mismo criterio que `puede_resolver_ticket`/
`puede_tomar` en Tickets: `tareas.gestionar` es una función supervisora,
nunca sustituye la relación operacional real (`tareas.gestionar no permite
completar trabajo ajeno`, instrucción explícita del usuario).

**Alcance GLOBAL únicamente en 3.3** — hallazgo a reportar, no una omisión
silenciosa: a diferencia de `Ticket` (que congela `TicketContextoAtencion`
desde el Área/Unidad configurada en su Servicio), `Tarea` no tiene ningún
Área/Unidad propia ni heredada documentada (W.2 confirmó, contra el Excel,
que RQF-071–077/CU-021–023 no las mencionan). Derivar un alcance AREA/UNIDAD
desde `equipo_responsable`/`usuario_responsable` sería una extrapolación sin
respaldo documental — se deja fuera en vez de inventarla; si se necesita, es
un incremento futuro con su propio requisito. `usuario_tiene_permiso` se
llama siempre sin `area`/`unidad_negocio`, consultando exclusivamente el
alcance GLOBAL, tal como ya lo documenta `apps.core.autorizacion`.
"""

from django.utils import timezone

from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import MiembroEquipo
from apps.tareas.models import DelegacionTarea, Tarea

PERMISO_CONSULTAR = "tareas.consultar"
PERMISO_GESTIONAR = "tareas.gestionar"


def _autenticado(usuario):
    return bool(getattr(usuario, "is_authenticated", False))


def es_creadora(usuario, tarea):
    return _autenticado(usuario) and tarea.creada_por_id == usuario.id


def es_responsable_directo(usuario, tarea):
    if not _autenticado(usuario):
        return False
    if tarea.usuario_responsable_id == usuario.id:
        return True
    if tarea.equipo_responsable_id is None:
        return False
    return MiembroEquipo.objects.filter(
        equipo_id=tarea.equipo_responsable_id, usuario=usuario, activo=True
    ).exists()


def delegado_actual(tarea, *, ahora=None):
    """Delegado con vigencia activa AHORA para `tarea`, o `None`. La
    "vigencia resuelve el problema" (W.1): no hay estado propio de
    `DelegacionTarea` que consultar, solo la ventana `[desde, hasta)`."""
    ahora = ahora or timezone.now()
    delegacion = (
        DelegacionTarea.objects.filter(tarea=tarea, desde__lte=ahora, hasta__gt=ahora)
        .order_by("-desde")
        .first()
    )
    return delegacion.delegado_a if delegacion else None


def es_delegada_actual(usuario, tarea):
    if not _autenticado(usuario):
        return False
    delegado = delegado_actual(tarea)
    return delegado is not None and delegado.id == usuario.id


def es_responsable_actual(usuario, tarea):
    """Relación operacional vigente: responsable directo (usuario o
    miembro de su equipo) O delegado con vigencia activa — durante la
    delegación, el delegado puede actuar como si fuera el responsable, sin
    que este cambie formalmente (W.1)."""
    return es_responsable_directo(usuario, tarea) or es_delegada_actual(usuario, tarea)


def puede_consultar_tarea(usuario, tarea):
    return (
        es_creadora(usuario, tarea)
        or es_responsable_actual(usuario, tarea)
        or usuario_tiene_permiso(usuario, PERMISO_CONSULTAR)
        or usuario_tiene_permiso(usuario, PERMISO_GESTIONAR)
    )


def puede_tomar_tarea(usuario, tarea):
    """TOMAR = autoasignación de una Tarea PENDIENTE sin responsable
    directo. Además de `tareas.gestionar` global, un miembro activo del
    `equipo_responsable` ya configurado puede tomarla para sí (mismo
    criterio que `usuario_es_responsable_configurado` en Tickets —
    RQF-077, "tareas del equipo")."""
    if tarea.estado == Tarea.Estado.COMPLETADA or tarea.usuario_responsable_id is not None:
        return False
    if usuario_tiene_permiso(usuario, PERMISO_GESTIONAR):
        return True
    if tarea.equipo_responsable_id and _autenticado(usuario):
        return MiembroEquipo.objects.filter(
            equipo_id=tarea.equipo_responsable_id, usuario=usuario, activo=True
        ).exists()
    return False


def puede_asignar_tarea(usuario, tarea):
    """ASIGNAR = asignar a un tercero una Tarea sin responsable directo
    todavía — función supervisora, exige `tareas.gestionar`."""
    if tarea.estado == Tarea.Estado.COMPLETADA or tarea.usuario_responsable_id is not None:
        return False
    return usuario_tiene_permiso(usuario, PERMISO_GESTIONAR)


def puede_reasignar_tarea(usuario, tarea):
    """REASIGNAR = cambiar responsable de una Tarea que ya lo tiene.
    `tareas.gestionar`, o el propio responsable actual entregando su
    Tarea (mismo criterio que `puede_reasignar` en Tickets)."""
    if tarea.estado == Tarea.Estado.COMPLETADA:
        return False
    return usuario_tiene_permiso(usuario, PERMISO_GESTIONAR) or es_responsable_actual(usuario, tarea)


def puede_delegar_tarea(usuario, tarea):
    """DELEGAR — CU-022, actor "Gestor / Ejecutor autorizado": misma
    población que reasignar (`tareas.gestionar`, o el responsable directo
    delegando su propio trabajo)."""
    if tarea.estado == Tarea.Estado.COMPLETADA:
        return False
    return usuario_tiene_permiso(usuario, PERMISO_GESTIONAR) or es_responsable_directo(usuario, tarea)


def puede_iniciar_tarea(usuario, tarea):
    """INICIAR (RQF-073) — ejecutar el trabajo, no supervisarlo:
    exclusivamente quien tiene relación operacional actual. Tener
    `tareas.gestionar` sin ser responsable/delegado NO basta (mismo
    criterio que `puede_resolver_ticket`: `tareas.gestionar` no permite
    completar/iniciar trabajo ajeno)."""
    if tarea.estado != Tarea.Estado.PENDIENTE:
        return False
    return es_responsable_actual(usuario, tarea)


def puede_completar_tarea(usuario, tarea):
    """COMPLETAR — mismo criterio que `puede_iniciar_tarea`: exclusivamente
    responsable actual (directo o delegado), nunca solo por
    `tareas.gestionar`. RQF-073 no exige pasar por EN_PROGRESO antes
    (ninguna RN lo impone) — se permite completar directamente desde
    PENDIENTE."""
    if tarea.estado not in (Tarea.Estado.PENDIENTE, Tarea.Estado.EN_PROGRESO):
        return False
    return es_responsable_actual(usuario, tarea)


def puede_comentar_tarea(usuario, tarea):
    """RQF-076 — creador o responsable actual, mientras la Tarea siga
    abierta a interacción (no COMPLETADA), mismo criterio que
    `puede_comentar_ticket`."""
    if tarea.estado == Tarea.Estado.COMPLETADA:
        return False
    return es_creadora(usuario, tarea) or es_responsable_actual(usuario, tarea)


def puede_crear_subtarea(usuario, tarea):
    """CU-023/RQF-074 — solo si `tarea.permite_subtareas` y `tarea` no es
    ya, ella misma, una subtarea (V1: un solo nivel, W.5)."""
    if not tarea.permite_subtareas or tarea.tarea_padre_id is not None:
        return False
    if tarea.estado == Tarea.Estado.COMPLETADA:
        return False
    return es_responsable_actual(usuario, tarea) or usuario_tiene_permiso(usuario, PERMISO_GESTIONAR)
