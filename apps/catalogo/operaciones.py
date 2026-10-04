"""Publicación explícita del catálogo — 4.1 (CU-010, entrada de CU-014).

`activo` conserva su significado y default históricos. Las altas del Admin
nacen inactivas; las escrituras ORM de confianza no se reinterpretan.
La validación es calculada, sin otro estado persistente ni correcciones
retroactivas. Responsables y contextos reutilizan sus modelos actuales.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import (
    Categoria,
    Formulario,
    FormularioVersion,
    Servicio,
    ServicioResponsable,
    ServicioVisibilidad,
)
from apps.core.auditoria import registrar_evento, serializar
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
    if workflow.modo == "PLANTILLA_FASES":
        config = servicio.configuracion_ejecucion_activa
        if config is None or config.estado != "ACTIVA":
            raise ValidationError("La ejecucion no tiene una configuracion operativa activa.")
        if config.workflow_version_id != version.pk:
            raise ValidationError("La configuracion operativa no corresponde a la version activa de la plantilla.")
        from apps.catalogo.configuracion_ejecucion import validar_configuracion_ejecucion

        errores = validar_configuracion_ejecucion(config)
        if errores:
            raise ValidationError(errores)
        return
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


def _datos_generales(servicio):
    return {
        campo: getattr(servicio, campo)
        for campo in ("nombre", "descripcion", "categoria_id", "tipo", "instrucciones", "alcance_visibilidad")
    }


@transaction.atomic
def editar_servicio_general(servicio, actor, *, nombre, descripcion, categoria, tipo, instrucciones, alcance_visibilidad):
    """4.4 (Studio, pestaña General) — único punto de edición de los datos
    empresariales básicos de `Servicio`. No existía una operación de
    dominio para esto (solo Django Admin, que se audita vía su propio
    `AdminAuditableMixin`); el Studio no pasa por Admin, así que necesita
    su propia operación auditada — mismo criterio que el resto del
    proyecto, no una excepción de Studio."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    anterior = _datos_generales(servicio)
    servicio.nombre = nombre
    servicio.descripcion = descripcion
    servicio.categoria = categoria
    servicio.tipo = tipo
    servicio.instrucciones = instrucciones
    servicio.alcance_visibilidad = alcance_visibilidad
    servicio.full_clean()
    nuevo = _datos_generales(servicio)
    if anterior == nuevo:
        return servicio
    servicio.save(
        update_fields=["nombre", "descripcion", "categoria", "tipo", "instrucciones", "alcance_visibilidad", "actualizado_en"]
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )
    return servicio


