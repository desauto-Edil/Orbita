"""Operaciones de dominio de Tareas — Sprint 3.3 (CU-021/022/023).

Mismo patrón de concurrencia que `apps/tickets/operaciones.py` y
`apps/workflows/motor.py`: `@transaction.atomic` + `select_for_update()` +
revalidación de estado bajo lock antes de mutar — todo Postgres, sin locks
de Redis (instrucción explícita).

`crear_tarea` es la única función sin `puede_*` interno: es una primitiva
de dominio (igual criterio que `apps.workflows.motor.iniciar_workflow` en
3.2) — ningún CU documenta un flujo de "crear tarea manual" en 3.3, así que
no hay una autorización de creación que exigir aquí; quien la invoque (la
Strategy del motor, o un futuro caller de 3.UI) es responsable de decidir
si procede. El resto de operaciones SÍ tiene un CU/actor concreto
(CU-021/022/023) y por eso SÍ valida `puede_*` internamente, mismo criterio
que `apps/tickets/operaciones.py`.

`HistorialTarea` (trazabilidad operacional visible) y `RegistroAuditoria`
(auditoría transversal) se alimentan juntos en cada cambio de responsable o
estado — no son sustitutos, mismo criterio ya establecido para Tickets.

Este módulo no importa nada de `apps.workflows` — la integración con el
motor vive del lado de `apps/workflows/integracion.py` (corrección
arquitectónica aprobada), nunca aquí.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tareas.autorizacion import (
    puede_asignar_tarea,
    puede_comentar_tarea,
    puede_completar_tarea,
    puede_crear_subtarea,
    puede_delegar_tarea,
    puede_iniciar_tarea,
    puede_reasignar_tarea,
    puede_tomar_tarea,
)
from apps.tareas.models import AdjuntoTarea, ComentarioTarea, DelegacionTarea, HistorialTarea, Tarea


def _registrar_historial(tarea, tipo_evento, actor, **datos):
    return HistorialTarea.objects.create(
        tarea=tarea, tipo_evento=tipo_evento, actor=actor, datos=datos or None
    )


def _auditar_cambio_responsable(
    tarea, actor, *, usuario_anterior_id, usuario_nuevo_id, equipo_anterior_id, equipo_nuevo_id
):
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=tarea,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={"usuario_responsable_id": usuario_anterior_id, "equipo_responsable_id": equipo_anterior_id},
        datos_nuevos={"usuario_responsable_id": usuario_nuevo_id, "equipo_responsable_id": equipo_nuevo_id},
    )


def _auditar_cambio_estado(tarea, actor, estado_anterior):
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=tarea,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={"estado": estado_anterior},
        datos_nuevos={"estado": tarea.estado},
    )


@transaction.atomic
def crear_tarea(
    *,
    titulo,
    descripcion="",
    origen=Tarea.Origen.MANUAL,
    creada_por=None,
    usuario_responsable=None,
    equipo_responsable=None,
    fecha_limite=None,
    permite_subtareas=False,
    tarea_padre=None,
):
    if origen == Tarea.Origen.SISTEMA and creada_por is not None:
        raise ValueError("origen=SISTEMA no admite creada_por (no se inventa un usuario técnico, W.9).")

    tarea = Tarea.objects.create(
        titulo=titulo,
        descripcion=descripcion,
        origen=origen,
        creada_por=creada_por,
        usuario_responsable=usuario_responsable,
        equipo_responsable=equipo_responsable,
        fecha_limite=fecha_limite,
        permite_subtareas=permite_subtareas,
        tarea_padre=tarea_padre,
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=tarea,
        # `RegistroAuditoria.Origen` describe si un actor humano concreto
        # hizo la acción — se deriva de `creada_por`, no de `Tarea.origen`
        # (una Tarea MANUAL sin `creada_por`, p.ej. creada por una prueba o
        # un futuro caller que no rastrea un humano concreto, también audita
        # como SISTEMA; `origen=SISTEMA` en Tarea ya garantiza `creada_por`
        # nulo, así que ambos casos coinciden con la constraint real).
        origen=(RegistroAuditoria.Origen.USUARIO if creada_por is not None else RegistroAuditoria.Origen.SISTEMA),
        usuario=creada_por,
        datos_anteriores=None,
        datos_nuevos={"titulo": tarea.titulo, "estado": tarea.estado, "origen": tarea.origen},
    )
    return tarea


@transaction.atomic
def tomar_tarea(tarea, actor):
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.usuario_responsable_id is not None:
        raise ValidationError("Esta tarea ya tiene un responsable directo.")
    if not puede_tomar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para tomar esta tarea.")

    usuario_anterior_id = tarea.usuario_responsable_id
    equipo_anterior_id = tarea.equipo_responsable_id
    tarea.usuario_responsable = actor
    tarea.save(update_fields=["usuario_responsable", "actualizado_en"])

    _registrar_historial(tarea, HistorialTarea.TipoEvento.TOMADA, actor)
    _auditar_cambio_responsable(
        tarea,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_nuevo_id=tarea.usuario_responsable_id,
        equipo_anterior_id=equipo_anterior_id,
        equipo_nuevo_id=tarea.equipo_responsable_id,
    )
    return tarea


@transaction.atomic
def asignar_tarea(tarea, actor, *, usuario=None, equipo=None):
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.usuario_responsable_id is not None:
        raise ValidationError("Esta tarea ya tiene un responsable directo: use reasignar_tarea.")
    if usuario is None and equipo is None:
        raise ValidationError("Debe indicar un usuario y/o un equipo para asignar.")
    if not puede_asignar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para asignar esta tarea.")

    usuario_anterior_id = tarea.usuario_responsable_id
    equipo_anterior_id = tarea.equipo_responsable_id
    if equipo is not None:
        tarea.equipo_responsable = equipo
    if usuario is not None:
        tarea.usuario_responsable = usuario
    tarea.save(update_fields=["usuario_responsable", "equipo_responsable", "actualizado_en"])

    _registrar_historial(
        tarea,
        HistorialTarea.TipoEvento.ASIGNADA,
        actor,
        usuario_id=usuario.id if usuario else None,
        equipo_id=equipo.id if equipo else None,
    )
    _auditar_cambio_responsable(
        tarea,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_nuevo_id=tarea.usuario_responsable_id,
        equipo_anterior_id=equipo_anterior_id,
        equipo_nuevo_id=tarea.equipo_responsable_id,
    )
    return tarea


@transaction.atomic
def asignar_por_flujo(tarea, usuario, actor):
    """4.F3 — una Tarea creada por el flujo para «el responsable del Ticket» antes de que el
    Ticket tuviera responsable queda sin dueño; cuando alguien toma o recibe el Ticket, la
    integración (`apps.workflows.integracion`) se la entrega a esa persona. No es una asignación
    discrecional (no exige `tareas.gestionar`): solo traslada una responsabilidad que ya se
    decidió en el Ticket, queda en el historial y en la auditoría con quien actuó, y nunca
    pisa a un responsable existente."""
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.estado == Tarea.Estado.COMPLETADA or tarea.usuario_responsable_id is not None:
        return tarea
    usuario_anterior_id, equipo_anterior_id = tarea.usuario_responsable_id, tarea.equipo_responsable_id
    tarea.usuario_responsable = usuario
    tarea.save(update_fields=["usuario_responsable", "actualizado_en"])
    _registrar_historial(
        tarea, HistorialTarea.TipoEvento.ASIGNADA, actor, usuario_id=usuario.id, equipo_id=None, causa="TICKET_TOMADO"
    )
    _auditar_cambio_responsable(
        tarea, actor, usuario_anterior_id=usuario_anterior_id, usuario_nuevo_id=usuario.id,
        equipo_anterior_id=equipo_anterior_id, equipo_nuevo_id=tarea.equipo_responsable_id,
    )
    return tarea


@transaction.atomic
def reasignar_tarea(tarea, actor, *, usuario=None, equipo=None):
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.estado == Tarea.Estado.COMPLETADA:
        raise ValidationError("No se puede reasignar una tarea ya completada.")
    if usuario is None and equipo is None:
        raise ValidationError("Debe indicar un usuario y/o un equipo para reasignar.")
    if not puede_reasignar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para reasignar esta tarea.")

    usuario_anterior_id = tarea.usuario_responsable_id
    equipo_anterior_id = tarea.equipo_responsable_id
    if equipo is not None:
        tarea.equipo_responsable = equipo
    if usuario is not None:
        tarea.usuario_responsable = usuario
    tarea.save(update_fields=["usuario_responsable", "equipo_responsable", "actualizado_en"])

    _registrar_historial(
        tarea,
        HistorialTarea.TipoEvento.REASIGNADA,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_id=usuario.id if usuario else None,
        equipo_anterior_id=equipo_anterior_id,
        equipo_id=equipo.id if equipo else None,
    )
    _auditar_cambio_responsable(
        tarea,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_nuevo_id=tarea.usuario_responsable_id,
        equipo_anterior_id=equipo_anterior_id,
        equipo_nuevo_id=tarea.equipo_responsable_id,
    )
    return tarea


@transaction.atomic
def delegar_tarea(tarea, actor, *, delegado_a, desde, hasta, motivo=""):
    """CU-022/RQF-075, W.1 — NO modifica el responsable formal de `tarea`.

    Bloquea la fila de `Tarea` (no la de `DelegacionTarea`, que todavía no
    existe) para serializar intentos concurrentes de delegar la MISMA
    tarea — evita el "no solapamiento ambiguo" exigido explícitamente: dos
    delegaciones con vigencias que se crucen dejarían sin resolver "quién
    es el delegado ahora mismo"."""
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.estado == Tarea.Estado.COMPLETADA:
        raise ValidationError("No se puede delegar una tarea ya completada.")
    if not puede_delegar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para delegar esta tarea.")

    solapa = DelegacionTarea.objects.filter(tarea=tarea, desde__lt=hasta, hasta__gt=desde).exists()
    if solapa:
        raise ValidationError(
            "Ya existe una delegación vigente de esta tarea que se solapa con el período indicado."
        )

    delegacion = DelegacionTarea(
        tarea=tarea, delegado_a=delegado_a, delegada_por=actor, desde=desde, hasta=hasta, motivo=motivo
    )
    delegacion.full_clean()
    delegacion.save()

    _registrar_historial(
        tarea,
        HistorialTarea.TipoEvento.DELEGADA,
        actor,
        delegado_a_id=delegado_a.id,
        desde=desde.isoformat(),
        hasta=hasta.isoformat(),
    )
    return delegacion


