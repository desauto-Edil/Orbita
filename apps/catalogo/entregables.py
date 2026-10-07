"""Configuración de salidas finales — 4.2. Sin versionamiento adicional."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import DefinicionEntregable, Servicio
from apps.core.auditoria import registrar_evento
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import RegistroAuditoria


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para configurar entregables.")


def _datos(definicion):
    return {campo: getattr(definicion, campo) for campo in (
        "servicio_id", "nombre", "descripcion", "tipo", "obligatorio", "orden", "activo",
    )}


@transaction.atomic
def configurar_definicion_entregable(servicio, actor, *, nombre, tipo, descripcion="",
                                     obligatorio=False, orden=0, definicion=None):
    """Alta o edición. El servicio propietario no se puede cambiar.

    El lock del Servicio serializa configuración y congelación: una
    materialización lee el conjunto de definiciones antes o después de
    esta operación, nunca una mezcla de ambas.
    """
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    anterior = None
    if definicion is None:
        definicion = DefinicionEntregable(servicio=servicio)
    else:
        definicion = DefinicionEntregable.objects.get(pk=definicion.pk)
        if definicion.servicio_id != servicio.pk:
            raise ValidationError("La definición no pertenece a este elemento del catálogo.")
        anterior = _datos(definicion)
    definicion.nombre = nombre.strip()
    definicion.descripcion = descripcion
    definicion.tipo = tipo
    definicion.obligatorio = obligatorio
    definicion.orden = orden
    definicion.full_clean()
    if anterior == _datos(definicion):
        return definicion
    definicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR if anterior is None else RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=definicion, origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=_datos(definicion),
    )
    return definicion


@transaction.atomic
def retirar_definicion_entregable(definicion, actor):
    """Retiro lógico: no elimina definiciones utilizadas ni cambia snapshots."""
    _exigir_administracion(actor)
    Servicio.objects.select_for_update().get(pk=definicion.servicio_id)
    definicion = DefinicionEntregable.objects.get(pk=definicion.pk)
    if not definicion.activo:
        return definicion
    # 4.B1: un bloque ENTREGABLE de una configuración vigente o en borrador necesita
    # esta definición activa (los tickets nuevos solo congelan las activas); retirarla
    # dejaría la configuración inválida. Las históricas no cuentan.
    usada_por = definicion.bloques_operativos.filter(
        version__estado__in=("BORRADOR", "ACTIVA")
    ).select_related("version__servicio").first()
    if usada_por is not None:
        raise ValidationError(
            f"No se puede retirar «{definicion.nombre}»: el bloque «{usada_por.nombre}» "
            "de la configuración del flujo la requiere. Quita o cambia ese bloque primero."
        )
    anterior = _datos(definicion)
    definicion.activo = False
    definicion.save(update_fields=["activo", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=_datos(definicion),
    )
    return definicion
