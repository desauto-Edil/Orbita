"""Programación de Procesos — Sprint 4.G1 (adelanto autorizado de «Procesos Recurrentes»).

Responsabilidad: configurar CUÁNDO Órbita genera automáticamente una ejecución de un Proceso
(`ProgramacionProceso`) y decidir si el Proceso es COMPATIBLE con ser programado. La generación del
Ticket vive en `apps.tickets.programadas`; el cálculo de periodos, en `apps.tickets.periodos`.

PROGRAMACIÓN ≠ WORKFLOW: la programación pertenece al Proceso. Un Workflow compartido puede servir
a un Proceso manual y a otro programado.

«Manual» = no tener una programación activa. Un Proceso con programación activa sale del catálogo
(`visibilidad.servicios_visibles_para`): se inicia por programación, no por el portal.

Compatibilidad: un ticket programado NO tiene solicitante, así que el Proceso no puede depender de
una persona que lo solicitó. Se valida AL ACTIVAR, AL PUBLICAR la ejecución y otra vez al generar
(`errores_de_generacion`), para que Celery no descubra el problema meses después.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalogo.models import ProgramacionProceso, Servicio, ServicioResponsable
from apps.core.auditoria import registrar_evento
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import RegistroAuditoria
from apps.tickets import periodos


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para administrar el catálogo.")


def programacion_activa_de(servicio):
    """La programación ACTIVA del Proceso, o `None` (inicio manual)."""
    return ProgramacionProceso.objects.filter(servicio=servicio, activa=True).first()


# --- Compatibilidad ----------------------------------------------------------


def bloques_dirigidos_al_solicitante(servicio, configuracion=None):
    """Nombres de los bloques/etapas del flujo del Proceso que dependen del SOLICITANTE (una
    actividad dirigida a él o una aprobación en la que participa). `configuracion` permite revisar
    una configuración de ejecución concreta (p. ej. la que está por publicarse); sin ella se revisa
    la vigente. Solo lectura."""
    workflow = servicio.workflow
    if workflow is None:
        return []
    nombres = []
    if workflow.modo == "PLANTILLA_FASES":
        configuracion = configuracion or servicio.configuracion_ejecucion_activa
        if configuracion is None:
            return []
        for bloque in configuracion.bloques.order_by("orden", "pk"):
            cfg = bloque.configuracion or {}
            if bloque.tipo == "ACTIVIDAD" and cfg.get("tipo_actor") == "SOLICITANTE":
                nombres.append(bloque.nombre)
            elif bloque.tipo == "APROBACION" and any(
                (p or {}).get("tipo") == "SOLICITANTE" for p in (cfg.get("participantes") or [])
            ):
                nombres.append(bloque.nombre)
        return nombres
    version = workflow.version_activa
    if version is None:
        return []
    for etapa in version.etapas.select_related("configuracion_tarea").prefetch_related("configuracion_aprobacion__participantes"):
        tarea = getattr(etapa, "configuracion_tarea", None)
        aprobacion = getattr(etapa, "configuracion_aprobacion", None)
        if tarea is not None and tarea.tipo_responsable == "SOLICITANTE":
            nombres.append(etapa.nombre)
        elif aprobacion is not None and aprobacion.participantes.filter(tipo_aprobador="SOLICITANTE").exists():
            nombres.append(etapa.nombre)
    return nombres


def campos_obligatorios_de_version(version):
    """Etiquetas de los campos de `version` (una `FormularioVersion`) que una persona DEBERÍA llenar:
    visibles y obligatorios con el formulario vacío. Un ticket programado nace con el formulario
    vacío y sin nadie que lo responda."""
    from apps.tickets.validaciones import calcular_estados_efectivos

    if version is None:
        return []
    estados = calcular_estados_efectivos(version, {})
    return [c.etiqueta for c in version.campos.all() if estados[c.id].visible and estados[c.id].requerido]


def campos_obligatorios_del_formulario(servicio):
    """Lo mismo, sobre la versión ACTIVA del formulario de entrada del Proceso."""
    formulario = servicio.formulario
    return campos_obligatorios_de_version(formulario.version_activa if formulario is not None else None)


def mensaje_campos_obligatorios(obligatorios):
    return (
        "El formulario de entrada tiene campos obligatorios ("
        + ", ".join(obligatorios[:5])
        + ("…" if len(obligatorios) > 5 else "")
        + ") y un proceso programado no tiene quién los llene. Hazlos opcionales o quítalos."
    )


def mensaje_bloque_solicitante(nombre):
    return (
        f"El bloque «{nombre}» está dirigido al solicitante, y un proceso programado no tiene "
        "solicitante. Cámbialo por una persona, un equipo o el responsable del ticket."
    )


def errores_de_compatibilidad(servicio, configuracion=None):
    """Por qué este Proceso NO puede (o dejaría de poder) ser programado, en lenguaje de quien lo
    configura: cada mensaje dice qué corregir. Lista vacía = compatible. Solo lectura."""
    errores = []
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        errores.append("Solo un Proceso puede programarse.")
        return errores
    if servicio.politica_entrega:
        errores.append(
            "El proceso tiene una entrega formal al solicitante, y un proceso programado no tiene "
            "solicitante. Quita la política de entrega en la pestaña Salida."
        )
    for nombre in bloques_dirigidos_al_solicitante(servicio, configuracion):
        errores.append(mensaje_bloque_solicitante(nombre))
    obligatorios = campos_obligatorios_del_formulario(servicio)
    if obligatorios:
        errores.append(mensaje_campos_obligatorios(obligatorios))
    return errores


def errores_de_responsable_inicial(responsable, servicio):
    """Por qué `responsable` (un `ServicioResponsable`) no sirve como responsable inicial de
    `servicio` HOY, o lista vacía."""
    if responsable is None:
        return ["Falta el responsable inicial."]
    if responsable.servicio_id != servicio.pk:
        return ["El responsable inicial no pertenece a este proceso."]
    if not responsable.activo:
        return ["El responsable inicial ya no está activo en este proceso."]
    if responsable.tipo_responsable == ServicioResponsable.TipoResponsable.USUARIO:
        if responsable.usuario is None or not responsable.usuario.is_active:
            return ["El usuario responsable inicial ya no está activo."]
    elif responsable.equipo is None or not responsable.equipo.activo:
        return ["El equipo responsable inicial ya no está activo."]
    return []


def errores_de_generacion(programacion):
    """Todo lo que impediría generar una ejecución AHORA. Lista vacía = se puede generar. Lo usa
    la generación automática; sus mensajes quedan en `ProgramacionProceso.ultimo_error`."""
    from apps.catalogo.operaciones import validar_publicacion

    servicio = programacion.servicio
    errores = []
    if not servicio.activo:
        errores.append("El proceso no está publicado (está inactivo).")
    errores.extend(errores_de_compatibilidad(servicio))
    errores.extend(errores_de_responsable_inicial(programacion.responsable_inicial, servicio))
    if not errores:
        try:
            validar_publicacion(servicio)
        except ValidationError as exc:
            errores.extend(exc.messages)
    return errores


def foto_de_programacion(programacion):
    """Cómo estaba configurada la programación al generar una ejecución (JSON)."""
    responsable = programacion.responsable_inicial
    return {
        "programacion_id": programacion.pk,
        "frecuencia": programacion.frecuencia,
        "dia_creacion": programacion.dia_creacion,
        "periodo": programacion.periodo,
        "activada_desde": programacion.activada_desde.isoformat() if programacion.activada_desde else None,
        "responsable_inicial": None
        if responsable is None
        else {
            "servicio_responsable_id": responsable.pk,
            "tipo": responsable.tipo_responsable,
            "usuario_id": responsable.usuario_id,
            "equipo_id": responsable.equipo_id,
        },
    }


# --- Configuración (Studio › General › Inicio) -------------------------------


def _datos_auditables(programacion):
    """Lo que identifica la configuración (sin marcas de tiempo ni estado operativo)."""
    return {
        "servicio_id": programacion.servicio_id,
        "activa": programacion.activa,
        "frecuencia": programacion.frecuencia,
        "dia_creacion": programacion.dia_creacion,
        "periodo": programacion.periodo,
        "activada_desde": programacion.activada_desde.isoformat() if programacion.activada_desde else None,
        "responsable_inicial_id": programacion.responsable_inicial_id,
    }


@transaction.atomic
def asegurar_responsable_inicial(servicio, actor, responsable):
    """El `ServicioResponsable` activo que corresponde a `responsable` = `(tipo, usuario | equipo)`.
    Si esa persona o equipo todavía no atiende el proceso, lo agrega (misma operación auditada de la
    pestaña Publicar): así se puede elegir el responsable inicial al CREAR el proceso, sin pasar
    antes por Publicar."""
    from apps.catalogo.operaciones import agregar_responsable

    tipo, objeto = responsable
    Tipo = ServicioResponsable.TipoResponsable
    campo = "usuario" if tipo == Tipo.USUARIO else "equipo"
    existente = ServicioResponsable.objects.filter(
        servicio=servicio, tipo_responsable=tipo, activo=True, **{campo: objeto}
    ).first()
    if existente is not None:
        return existente
    return agregar_responsable(
        servicio, actor, tipo_responsable=tipo,
        usuario=objeto if tipo == Tipo.USUARIO else None,
        equipo=objeto if tipo == Tipo.EQUIPO else None,
    )


@transaction.atomic
def configurar_programacion(servicio, actor, *, dia_creacion, periodo, responsable_inicial):
    """Deja el Proceso como «Programado»: crea la programación o actualiza la existente y la activa.

    Cambiar el calendario (día o periodo) o reactivar una programación pausada reinicia
    `activada_desde` a HOY: la nueva regla aplica hacia adelante y nunca genera periodos que ya
    pasaron. Cambiar solo el responsable inicial no la reinicia."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Solo un Proceso puede programarse.")
    try:
        periodos.validar_dia(dia_creacion)
    except ValueError as exc:
        raise ValidationError(str(exc))
    if periodo not in periodos.PERIODOS:
        raise ValidationError("Seleccione si la ejecución cubre el mes actual o el mes siguiente.")
    errores = errores_de_responsable_inicial(responsable_inicial, servicio)
    errores.extend(errores_de_compatibilidad(servicio))
    if errores:
        raise ValidationError(errores)

    hoy = timezone.localdate()
    programacion = ProgramacionProceso.objects.select_for_update().filter(servicio=servicio).first()
    if programacion is None:
        programacion = ProgramacionProceso.objects.create(
            servicio=servicio, activa=True, frecuencia=periodos.MENSUAL, dia_creacion=dia_creacion,
            periodo=periodo, activada_desde=hoy, responsable_inicial=responsable_inicial,
        )
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR, instancia=programacion,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=None, datos_nuevos=_datos_auditables(programacion),
        )
        return programacion

    anterior = _datos_auditables(programacion)
    reinicia = (
        not programacion.activa
        or programacion.dia_creacion != dia_creacion
        or programacion.periodo != periodo
    )
    programacion.activa = True
    programacion.frecuencia = periodos.MENSUAL
    programacion.dia_creacion = dia_creacion
    programacion.periodo = periodo
    programacion.responsable_inicial = responsable_inicial
    if reinicia or programacion.activada_desde is None:
        programacion.activada_desde = hoy
    programacion.ultimo_error = ""
    programacion.save()
    nuevo = _datos_auditables(programacion)
    if anterior != nuevo:
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=programacion,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=anterior, datos_nuevos=nuevo,
        )
    return programacion


@transaction.atomic
def desactivar_programacion(servicio, actor):
    """Deja el Proceso como «Manual»: pausa la programación (se conserva su configuración). Las
    ejecuciones ya generadas no cambian y el Proceso vuelve a poder solicitarse a mano."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    programacion = ProgramacionProceso.objects.select_for_update().filter(servicio=servicio).first()
    if programacion is None or not programacion.activa:
        return programacion
    anterior = _datos_auditables(programacion)
    programacion.activa = False
    programacion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=programacion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=_datos_auditables(programacion),
    )
    return programacion
