"""Entrega formal al solicitante y cierre del Ticket — Sprint 4.5.

Completa el ciclo PRODUCIR → ENTREGAR → ACEPTAR/OBSERVAR → CERRAR sin
agregar estados a `Ticket` ni tocar el motor de Workflow:

    entregar_ticket     EN_ATENCION → RESUELTO   (transición RESOLVER existente)
    aceptar_entrega     RESUELTO    → CERRADO    (transición CERRAR existente)
    observar_entrega    RESUELTO    → EN_ATENCION (retorno ya existente,
                                                  iniciado por el solicitante)
    cerrar_entrega_vencida  RESUELTO → CERRADO   (actor: Sistema)

Entregable satisfecho ≠ entrega formal (`EntregaTicket`) ≠ aceptación del
solicitante ≠ Ticket cerrado. La respuesta del solicitante NO es una
Aprobación de Workflow; la `InstanciaWorkflow` no se toca (ni se reinicia) al
observar — el ticket simplemente continúa en atención.

Concurrencia: toda operación bloquea PRIMERO la fila del Ticket y luego la
entrega (mismo orden en todas, sin riesgo de deadlock), compatible con los
locks de `apps.tickets.entregables`/`operaciones`. Bajo el lock se revalida el
estado: aceptar vs cierre automático, observar vs cierre automático y doble
entrega resuelven con un solo ganador; el perdedor falla con un mensaje claro
(usuario) o es un no-op (Sistema). Respaldo en BD: a lo sumo una entrega
PENDIENTE por ticket (`uq_entrega_pendiente_por_ticket`).
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.catalogo.models import DefinicionEntregable, Servicio
from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria
from apps.tickets import historial
from apps.tickets.autorizacion import puede_escribir_entregables_finales, puede_responder_entrega
from apps.tickets.estados import exigir_transicion
from apps.tickets.models import (
    EntregaTicket,
    HistorialTicket,
    ResultadoEntregaTicket,
    SolicitudInformacion,
    Ticket,
)
from apps.tickets.operaciones import _auditar_cambio_estado, _transicionar_a_cerrado
from apps.workflows.models import InstanciaWorkflow

CAUSA_ACEPTACION = "ACEPTACION_SOLICITANTE"
CAUSA_VENCIMIENTO = "VENCIMIENTO_SIN_RESPUESTA"
CAUSA_CIERRE_DIRECTO = "CIERRE_DIRECTO"


def _auditar_entrega(entrega, actor, anterior, nuevo, accion):
    registrar_evento(
        accion=accion, instancia=entrega,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=nuevo,
    )


def _trabajo_interno_en_curso(ticket):
    """La entrega comienza DESPUÉS del trabajo interno: mientras la ejecución
    (Workflow) del ticket siga en curso o en espera de una tarea/aprobación,
    no se entrega. Solo se consulta su estado — nunca se modifica."""
    instancia_id = ticket.instancia_workflow_id
    if instancia_id is None:
        return False
    return InstanciaWorkflow.objects.filter(
        pk=instancia_id,
        estado__in=(InstanciaWorkflow.Estado.EN_EJECUCION, InstanciaWorkflow.Estado.EN_ESPERA),
    ).exists()


def _archivos_vigentes(entregable):
    # Mismo criterio que `EntregableTicket.satisfecho` para ARCHIVO.
    return entregable.archivos.filter(retirado_en__isnull=True, tamano_bytes__gt=0).exclude(archivo="")


def _congelar_resultados(entrega, entregables):
    """Snapshot de lo que se entrega AHORA (solo entregables satisfechos)."""
    resumen = []
    for entregable in entregables:
        if not entregable.satisfecho:
            continue
        es_texto = entregable.tipo == DefinicionEntregable.Tipo.TEXTO
        es_enlace = entregable.tipo == DefinicionEntregable.Tipo.ENLACE
        es_confirmacion = entregable.tipo == DefinicionEntregable.Tipo.CONFIRMACION
        resultado = ResultadoEntregaTicket.objects.create(
            entrega=entrega, entregable=entregable, nombre=entregable.nombre, tipo=entregable.tipo,
            obligatorio=entregable.obligatorio, orden=entregable.orden,
            texto=entregable.texto if es_texto else "",
            enlace=entregable.enlace if es_enlace else "",
            confirmado_por=entregable.confirmado_por if es_confirmacion else None,
            confirmado_en=entregable.confirmado_en if es_confirmacion else None,
        )
        adjuntos = []
        if entregable.tipo == DefinicionEntregable.Tipo.ARCHIVO:
            adjuntos = list(_archivos_vigentes(entregable))
            resultado.adjuntos.set(adjuntos)
        resumen.append(
            {"entregable_id": entregable.pk, "nombre": entregable.nombre, "tipo": entregable.tipo,
             "adjunto_ids": [a.pk for a in adjuntos]}
        )
    return resumen


@transaction.atomic
def entregar_ticket(ticket, actor):
    """Entrega FORMAL de los resultados al solicitante. Solo el responsable
    individual actual, con el ticket EN_ATENCION, la política congelada y
    todos los entregables obligatorios satisfechos. Crea una `EntregaTicket`
    (nueva por ciclo) con su snapshot y transiciona RESUELTO. Con la política
    de cierre directo, cierra el ticket en la misma transacción."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        raise ValidationError("Solo un ticket EN_ATENCION puede entregarse.")
    if not ticket.entrega_politica:
        raise ValidationError("Este ticket no tiene entrega formal: se resuelve por el flujo habitual.")
    if not puede_escribir_entregables_finales(actor, ticket):
        raise PermissionDenied("Solo el responsable individual actual puede entregar este ticket.")
    if ticket.solicitudes_informacion.filter(estado=SolicitudInformacion.Estado.PENDIENTE).exists():
        raise ValidationError("No es posible entregar mientras existan solicitudes de información pendientes.")
    if _trabajo_interno_en_curso(ticket):
        raise ValidationError("El trabajo interno de este ticket todavía no ha terminado.")

    entregables = list(ticket.entregables.all())
    pendientes = [e.nombre for e in entregables if e.obligatorio and not e.satisfecho]
    if pendientes:
        raise ValidationError("Faltan entregables obligatorios: " + ", ".join(pendientes) + ".")

    ahora = timezone.now()
    politica = ticket.entrega_politica
    es_directo = politica == Servicio.PoliticaEntrega.CIERRE_DIRECTO
    if not es_directo and not ticket.entrega_dias_observacion:
        raise ValidationError("La política de entrega de este ticket no define el periodo de observaciones.")
    vence_en = None if es_directo else ahora + timedelta(days=ticket.entrega_dias_observacion)
    numero = (ticket.entregas.aggregate(ultimo=Max("numero"))["ultimo"] or 0) + 1

    entrega = EntregaTicket.objects.create(
        ticket=ticket, numero=numero, entregada_por=actor, entregada_en=ahora, politica=politica,
        dias_observacion=None if es_directo else ticket.entrega_dias_observacion, vence_en=vence_en,
        estado=EntregaTicket.Estado.CERRADA_SIN_RESPUESTA if es_directo else EntregaTicket.Estado.PENDIENTE,
        resuelta_en=ahora if es_directo else None,
    )
    resultados = _congelar_resultados(entrega, entregables)

    estado_anterior = ticket.estado
    exigir_transicion(ticket, "RESOLVER")
    ticket.save()
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.ENTREGADO, actor,
        estado_anterior=estado_anterior, estado_nuevo=ticket.estado,
        entrega_id=entrega.pk, numero=numero, politica=politica,
        vence_en=vence_en.isoformat() if vence_en else None,
    )
    _auditar_cambio_estado(ticket, actor, estado_anterior)
    _auditar_entrega(
        entrega, actor, None, {**serializar(entrega), "resultados": resultados}, RegistroAuditoria.Accion.CREAR
    )

    if es_directo:
        _transicionar_a_cerrado(ticket, actor, causa=CAUSA_CIERRE_DIRECTO, entrega_id=entrega.pk)
    return entrega