@transaction.atomic
def iniciar_tarea(tarea, actor):
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.estado != Tarea.Estado.PENDIENTE:
        raise ValidationError("Solo una tarea PENDIENTE puede iniciarse.")
    if not puede_iniciar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para iniciar esta tarea.")

    estado_anterior = tarea.estado
    tarea.estado = Tarea.Estado.EN_PROGRESO
    tarea.iniciada_en = timezone.now()
    tarea.save(update_fields=["estado", "iniciada_en", "actualizado_en"])

    _registrar_historial(tarea, HistorialTarea.TipoEvento.INICIADA, actor)
    _auditar_cambio_estado(tarea, actor, estado_anterior)
    return tarea


@transaction.atomic
def completar_tarea(tarea, actor):
    """CU-021/RN-024. No conoce Workflow: si `tarea` es la principal de una
    `InstanciaEtapa`, quien deba continuar el motor debe llamar a
    `apps.workflows.integracion.completar_tarea_workflow`, no a esta
    función directamente (W.6/corrección arquitectónica aprobada) — esta
    función por sí sola nunca reanuda nada."""
    tarea = Tarea.objects.select_for_update().get(pk=tarea.pk)
    if tarea.estado not in (Tarea.Estado.PENDIENTE, Tarea.Estado.EN_PROGRESO):
        raise ValidationError("Solo una tarea PENDIENTE o EN_PROGRESO puede completarse.")
    if not puede_completar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para completar esta tarea.")
    if tarea.subtareas.exclude(estado=Tarea.Estado.COMPLETADA).exists():
        raise ValidationError("No se puede completar una tarea con subtareas pendientes (W.6).")

    estado_anterior = tarea.estado
    tarea.estado = Tarea.Estado.COMPLETADA
    tarea.completada_por = actor
    tarea.completada_en = timezone.now()
    tarea.save(update_fields=["estado", "completada_por", "completada_en", "actualizado_en"])

    _registrar_historial(tarea, HistorialTarea.TipoEvento.COMPLETADA, actor)
    _auditar_cambio_estado(tarea, actor, estado_anterior)
    return tarea