@transaction.atomic
def asociar_formulario_nuevo(servicio, actor, *, nombre, descripcion=""):
    """4.4 (Studio, pestaña Entrada) — primera vez que un Servicio obtiene
    una Entrada propia: crea el `Formulario` (plantilla transversal, ver
    `apps/catalogo/models/formularios.py`) y lo asocia. No duplica el Form
    Builder: reutiliza sus modelos y deja la creación de la v1 BORRADOR a
    `apps.catalogo.versionamiento.crear_nueva_version`, igual que
    cualquier otro Formulario."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.formulario_id is not None:
        raise ValidationError("Este elemento del catálogo ya tiene un formulario de entrada.")
    formulario = Formulario(nombre=nombre, descripcion=descripcion)
    formulario.full_clean()
    formulario.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=formulario,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(formulario),
    )
    from apps.catalogo.versionamiento import crear_nueva_version

    crear_nueva_version(formulario, actor)
    anterior = {"formulario_id": None}
    servicio.formulario = formulario
    servicio.save(update_fields=["formulario", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos={"formulario_id": formulario.pk},
    )
    return formulario


# --- 4.4.1 — Creación, visibilidad y responsables desde Studio -------------
#
# Hasta 4.4 estas mutaciones solo existían en Django Admin (inlines de
# `ServicioAdmin`, auditadas vía `guardar_formset_auditado`). Studio no
# pasa por Admin, así que necesita sus propias operaciones de dominio
# auditadas — sobre los MISMOS modelos (`Servicio`, `ServicioVisibilidad`,
# `ServicioResponsable`), sin duplicarlos ni crear un estado paralelo.
#
# "Retirar" desactiva (`activo=False`) en vez de borrar la fila: ambos
# modelos ya tienen `activo` y sus UniqueConstraint son parciales sobre
# `activo=True`, justo para permitir retirar y volver a conceder sin perder
# el historial (el Admin, en cambio, borra la fila del inline).


@transaction.atomic
def crear_categoria(actor, *, nombre):
    """Categoría mínima para que Studio no dependa de Admin cuando el catálogo
    todavía no tiene ninguna. No edita ni desactiva categorías existentes."""
    _exigir_administracion(actor)
    nombre = (nombre or "").strip()
    if Categoria.objects.filter(nombre__iexact=nombre).exists():
        raise ValidationError(f"Ya existe una categoría llamada «{nombre}». Selecciónela de la lista.")
    categoria = Categoria(nombre=nombre)
    categoria.full_clean()
    categoria.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=categoria,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(categoria),
    )
    return categoria


@transaction.atomic
def crear_servicio(actor, *, nombre, descripcion="", categoria, tipo, instrucciones=""):
    """Alta de un Servicio/Proceso en BORRADOR (`activo=False`). No crea
    Workflow, Formulario ni Entregables, no concede visibilidad ni responsables
    y no publica: eso lo completa Studio paso a paso. `alcance_visibilidad`
    conserva el default RESTRINGIDO del modelo (RN-038: nada es visible hasta
    concederlo explícitamente)."""
    _exigir_administracion(actor)
    if categoria is None or not categoria.activo:
        raise ValidationError("Seleccione una categoría activa.")
    servicio = Servicio(
        nombre=nombre, descripcion=descripcion, categoria=categoria, tipo=tipo,
        instrucciones=instrucciones, activo=False,
    )
    servicio.full_clean()
    servicio.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(servicio),
    )
    return servicio


def _bloquear_servicio(servicio):
    # Serializa conceder/agregar concurrentes sobre el mismo servicio: el
    # chequeo de duplicado + insert no es atómico por sí solo, y la
    # UniqueConstraint parcial respondería con un IntegrityError opaco.
    return Servicio.objects.select_for_update().get(pk=servicio.pk)


@transaction.atomic
def conceder_visibilidad(servicio, actor, *, tipo_alcance, usuario=None, area=None, unidad_negocio=None):
    """Concede a un usuario, área o unidad la posibilidad de encontrar y
    solicitar un servicio RESTRINGIDO (RQF-032, RN-038). Sin efecto sobre
    PUBLICO_INTERNO — se puede preparar de antemano."""
    _exigir_administracion(actor)
    servicio = _bloquear_servicio(servicio)
    Tipo = ServicioVisibilidad.TipoAlcance
    if tipo_alcance == Tipo.USUARIO and usuario is not None and usuario.is_active:
        filtro, campos = {"usuario": usuario}, {"usuario": usuario}
    elif tipo_alcance == Tipo.AREA and area is not None and area.activo:
        filtro, campos = {"area": area}, {"area": area}
    elif tipo_alcance == Tipo.UNIDAD and unidad_negocio is not None and unidad_negocio.activo:
        filtro, campos = {"unidad_negocio": unidad_negocio}, {"unidad_negocio": unidad_negocio}
    else:
        raise ValidationError("Seleccione un usuario, un área o una unidad de negocio activos.")
    if ServicioVisibilidad.objects.filter(servicio=servicio, tipo_alcance=tipo_alcance, activo=True, **filtro).exists():
        raise ValidationError("Esa visibilidad ya está concedida.")
    concesion = ServicioVisibilidad.objects.create(servicio=servicio, tipo_alcance=tipo_alcance, activo=True, **campos)
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=concesion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(concesion),
    )
    return concesion


@transaction.atomic
def retirar_visibilidad(concesion, actor):
    _exigir_administracion(actor)
    concesion = ServicioVisibilidad.objects.select_for_update().get(pk=concesion.pk)
    if not concesion.activo:
        return concesion
    anterior = serializar(concesion)
    concesion.activo = False
    concesion.save(update_fields=["activo", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=concesion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=serializar(concesion),
    )
    return concesion


@transaction.atomic
def agregar_responsable(servicio, actor, *, tipo_responsable, usuario=None, equipo=None):
    """Configura quién puede ATENDER el servicio (RQF-033). No es el actor de
    una actividad del flujo (eso es Ejecución) ni el responsable actual de un
    Ticket concreto. Atender además exige el permiso `tickets.atender`."""
    _exigir_administracion(actor)
    servicio = _bloquear_servicio(servicio)
    Tipo = ServicioResponsable.TipoResponsable
    if tipo_responsable == Tipo.USUARIO and usuario is not None and usuario.is_active:
        filtro, campos = {"usuario": usuario}, {"usuario": usuario}
    elif tipo_responsable == Tipo.EQUIPO and equipo is not None and equipo.activo:
        filtro, campos = {"equipo": equipo}, {"equipo": equipo}
    else:
        raise ValidationError("Seleccione un usuario o un equipo activos.")
    if ServicioResponsable.objects.filter(
        servicio=servicio, tipo_responsable=tipo_responsable, activo=True, **filtro
    ).exists():
        raise ValidationError("Ese responsable ya está configurado.")
    responsable = ServicioResponsable.objects.create(
        servicio=servicio, tipo_responsable=tipo_responsable, activo=True, **campos
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=responsable,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(responsable),
    )
    return responsable


@transaction.atomic
def retirar_responsable(responsable, actor):
    _exigir_administracion(actor)
    responsable = ServicioResponsable.objects.select_for_update().get(pk=responsable.pk)
    if not responsable.activo:
        return responsable
    anterior = serializar(responsable)
    responsable.activo = False
    responsable.save(update_fields=["activo", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=responsable,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=serializar(responsable),
    )
    return responsable


# --- 4.5 — Política de entrega ---------------------------------------------


@transaction.atomic
def configurar_politica_entrega(servicio, actor, *, politica, dias_observacion=None):
    """Define qué ocurre tras entregar formalmente el resultado al
    solicitante: cierre inmediato o periodo de observaciones de N días.

    Solo configura el Servicio. Cada Ticket congela la política vigente al
    crear su borrador (`Ticket.entrega_politica`), así que un cambio aquí NO
    modifica retroactivamente a los Tickets ya creados."""
    _exigir_administracion(actor)
    Politica = Servicio.PoliticaEntrega
    if politica not in (Politica.CIERRE_DIRECTO, Politica.PERIODO_OBSERVACIONES):
        raise ValidationError("Seleccione una política de entrega válida.")
    if politica == Politica.PERIODO_OBSERVACIONES:
        if not isinstance(dias_observacion, int) or isinstance(dias_observacion, bool) or not 1 <= dias_observacion <= 90:
            raise ValidationError("El periodo de observaciones debe ser de 1 a 90 días.")
    else:
        dias_observacion = None
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    anterior = {"politica_entrega": servicio.politica_entrega, "dias_observacion": servicio.dias_observacion}
    nuevo = {"politica_entrega": politica, "dias_observacion": dias_observacion}
    if anterior == nuevo:
        return servicio
    servicio.politica_entrega = politica
    servicio.dias_observacion = dias_observacion
    servicio.save(update_fields=["politica_entrega", "dias_observacion", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )
    return servicio
