"""Entregables finales V1 — snapshots, resultados y consultas explícitas.

Orden de escritura: lock de la ejecución de Workflow del Ticket (si la tiene) → lock
del Ticket → comprobación de responsable actual → lectura fresca del entregable.
Compatible con reasignar_ticket, que bloquea la misma fila del Ticket. No modifica
estados ni la semántica del equipo.

4.B1: cada escritura que puede dejar un entregable satisfecho avisa al Workflow
(`_continuar_flujo`) para que un bloque ENTREGABLE en espera continúe. El lock de la
instancia va PRIMERO porque es el orden del motor (instancia → ticket al resolver
actores): así un entregable y una aprobación simultáneos no se cruzan.
"""

import hashlib

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
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow


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


def entregable_esta_satisfecho(entregable):
    """ÚNICA regla de "entregable satisfecho" para el Workflow (4.B1): la del dominio,
    `EntregableTicket.satisfecho` (TEXTO/ENLACE con valor válido, ARCHIVO con al menos un
    archivo vigente, CONFIRMACION confirmada). Relee la fila: nunca decide con un objeto
    en memoria desactualizado. Satisfecho ≠ entrega formal (`EntregaTicket`)."""
    return EntregableTicket.objects.get(pk=entregable.pk).satisfecho


def _huella(valor):
    return hashlib.sha256((valor or "").strip().encode("utf-8")).hexdigest()


def version_vigente_de_entregable(entregable):
    """Identidad (JSON, sin el contenido) de la versión que HOY representa al entregable, para
    saber si cambió entre dos revisiones. No es un sistema de versiones: reutiliza lo que el
    dominio ya conserva.

    - ARCHIVO: los archivos vigentes (`Adjunto` nunca se borra; retirar es lógico). Una versión
      nueva es un archivo nuevo; la anterior sigue ahí, retirada o no.
    - TEXTO / ENLACE: huella del valor actual (el valor anterior queda en la auditoría).
    - CONFIRMACION: cuándo se confirmó (no es revisable: ver `TIPOS_ENTREGABLE_REVISABLES`)."""
    entregable = EntregableTicket.objects.get(pk=entregable.pk)
    if entregable.tipo == DefinicionEntregable.Tipo.ARCHIVO:
        ids = entregable.archivos.filter(retirado_en__isnull=True, tamano_bytes__gt=0).exclude(archivo="")
        return {"tipo": entregable.tipo, "adjuntos": sorted(ids.values_list("pk", flat=True))}
    if entregable.tipo == DefinicionEntregable.Tipo.TEXTO:
        return {"tipo": entregable.tipo, "huella": _huella(entregable.texto)}
    if entregable.tipo == DefinicionEntregable.Tipo.ENLACE:
        return {"tipo": entregable.tipo, "huella": _huella(entregable.enlace)}
    return {
        "tipo": entregable.tipo,
        "confirmado_en": entregable.confirmado_en.isoformat() if entregable.confirmado_en else None,
    }


def foto_de_revision(instancia_etapa):
    """Si `instancia_etapa` es una APROBACION que revisa un entregable, devuelve qué entregable y
    qué versión del mismo está viendo en este momento (se guarda en el historial de esa
    ejecución al decidir). `None` para una aprobación general."""
    bloque = instancia_etapa.bloque_operativo
    if bloque is None or bloque.entregable_revisado_id is None:
        return None
    definicion_id = bloque.entregable_revisado.definicion_entregable_id
    entregable = EntregableTicket.objects.filter(
        ticket__instancia_workflow_id=instancia_etapa.instancia_workflow_id, definicion_id=definicion_id
    ).first()
    if entregable is None:
        return None
    return {
        "entregable_id": entregable.pk,
        "definicion_id": definicion_id,
        "bloque_entregable_id": bloque.entregable_revisado_id,
        "version": version_vigente_de_entregable(entregable),
    }


def entregable_vigente_para_flujo(entregable):
    """¿El bloque ENTREGABLE puede darse por cumplido con lo que hay ahora? (4.E2)

    Parte de la única regla de «satisfecho» (`EntregableTicket.satisfecho`) y le añade la
    vigencia: si la ÚLTIMA aprobación completada que revisa este entregable lo devolvió o
    rechazó, la versión que vio ya fue observada y no vale otra vez; hace falta una nueva
    (archivo nuevo o valor distinto). Si esa aprobación lo aprobó, o ninguna lo ha revisado,
    basta con que esté satisfecho. Sirve para cualquier número de vueltas: cada una compara
    contra la última revisión, no contra la primera."""
    entregable = EntregableTicket.objects.select_related("ticket").get(pk=entregable.pk)
    if not entregable.satisfecho:
        return False
    instancia_id = entregable.ticket.instancia_workflow_id
    if instancia_id is None:
        return True
    ultima = (
        InstanciaEtapa.objects.filter(
            instancia_workflow_id=instancia_id,
            estado=InstanciaEtapa.Estado.COMPLETADA,
            bloque_operativo__tipo="APROBACION",
            bloque_operativo__entregable_revisado__definicion_entregable_id=entregable.definicion_id,
        )
        .select_related("transicion_bloque_tomada")
        .order_by("-orden")
        .first()
    )
    if ultima is None:
        return True
    transicion = ultima.transicion_bloque_tomada
    if transicion is None or transicion.resultado_aprobacion == "APROBADA":
        return True
    revisada = ((ultima.resultado or {}).get("revision") or {}).get("version")
    if revisada is None:
        return True
    return version_vigente_de_entregable(entregable) != revisada


def _continuar_flujo(entregable):
    """Avisa al Workflow que `entregable` puede haber quedado satisfecho. Idempotente: si
    ningún bloque ENTREGABLE lo espera, no hace nada."""
    from apps.workflows.integracion import continuar_por_entregable

    continuar_por_entregable(EntregableTicket.objects.select_related("ticket").get(pk=entregable.pk))


def _bloquear_y_autorizar(entregable, actor):
    ticket_id, instancia_id = EntregableTicket.objects.values_list(
        "ticket_id", "ticket__instancia_workflow_id"
    ).get(pk=entregable.pk)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
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
        _continuar_flujo(entregable)
        return entregable
    setattr(entregable, campo, valor)
    entregable.registrado_por = actor
    entregable.full_clean()
    entregable.save()
    _auditar(entregable, actor, anterior, {campo: valor})
    _continuar_flujo(entregable)
    return entregable


@transaction.atomic
def confirmar_entregable(entregable, actor):
    entregable = _bloquear_y_autorizar(entregable, actor)
    if entregable.tipo != DefinicionEntregable.Tipo.CONFIRMACION:
        raise ValidationError("Solo un entregable de confirmación puede confirmarse.")
    if entregable.confirmado_en is not None:
        _continuar_flujo(entregable)
        return entregable
    entregable.confirmado_por = actor
    entregable.confirmado_en = timezone.now()
    entregable.registrado_por = actor
    entregable.save()
    _auditar(entregable, actor, {"confirmado_por_id": None, "confirmado_en": None}, {
        "confirmado_por_id": actor.pk, "confirmado_en": entregable.confirmado_en,
    })
    _continuar_flujo(entregable)
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
    _continuar_flujo(entregable)
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