@transaction.atomic
def crear_subtarea(tarea_padre, actor, *, titulo, descripcion="", usuario_responsable=None, equipo_responsable=None, fecha_limite=None):
    """CU-023/RQF-074 — "cuando la configuración lo permita"
    (`tarea_padre.permite_subtareas`). Una subtarea nunca hereda
    `permite_subtareas` (queda en `False`: V1 es de un solo nivel, W.5) ni
    queda vinculada a ningún Workflow por sí misma (solo la tarea principal
    que una Strategy creó puede estarlo)."""
    if not puede_crear_subtarea(actor, tarea_padre):
        raise PermissionDenied("No tiene autorización para crear subtareas de esta tarea.")

    return crear_tarea(
        titulo=titulo,
        descripcion=descripcion,
        origen=Tarea.Origen.MANUAL,
        creada_por=actor,
        usuario_responsable=usuario_responsable,
        equipo_responsable=equipo_responsable,
        fecha_limite=fecha_limite,
        permite_subtareas=False,
        tarea_padre=tarea_padre,
    )


def _validar_archivo_tecnico(archivo_subido):
    """Mismo control técnico mínimo que `apps.tickets.operaciones` — sin
    tamaño/extensión inventados (ningún RQF/RNF de Tarea los documenta)."""
    if not archivo_subido or not archivo_subido.name:
        raise ValidationError("Debe seleccionar un archivo.")
    if archivo_subido.size == 0:
        raise ValidationError("El archivo está vacío.")


