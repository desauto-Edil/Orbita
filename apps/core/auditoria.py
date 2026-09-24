"""Registro de auditoría. CU-040 (RQF-008/116/117/120, RN-033/034).

Captura explícita, sin señales globales, middleware ni thread-local: cada
punto de mutación que conoce al actor (hoy, únicamente los hooks de Django
Admin en `admin.py`) llama directamente a `registrar_evento(...)`. Un
`post_save`/`pre_delete` no tiene acceso a `request` bajo ninguna
circunstancia — resolver "quién lo hizo" solo es posible donde `request`
existe, así que ahí es donde se registra el evento, sin capas intermedias.

Cuando exista un consumidor sin `request` (un proceso Celery, por ejemplo),
llamará a este mismo helper con `origen=RegistroAuditoria.Origen.SISTEMA` y
`usuario=None` explícitamente — no hace falta ningún mecanismo adicional.
"""

from django.contrib.contenttypes.models import ContentType

from apps.core.models.auditoria import RegistroAuditoria

CAMPOS_SENSIBLES = {"password"}


def serializar(instancia):
    """Dict JSON-serializable de los campos concretos de `instancia`.

    Excluye campos sensibles (`CAMPOS_SENSIBLES`) — hoy solo `password` en
    `Usuario`, el único caso real entre los modelos auditados. Los `ForeignKey`
    se guardan como `<campo>_id` (el id crudo), sin resolver ni serializar el
    objeto relacionado: evita recursión y consultas adicionales, y basta para
    reconstruir qué cambió.
    """
    datos = {}
    for campo in instancia._meta.fields:
        if campo.name in CAMPOS_SENSIBLES:
            continue
        datos[campo.name] = campo.value_from_object(instancia)
    return datos


def registrar_evento(*, accion, instancia, origen, usuario=None, datos_anteriores=None, datos_nuevos=None):
    """Crea el `RegistroAuditoria` de un evento ya ocurrido sobre `instancia`.

    `origen`/`usuario` deben ser coherentes con la constraint del modelo
    (`USUARIO` exige `usuario`; `SISTEMA` lo exige nulo) — se valida aquí
    también para fallar con un mensaje claro en vez de un `IntegrityError`
    genérico de base de datos.
    """
    if origen == RegistroAuditoria.Origen.USUARIO and usuario is None:
        raise ValueError("origen=USUARIO exige un usuario explícito.")
    if origen == RegistroAuditoria.Origen.SISTEMA and usuario is not None:
        raise ValueError("origen=SISTEMA no admite usuario.")

    modelo = type(instancia)
    content_type = ContentType.objects.get_for_model(modelo)

    return RegistroAuditoria.objects.create(
        content_type=content_type,
        object_id=instancia.pk,
        modelo=f"{content_type.app_label}.{content_type.model}",
        objeto_repr=str(instancia)[:255],
        accion=accion,
        origen=origen,
        usuario=usuario,
        datos_anteriores=datos_anteriores,
        datos_nuevos=datos_nuevos,
    )


def guardar_formset_auditado(request, formset):
    """Guarda un inline de Django Admin y audita cada alta/baja/cambio.

    Reemplaza una llamada directa a `formset.save()` en `ModelAdmin.save_formset`
    cuando el modelo del inline debe auditarse (RQF-116/120) — extraído aquí
    porque, a partir del segundo inline auditado (`RolPermiso` en 0.4,
    `ServicioVisibilidad`/`ServicioResponsable` en 1.1), repetir la misma
    lógica en cada `ModelAdmin` sería duplicar reglas de negocio.

    Orden crítico: `deleted_forms` (validación, antes de guardar) conserva el
    `pk` intacto; `new_objects`/`changed_objects` son efectos secundarios de
    `formset.save()` y solo existen después de llamarlo. Django limpia el
    `pk` de un objeto tras `.delete()`, así que los eliminados se capturan
    antes y se restauran para poder registrar su identidad.
    """
    modelo = formset.model
    datos_antes = {
        obj.pk: serializar(obj)
        for obj in modelo.objects.filter(
            pk__in=[f.instance.pk for f in formset.forms if f.instance.pk]
        )
    }
    eliminados = [
        (f.instance, f.instance.pk, datos_antes.get(f.instance.pk, serializar(f.instance)))
        for f in formset.deleted_forms
    ]

    formset.save()

    for obj, pk_guardado, datos in eliminados:
        obj.pk = pk_guardado
        registrar_evento(
            accion=RegistroAuditoria.Accion.ELIMINAR,
            instancia=obj,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=request.user,
            datos_anteriores=datos,
            datos_nuevos=None,
        )
    for obj in formset.new_objects:
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR,
            instancia=obj,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=request.user,
            datos_anteriores=None,
            datos_nuevos=serializar(obj),
        )
    for obj, _campos_modificados in formset.changed_objects:
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR,
            instancia=obj,
            origen=RegistroAuditoria.Origen.USUARIO,
            usuario=request.user,
            datos_anteriores=datos_antes.get(obj.pk),
            datos_nuevos=serializar(obj),
        )
