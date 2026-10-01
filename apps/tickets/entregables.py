"""Entregables finales V1 — snapshots, resultados y consultas explícitas.

Orden de escritura: lock del Ticket → comprobación de responsable actual
→ lectura fresca del entregable. Compatible con reasignar_ticket, que
bloquea la misma fila. No modifica estados ni la semántica del equipo.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from apps.catalogo.models import DefinicionEntregable, Servicio
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets.autorizacion import puede_consultar_ticket, puede_escribir_entregables_finales
from apps.tickets.models import Adjunto, EntregableTicket, Ticket
from apps.tickets.operaciones import _crear_adjunto


def _auditar(instancia, actor, anterior, nuevo, accion=RegistroAuditoria.Accion.ACTUALIZAR):
    registrar_evento(
        accion=accion, instancia=instancia, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=nuevo,
    )


@transaction.atomic
def materializar_entregables(ticket):
    """Solo crear_borrador habilita esta operación con el marcador en False.

    Históricos y Tickets ya materializados (incluso con cero expectativas)
    son no-op. No consulta el catálogo para agregar obligaciones a ellos.
    La transacción de creación nunca publica un Ticket a medio materializar.
    """
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.entregables_materializados:
        return list(ticket.entregables.all())
    servicio = Servicio.objects.select_for_update().get(pk=ticket.detalle_servicio.servicio_id)
    nuevos = []
    for definicion in servicio.definiciones_entregables.filter(activo=True):
        nuevos.append(EntregableTicket.objects.create(
            ticket=ticket, definicion=definicion, nombre=definicion.nombre,
            descripcion=definicion.descripcion, tipo=definicion.tipo,
            obligatorio=definicion.obligatorio, orden=definicion.orden,
        ))
    ticket.entregables_materializados = True
    ticket.save(update_fields=["entregables_materializados", "actualizado_en"])
    if nuevos:
        _auditar(ticket, ticket.solicitante, None, {
            "entregables": [{"id": e.pk, "definicion_id": e.definicion_id, "nombre": e.nombre,
                             "descripcion": e.descripcion, "tipo": e.tipo,
                             "obligatorio": e.obligatorio, "orden": e.orden} for e in nuevos],
        })
    return nuevos


def _bloquear_y_autorizar(entregable, actor):
    ticket_id = EntregableTicket.objects.values_list("ticket_id", flat=True).get(pk=entregable.pk)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    if not puede_escribir_entregables_finales(actor, ticket):
        raise PermissionDenied("Solo el responsable individual actual puede modificar entregables de un ticket en atención.")
    return EntregableTicket.objects.get(pk=entregable.pk)


@transaction.atomic
def registrar_resultado_entregable(entregable, actor, valor):
    """TEXTO/ENLACE: reemplazo del valor actual; vacío deja de satisfacer."""
    entregable = _bloquear_y_autorizar(entregable, actor)
    if entregable.tipo not in (DefinicionEntregable.Tipo.TEXTO, DefinicionEntregable.Tipo.ENLACE):
        raise ValidationError("Este tipo requiere archivos o confirmación explícita.")
    if not isinstance(valor, str):
        raise ValidationError("El resultado debe ser texto.")
    valor = valor.strip()
    campo = "texto" if entregable.tipo == DefinicionEntregable.Tipo.TEXTO else "enlace"
    if campo == "enlace" and valor:
        URLValidator(schemes=["http", "https"])(valor)
    anterior = {campo: getattr(entregable, campo)}
    if anterior[campo] == valor:
        return entregable
    setattr(entregable, campo, valor)
    entregable.registrado_por = actor
    entregable.full_clean()
    entregable.save()
    _auditar(entregable, actor, anterior, {campo: valor})
    return entregable


@transaction.atomic
def confirmar_entregable(entregable, actor):
    entregable = _bloquear_y_autorizar(entregable, actor)
    if entregable.tipo != DefinicionEntregable.Tipo.CONFIRMACION:
        raise ValidationError("Solo un entregable de confirmación puede confirmarse.")
    if entregable.confirmado_en is not None:
        return entregable
    entregable.confirmado_por = actor
    entregable.confirmado_en = timezone.now()
    entregable.registrado_por = actor
    entregable.save()
    _auditar(entregable, actor, {"confirmado_por_id": None, "confirmado_en": None}, {
        "confirmado_por_id": actor.pk, "confirmado_en": entregable.confirmado_en,
    })
    return entregable


@transaction.atomic
def adjuntar_archivo_entregable(entregable, actor, archivo_subido):
    entregable = _bloquear_y_autorizar(entregable, actor)
    if entregable.tipo != DefinicionEntregable.Tipo.ARCHIVO:
        raise ValidationError("Solo un entregable ARCHIVO admite archivos.")
    adjunto = None
    try:
        adjunto = _crear_adjunto(Adjunto.TipoRelacion.ENTREGABLE, {"entregable": entregable}, archivo_subido, actor)
        _auditar(adjunto, actor, None, {
            "entregable_id": entregable.pk, "nombre_original": adjunto.nombre_original,
            "tamano_bytes": adjunto.tamano_bytes, "tipo_mime": adjunto.tipo_mime,
        }, accion=RegistroAuditoria.Accion.CREAR)
    except Exception:
        # FileSystemStorage no participa en SQL; limpiar el archivo si la
        # operación falla después de su alta (sin tocar archivos anteriores).
        if adjunto is not None:
            adjunto.archivo.delete(save=False)
        raise
    return adjunto


@transaction.atomic
def retirar_archivo_entregable(adjunto, actor):
    adjunto = Adjunto.objects.get(pk=adjunto.pk)
    if adjunto.tipo_relacion != Adjunto.TipoRelacion.ENTREGABLE:
        raise ValidationError("El archivo no pertenece a un entregable.")
    _bloquear_y_autorizar(adjunto.entregable, actor)
    adjunto.refresh_from_db()
    if adjunto.retirado_en is not None:
        return adjunto
    adjunto.retirado_en = timezone.now()
    adjunto.save(update_fields=["retirado_en", "actualizado_en"])
    _auditar(adjunto, actor, {"retirado_en": None}, {
        "retirado_en": adjunto.retirado_en, "entregable_id": adjunto.entregable_id,
    })
    return adjunto


def entregables_para_ticket(ticket, actor):
    ticket = Ticket.objects.get(pk=ticket.pk)
    if not puede_consultar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para consultar este ticket.")
    return ticket.entregables.all()


def entregables_obligatorios_pendientes(ticket, actor):
    return [e for e in entregables_para_ticket(ticket, actor).filter(obligatorio=True) if not e.satisfecho]
