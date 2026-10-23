"""Datos de inicio diligenciados DENTRO de la ejecución — Fase 2 del modelo configurable de procesos.

En un PROCESO cuyos datos de inicio pertenecen a la ejecución (`Ticket.datos_inicio_en_ejecucion`, congelado
al crear el ticket desde `Servicio.datos_inicio_en_ejecucion`; siempre en un proceso programado) el ticket se
inicia sin exigir el formulario completo, y quien lo atiende lo diligencia después con dos operaciones
distintas sobre el MISMO formulario congelado del ticket (no hay otro formulario, ni campos duplicados, ni
asociación obligatoria a fases o actividades):

- `guardar_avance`: persiste información parcial (formato validado con las Strategies; sin exigir
  obligatorios). No avanza el workflow. Se puede repetir.
- `completar_diligenciamiento`: guarda lo enviado, exige los obligatorios visibles (la misma regla de la
  radicación: `validaciones.validar_para_radicar`) y marca los datos como completos
  (`RespuestaFormulario.completado_en/por`, historial DATOS_COMPLETADOS, auditoría). Desde ese momento las
  respuestas son inmutables, como las de un ticket radicado de siempre.

«Habilitar el avance»: una ACTIVIDAD puede exigir los datos completos para completarse
(`exige_datos_inicio`, `workflows.integracion`) y una DECISION puede consultar `ticket.datos_inicio_completos`.
La radicación de SERVICIOS y de procesos que completan sus datos al iniciar no cambia.

Quién: `autorizacion.puede_diligenciar_datos_inicio` (responsable individual en atención o quien lo inició).
Orden de locks: instancia de Workflow → Ticket (el del motor).
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets import historial
from apps.tickets.autorizacion import puede_diligenciar_datos_inicio
from apps.tickets.models import HistorialTicket, RespuestaFormulario, Ticket
from apps.workflows.models import InstanciaWorkflow


def _sin_datos_de_inicio(ticket):
    """Un Proceso puede no pedir datos de inicio: su versión congelada no tiene campos y no hay nada que
    diligenciar (ver `catalogo.operaciones.asegurar_datos_inicio`)."""
    return not RespuestaFormulario.objects.filter(
        ticket_id=ticket.pk, formulario_version__campos__isnull=False
    ).exists()


def requiere_diligenciamiento(ticket):
    """¿Los datos de inicio de este ticket se diligencian dentro de la ejecución?"""
    return bool(ticket.datos_inicio_en_ejecucion) and not _sin_datos_de_inicio(ticket)


def datos_completos(ticket):
    """¿Los datos de inicio ya están completos? Siempre `True` si se completaron al iniciar (lo de siempre) o si
    el ticket no tiene datos de inicio que responder."""
    if not ticket.datos_inicio_en_ejecucion or _sin_datos_de_inicio(ticket):
        return True
    return RespuestaFormulario.objects.filter(ticket_id=ticket.pk, completado_en__isnull=False).exists()


def pendientes_de(ticket):
    """Etiquetas de los campos obligatorios visibles que todavía faltan (o no son válidos). Solo lee."""
    from apps.tickets.validaciones import validar_para_radicar

    try:
        validar_para_radicar(ticket)
    except ValidationError as exc:
        errores = getattr(exc, "error_dict", None) or {}
        campos = {c.pk: c.etiqueta for c in ticket.respuesta_formulario.formulario_version.campos.all()}
        return [campos.get(campo_id, str(campo_id)) for campo_id in errores]
    return []


def validar_formato(ticket):
    """Lo guardado debe ser válido (formato, opciones, referencias activas) aunque falten obligatorios: lo usa
    la radicación de un proceso con datos en la ejecución."""
    from apps.tickets.validaciones import validar_para_radicar

    try:
        validar_para_radicar(ticket)
    except ValidationError as exc:
        errores = {
            campo_id: mensajes
            for campo_id, mensajes in (getattr(exc, "message_dict", None) or {}).items()
            if mensajes != ["Este campo es obligatorio."]
        }
        if errores:
            raise ValidationError(errores)


def _bloquear(ticket, actor):
    instancia_id = Ticket.objects.values_list("instancia_workflow_id", flat=True).get(pk=ticket.pk)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if not ticket.datos_inicio_en_ejecucion:
        raise ValidationError("Los datos de inicio de este ticket se completaron al iniciarlo.")
    if datos_completos(ticket):
        raise ValidationError("Los datos de inicio ya se completaron.")
    if ticket.estado not in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION):
        raise ValidationError("Este ticket ya no admite cambios en sus datos de inicio.")
    if not puede_diligenciar_datos_inicio(actor, ticket):
        raise PermissionDenied("Solo quien atiende el ticket (o quien lo inició) puede diligenciar sus datos de inicio.")
    return ticket


def _guardar(ticket, actor, respuestas_crudas):
    from apps.tickets.operaciones import _guardar_respuestas

    if not respuestas_crudas:
        return []
    antes = {rc.campo_id: rc.valor for rc in ticket.respuesta_formulario.respuestas_campo.select_related("campo")}
    _guardar_respuestas(ticket, actor, respuestas_crudas)
    despues = {rc.campo_id: rc.valor for rc in ticket.respuesta_formulario.respuestas_campo.select_related("campo")}
    return sorted(campo_id for campo_id in set(antes) | set(despues) if antes.get(campo_id) != despues.get(campo_id))


@transaction.atomic
def guardar_avance(ticket, actor, respuestas_crudas):
    """GUARDAR AVANCE: persiste información parcial del formulario de inicio. No exige obligatorios ni avanza
    el workflow. `respuestas_crudas` es `{campo_id: valor}` (mismo contrato que el borrador)."""
    ticket = _bloquear(ticket, actor)
    cambiados = _guardar(ticket, actor, respuestas_crudas)
    if cambiados:
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=ticket.respuesta_formulario,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor, datos_anteriores=None,
            datos_nuevos={"ticket_id": ticket.pk, "avance_guardado": True, "campos": cambiados},
        )
    return ticket.respuesta_formulario


@transaction.atomic
def completar_diligenciamiento(ticket, actor, respuestas_crudas=None):
    """COMPLETAR DILIGENCIAMIENTO: guarda lo enviado, exige los obligatorios visibles y marca los datos como
    completos. Todo o nada: si falta algo se rechaza sin cambios (la vista guarda antes el avance con
    `guardar_avance`, en su propia transacción, para no perder lo escrito)."""
    from apps.tickets.validaciones import validar_para_radicar

    ticket = _bloquear(ticket, actor)
    _guardar(ticket, actor, respuestas_crudas or {})
    validar_para_radicar(ticket)
    respuesta = RespuestaFormulario.objects.select_for_update().get(ticket=ticket)
    respuesta.completado_en = timezone.now()
    respuesta.completado_por = actor
    respuesta.save(update_fields=["completado_en", "completado_por", "actualizado_en"])
    historial.registrar(ticket, HistorialTicket.TipoEvento.DATOS_COMPLETADOS, actor)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=respuesta, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"completado_en": None},
        datos_nuevos={"ticket_id": ticket.pk, "completado_en": respuesta.completado_en},
    )
    return respuesta