def _entrega_pendiente_para_responder(ticket, actor):
    """Precondiciones comunes de aceptar/observar (ticket ya bloqueado):
    hay una entrega PENDIENTE, el actor es el solicitante y el plazo no venció.
    La entrega se lee con lock — otra respuesta o el cierre automático
    concurrentes ya la habrían dejado de estar PENDIENTE."""
    entrega = None
    if ticket.estado == Ticket.Estado.RESUELTO:
        entrega = (
            EntregaTicket.objects.select_for_update(of=("self",))
            .select_related("ticket")
            .filter(ticket=ticket, estado=EntregaTicket.Estado.PENDIENTE)
            .first()
        )
    if entrega is None:
        raise ValidationError("No hay una entrega pendiente de respuesta.")
    if not puede_responder_entrega(actor, entrega):
        raise PermissionDenied("Solo el solicitante puede responder sobre la entrega.")
    if entrega.vence_en is not None and timezone.now() >= entrega.vence_en:
        raise ValidationError("El plazo para responder esta entrega ya venció.")
    return entrega


@transaction.atomic
def aceptar_entrega(ticket, actor):
    """El solicitante acepta el resultado: registra actor y fecha y cierra el
    ticket por la transición CERRAR real."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    entrega = _entrega_pendiente_para_responder(ticket, actor)
    ahora = timezone.now()
    entrega.estado = EntregaTicket.Estado.ACEPTADA
    entrega.resuelta_por = actor
    entrega.resuelta_en = ahora
    entrega.save()
    _auditar_entrega(
        entrega, actor, {"estado": EntregaTicket.Estado.PENDIENTE},
        {"estado": entrega.estado, "resuelta_por_id": actor.pk, "resuelta_en": ahora},
        RegistroAuditoria.Accion.ACTUALIZAR,
    )
    _transicionar_a_cerrado(ticket, actor, causa=CAUSA_ACEPTACION, entrega_id=entrega.pk)
    return entrega


@transaction.atomic
def observar_entrega(ticket, actor, comentario):
    """El solicitante presenta observaciones (comentario obligatorio). La
    entrega se conserva intacta como historia y el ticket vuelve a atención
    (RESUELTO → EN_ATENCION) para realizar los ajustes; el mismo responsable
    individual sigue asignado. No reinicia ni altera la InstanciaWorkflow."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    entrega = _entrega_pendiente_para_responder(ticket, actor)
    if not comentario or not comentario.strip():
        raise ValidationError("Las observaciones no pueden estar vacías.")
    ahora = timezone.now()
    entrega.estado = EntregaTicket.Estado.OBSERVADA
    entrega.resuelta_por = actor
    entrega.resuelta_en = ahora
    entrega.observaciones = comentario.strip()
    entrega.save()
    _auditar_entrega(
        entrega, actor, {"estado": EntregaTicket.Estado.PENDIENTE},
        {"estado": entrega.estado, "resuelta_por_id": actor.pk, "resuelta_en": ahora,
         "observaciones": entrega.observaciones},
        RegistroAuditoria.Accion.ACTUALIZAR,
    )
    estado_anterior = ticket.estado
    exigir_transicion(ticket, "REABRIR")
    ticket.save()
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.ENTREGA_OBSERVADA, actor,
        estado_anterior=estado_anterior, estado_nuevo=ticket.estado,
        entrega_id=entrega.pk, numero=entrega.numero,
    )
    _auditar_cambio_estado(ticket, actor, estado_anterior)
    return entrega