@transaction.atomic
def comentar_tarea(tarea, actor, contenido):
    if not puede_comentar_tarea(actor, tarea):
        raise PermissionDenied("No tiene autorización para comentar esta tarea.")
    if not contenido or not contenido.strip():
        raise ValidationError("El comentario no puede estar vacío.")
    return ComentarioTarea.objects.create(tarea=tarea, autor=actor, contenido=contenido.strip())


@transaction.atomic
def adjuntar_evidencia_tarea(actor, archivo_subido, *, tarea=None, comentario=None):
    """RQF-076. Exactamente uno de `tarea`/`comentario` (mismo
    discriminador que `apps.tickets.operaciones._crear_adjunto`)."""
    if (tarea is None) == (comentario is None):
        raise ValueError("Debe indicar exactamente uno de tarea o comentario.")
    objetivo = tarea if tarea is not None else comentario.tarea
    if not puede_comentar_tarea(actor, objetivo):
        raise PermissionDenied("No tiene autorización para adjuntar evidencia a esta tarea.")
    _validar_archivo_tecnico(archivo_subido)

    return AdjuntoTarea.objects.create(
        tipo_relacion=AdjuntoTarea.TipoRelacion.TAREA if tarea is not None else AdjuntoTarea.TipoRelacion.COMENTARIO,
        tarea=tarea,
        comentario=comentario,
        archivo=archivo_subido,
        nombre_original=archivo_subido.name,
        tipo_mime=getattr(archivo_subido, "content_type", "") or "",
        tamano_bytes=archivo_subido.size,
        subido_por=actor,
    )
