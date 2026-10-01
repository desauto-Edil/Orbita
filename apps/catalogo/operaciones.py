"""Publicación explícita del catálogo — 4.1 (CU-010, entrada de CU-014).

`activo` conserva su significado y default históricos. Las altas del Admin
nacen inactivas; las escrituras ORM de confianza no se reinterpretan.
La validación es calculada, sin otro estado persistente ni correcciones
retroactivas. Responsables y contextos reutilizan sus modelos actuales.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import FormularioVersion, Servicio
from apps.core.auditoria import registrar_evento
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import RegistroAuditoria
from apps.workflows.models import WorkflowVersion


def validar_ejecucion(servicio):
    """No revalida el formulario: al radicar ya está congelado en el Ticket."""
    workflow = servicio.workflow
    if workflow is None:
        if servicio.tipo == Servicio.Tipo.PROCESO:
            raise ValidationError("El proceso no tiene una ejecución configurada.")
        return
    version = workflow.version_activa
    if version is None or version.estado != WorkflowVersion.Estado.ACTIVA or version.workflow_id != workflow.pk:
        raise ValidationError("La ejecución no tiene una versión activa utilizable.")
    from apps.workflows.validacion import validar_estructura

    errores = validar_estructura(version)
    if errores:
        raise ValidationError(errores)


def validar_ejecucion_proceso(servicio):
    """Nombre conservado para consumidores de 4.1; la validación es común."""
    validar_ejecucion(servicio)


def validar_publicacion(servicio):
    """Solo lectura. No exige responsables/área: no son requisitos de radicación."""
    errores = []
    if servicio.tipo not in Servicio.Tipo.values:
        errores.append("Tipo de catálogo inválido.")
    formulario = servicio.formulario
    version = formulario.version_activa if formulario is not None else None
    if (
        version is None or version.estado != FormularioVersion.Estado.ACTIVA
        or version.formulario_id != formulario.pk
    ):
        errores.append("Se requiere un formulario con una versión ACTIVA propia.")
    try:
        validar_ejecucion(servicio)
    except ValidationError as exc:
        errores.extend(exc.messages)
    if errores:
        raise ValidationError(errores)


def diagnosticar_activos_incompletos():
    """Reporte de solo lectura, incluidos registros históricos. No audita mutaciones inexistentes."""
    for servicio in Servicio.objects.filter(activo=True).select_related(
        "formulario__version_activa", "workflow__version_activa"
    ).order_by("pk"):
        try:
            validar_publicacion(servicio)
        except ValidationError as exc:
            yield {"id": servicio.pk, "nombre": servicio.nombre, "tipo": servicio.tipo, "errores": exc.messages}


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para administrar el catálogo.")


@transaction.atomic
def activar_servicio(servicio, actor):
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    validar_publicacion(servicio)
    if servicio.activo:
        return servicio
    servicio.activo = True
    servicio.save(update_fields=["activo", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"activo": False},
        datos_nuevos={"activo": True, "tipo": servicio.tipo,
                      "formulario_id": servicio.formulario_id, "workflow_id": servicio.workflow_id},
    )
    return servicio


@transaction.atomic
def desactivar_servicio(servicio, actor):
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if not servicio.activo:
        return servicio
    servicio.activo = False
    servicio.save(update_fields=["activo", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"activo": True}, datos_nuevos={"activo": False},
    )
    return servicio