@transaction.atomic
def cerrar_entrega_vencida(entrega):
    """Cierre automático (actor: Sistema) de una entrega cuyo periodo de
    observaciones venció sin respuesta. IDEMPOTENTE: si la entrega ya no está
    pendiente, el ticket ya no está RESUELTO o el plazo no ha vencido
    (respondió el solicitante o la procesó otra ejecución), no hace nada y
    devuelve None. Cierra por la misma transición CERRAR que la aceptación."""
    ticket_id = EntregaTicket.objects.values_list("ticket_id", flat=True).get(pk=entrega.pk)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    entrega = EntregaTicket.objects.select_for_update().get(pk=entrega.pk)
    ahora = timezone.now()
    if (
        ticket.estado != Ticket.Estado.RESUELTO
        or entrega.estado != EntregaTicket.Estado.PENDIENTE
        or entrega.vence_en is None
        or entrega.vence_en > ahora
    ):
        return None
    entrega.estado = EntregaTicket.Estado.CERRADA_POR_VENCIMIENTO
    entrega.resuelta_en = ahora
    entrega.save()
    _auditar_entrega(
        entrega, None, {"estado": EntregaTicket.Estado.PENDIENTE},
        {"estado": entrega.estado, "resuelta_en": ahora}, RegistroAuditoria.Accion.ACTUALIZAR,
    )
    _transicionar_a_cerrado(ticket, None, causa=CAUSA_VENCIMIENTO, entrega_id=entrega.pk)
    return entrega
