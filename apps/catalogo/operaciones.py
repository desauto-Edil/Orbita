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
        # Fase 4.A: la configuración activa se ejecuta sobre SU versión de la plantilla (la vigente o una anterior
        # publicada), así publicar otra versión de un flujo compartido no deja sin ejecución a los Servicios que
        # aún no la reconcilian. Nunca sobre un borrador ni sobre otra plantilla.
        propia = config.workflow_version
        if propia.workflow_id != workflow.pk or propia.estado == WorkflowVersion.Estado.BORRADOR:
            raise ValidationError("La configuracion operativa no corresponde a la version activa de la plantilla.")
        from apps.catalogo.configuracion_ejecucion import validar_configuracion_ejecucion

        errores = validar_configuracion_ejecucion(config, exigir_plantilla_vigente=False)
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


def errores_de_prorroga(servicio):
    """La regla de publicación de la prórroga (4.A2), reutilizable por quien muestra su estado. Solo lectura."""
    if servicio.politica_prorroga == Servicio.PoliticaProrroga.CON_APROBACION:
        usuario, equipo = servicio.prorroga_aprobador_usuario, servicio.prorroga_aprobador_equipo
        if not ((usuario is not None and usuario.is_active) or (equipo is not None and equipo.activo)):
            return ["La prórroga con aprobación necesita un aprobador activo (usuario o equipo)."]
    return []


def requiere_formulario_inicial(servicio):
    """¿Este elemento del catálogo NECESITA un formulario de inicio propio para publicarse?

    Un SERVICIO (incluido el Servicio interno del Ticket General) sí: quien solicita responde ese formulario.
    Un PROCESO no: puede trabajar solo con datos de inicio, solo con colecciones de ítems, con ambos o con
    ninguno. Si no los configuró, al publicarse recibe unos datos de inicio VACÍOS (`asegurar_datos_inicio`)
    para que cada Ticket siga congelando una `FormularioVersion`, sin cambiar ese contrato."""
    return servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general


def errores_de_formulario_inicial(servicio):
    """La regla del formulario de inicio para publicar. Solo lectura.

    - Servicio: formulario propio con versión ACTIVA (lo de siempre).
    - Proceso sin formulario: nada que exigir (los datos de inicio son opcionales).
    - Proceso que empezó sus datos de inicio: lo guardado debe estar en vigor, si no el Ticket congelaría una
      versión que el diseñador no ve como vigente."""
    formulario = servicio.formulario
    version = formulario.version_activa if formulario is not None else None
    vigente = (
        version is not None and version.estado == FormularioVersion.Estado.ACTIVA
        and version.formulario_id == formulario.pk
    )
    if vigente:
        return []
    if requiere_formulario_inicial(servicio):
        return ["Se requiere un formulario con una versión ACTIVA propia."]
    if formulario is None:
        return []
    return ["Los datos de inicio tienen un borrador sin activar: actívalo o publica los cambios."]


MENSAJE_SIN_ACCESO = (
    "Nadie puede iniciarlo: está restringido y nadie tiene acceso. Concede acceso o hazlo público interno."
)
MENSAJE_SIN_ATENCION_PROGRAMADO = (
    "Nadie puede tomar sus ejecuciones: ningún miembro activo del equipo responsable inicial tiene el permiso de "
    "atender tickets. Elige una persona como responsable inicial o asigna ese permiso a alguien del equipo."
)
MENSAJE_SIN_ATENCION = (
    "Nadie puede atenderlo: agrega un responsable (persona o equipo) que tenga el permiso de atender tickets, "
    "o asigna ese permiso con alcance global a alguien."
)


def _es_proceso_programado(servicio):
    from apps.catalogo.programacion import programacion_activa_de

    return servicio.tipo == Servicio.Tipo.PROCESO and programacion_activa_de(servicio) is not None


def hay_ruta_de_atencion(servicio):
    """¿Existe hoy alguien que pueda TOMAR los tickets de `servicio`? Es la misma regla de
    `tickets.autorizacion.puede_tomar`, leída sobre la configuración: alguien con `tickets.atender` GLOBAL, o un
    responsable configurado (persona activa, o miembro activo de un equipo activo) con `tickets.atender` en algún
    alcance. Solo lectura; no cambia el modelo de permisos."""
    from apps.core.autorizacion import existe_asignacion_vigente
    from apps.core.models import MiembroEquipo

    permiso = "tickets.atender"
    if existe_asignacion_vigente(permiso, solo_global=True):
        return True
    responsables = servicio.responsables.filter(activo=True)
    personas = set(
        responsables.filter(tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO, usuario__is_active=True)
        .values_list("usuario_id", flat=True)
    )
    equipos = responsables.filter(
        tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO, equipo__activo=True
    ).values_list("equipo_id", flat=True)
    personas |= set(
        MiembroEquipo.objects.filter(equipo_id__in=list(equipos), activo=True).values_list("usuario_id", flat=True)
    )
    return bool(personas) and existe_asignacion_vigente(permiso, usuarios_ids=personas)


def errores_de_atencion_programada(programacion):
    """Bloque 4.1 — ¿alguien podrá poner en atención las ejecuciones de un Proceso programado? Cada una nace
    RADICADA con el responsable inicial: una PERSONA la inicia ella misma (`puede_iniciar_atencion`, sin otro
    permiso); un EQUIPO la deja en Cola y alguien debe TOMARLA (`puede_tomar`: `tickets.atender` GLOBAL, o un
    miembro activo del equipo con ese permiso en algún alcance). Lista vacía = hay ruta. Solo lectura; supone un
    responsable inicial ya válido (`programacion.errores_de_responsable_inicial`)."""
    from apps.core.autorizacion import existe_asignacion_vigente
    from apps.core.models import MiembroEquipo

    responsable = programacion.responsable_inicial if programacion is not None else None
    if responsable is None or responsable.tipo_responsable != ServicioResponsable.TipoResponsable.EQUIPO:
        return []
    permiso = "tickets.atender"
    if existe_asignacion_vigente(permiso, solo_global=True):
        return []
    miembros = MiembroEquipo.objects.filter(equipo_id=responsable.equipo_id, activo=True).values_list("usuario_id", flat=True)
    if existe_asignacion_vigente(permiso, usuarios_ids=list(miembros)):
        return []
    return [MENSAJE_SIN_ATENCION_PROGRAMADO]


def errores_de_acceso(servicio):
    """Requisitos de PUBLICACIÓN para que lo publicado se pueda usar (Bloque 2): alguien puede iniciarlo (no está
    restringido sin concesiones) y alguien puede atenderlo (`hay_ruta_de_atencion`). No aplican al Servicio interno
    del Ticket General (se dirige por destinos, 4.C2); un Proceso programado no se solicita y su ruta es el responsable
    inicial vigente (`programacion.errores_de_responsable_inicial`). Como `dependencias.exigir`, lo aplica `activar_servicio` y no
    `validar_publicacion`: lo ya publicado y la generación programada no cambian. Solo lectura."""
    if servicio.es_ticket_general:
        return []
    if _es_proceso_programado(servicio):
        # Su ruta operativa es el responsable inicial de la programación (misma regla que la generación) y, si es un
        # equipo, que alguien pueda tomar sus ejecuciones (Bloque 4.1).
        from apps.catalogo.programacion import errores_de_responsable_inicial, programacion_activa_de

        programacion = programacion_activa_de(servicio)
        return (
            errores_de_responsable_inicial(programacion.responsable_inicial, servicio)
            or errores_de_atencion_programada(programacion)
        )
    errores = []
    if (
        servicio.alcance_visibilidad == Servicio.AlcanceVisibilidad.RESTRINGIDO
        and not servicio.visibilidad.filter(activo=True).exists()
    ):
        errores.append(MENSAJE_SIN_ACCESO)
    if not hay_ruta_de_atencion(servicio):
        errores.append(MENSAJE_SIN_ATENCION)
    return errores


def datos_inicio_configurados(servicio):
    """¿El Proceso tiene datos de inicio CONFIGURADOS (campos en su versión activa o un borrador en curso)?
    El formulario técnico vacío que crea `asegurar_datos_inicio` no cuenta: la interfaz lo presenta como «sin
    datos de inicio». En un Servicio equivale a tener formulario. Solo lectura."""
    formulario = servicio.formulario
    if formulario is None:
        return False
    if requiere_formulario_inicial(servicio):
        return True
    if formulario.versiones.filter(estado=FormularioVersion.Estado.BORRADOR).exists():
        return True
    activa = formulario.version_activa
    return activa is not None and activa.campos.exists()


@transaction.atomic
def asegurar_datos_inicio(servicio, actor):
    """Un PROCESO que no configuró datos de inicio recibe, al publicarse, un formulario de inicio VACÍO con su
    versión 1 ACTIVA. Así cada Ticket congela su `FormularioVersion` como siempre (el contrato de
    `TicketServicio`/`RespuestaFormulario` no cambia) y no hay nada que responder ni diligenciar. Idempotente:
    no hace nada si el Proceso ya tiene formulario o si es un Servicio. Auditado (reutiliza las operaciones
    existentes)."""
    if requiere_formulario_inicial(servicio) or servicio.formulario_id is not None:
        return None
    from apps.catalogo.versionamiento import activar_version

    formulario = asociar_formulario_nuevo(
        servicio, actor, nombre=f"Datos de inicio · {servicio.nombre}"[:150],
        descripcion="Sin datos de inicio: el proceso no pide información al crearse.",
    )
    version = formulario.versiones.order_by("-numero").first()
    activar_version(formulario, version, actor)
    servicio.refresh_from_db()
    return formulario


def validar_publicacion(servicio):
    """Solo lectura. No exige responsables/área: no son requisitos de radicación."""
    errores = []
    if servicio.tipo not in Servicio.Tipo.values:
        errores.append("Tipo de catálogo inválido.")
    errores.extend(errores_de_formulario_inicial(servicio))
    try:
        validar_ejecucion(servicio)
    except ValidationError as exc:
        errores.extend(exc.messages)
    errores.extend(errores_de_prorroga(servicio))
    # 4.G1: un Proceso con programación activa no puede depender de un solicitante humano.
    from apps.catalogo.programacion import errores_de_compatibilidad, programacion_activa_de

    if programacion_activa_de(servicio) is not None:
        errores.extend(errores_de_compatibilidad(servicio))
    # 4.G2B1: si el Proceso usa elementos estructurados, sus columnas deben estar listas (si no,
    # el Ticket no podría congelarlas al nacer).
    from apps.catalogo.parrilla import errores_de_parrilla

    errores.extend(errores_de_parrilla(servicio))
    # 4.G2C1: un entregable que revisa elementos necesita columnas vigentes y quién revise.
    from apps.catalogo.revision_elementos import errores_de_conjuntos

    errores.extend(errores_de_conjuntos(servicio))
    # 4.G2D1: la configuración de tareas por elemento debe seguir siendo válida con la versión activa.
    from apps.catalogo.tareas_elemento import errores_de_tareas_elemento

    errores.extend(errores_de_tareas_elemento(servicio))
    # Fase 3: cada gaceta activa del Proceso debe seguir siendo válida (colecciones, columnas, personas).
    from apps.catalogo.gacetas import errores_de_gacetas

    errores.extend(errores_de_gacetas(servicio))
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


def _tiene_programacion_activa(servicio):
    # Import diferido: `programacion` usa este módulo para `validar_publicacion`.
    from apps.catalogo.programacion import programacion_activa_de

    return programacion_activa_de(servicio) is not None


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para administrar el catálogo.")


def errores_para_publicar(servicio):
    """TODO lo que impide publicar `servicio` tal como está persistido, junto: `validar_publicacion` (también lo
    usa la ejecución), `errores_de_acceso` (Bloque 2) y las dependencias obligatorias del flujo (Fase 4.A: no se
    publica una configuración que promete algo que nunca ocurriría). Las dos últimas no forman parte de
    `validar_publicacion`, así lo ya publicado y la generación programada no se rompen. Solo lectura; es la regla
    que aplica `activar_servicio` y la que evalúa el Studio (`apps.catalogo.evaluacion`)."""
    from apps.catalogo import dependencias

    errores = []
    try:
        validar_publicacion(servicio)
    except ValidationError as exc:
        errores.extend(exc.messages)
    errores.extend(errores_de_acceso(servicio))
    errores.extend(d.mensaje for d in dependencias.bloqueantes(servicio, servicio.configuracion_ejecucion_activa))
    return errores


@transaction.atomic
def activar_servicio(servicio, actor):
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    errores = errores_para_publicar(servicio)
    if errores:
        raise ValidationError(errores)
    # Un Proceso sin datos de inicio los recibe vacíos (ver `asegurar_datos_inicio`).
    asegurar_datos_inicio(servicio, actor)
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
    # 4.G1: un Proceso programado deja de poder serlo si cambia de tipo.
    if tipo != Servicio.Tipo.PROCESO and _tiene_programacion_activa(servicio):
        raise ValidationError(
            "Este proceso tiene una programación activa. Pásalo a «Manual» en Inicio antes de cambiarlo a servicio."
        )
    if tipo != Servicio.Tipo.PROCESO:
        from apps.catalogo.parrilla import definicion_activa_de

        if definicion_activa_de(servicio) is not None:
            raise ValidationError(
                "Este proceso usa una estructura de elementos. Desactívala en la pestaña Formulario "
                "antes de cambiarlo a servicio."
            )
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
    """Retira (desactiva) un responsable general. Bloque 4.1: el responsable inicial de una programación ACTIVA no
    se retira (las ejecuciones dejarían de generarse sin aviso): primero se cambia en Básico › Inicio o se pasa el
    proceso a Manual. Locks: Servicio → programación → responsable (el orden de `configurar_programacion`)."""
    from apps.catalogo.models import ProgramacionProceso

    _exigir_administracion(actor)
    _bloquear_servicio(responsable.servicio)
    if ProgramacionProceso.objects.select_for_update().filter(
        servicio_id=responsable.servicio_id, activa=True, responsable_inicial_id=responsable.pk
    ).exists():
        raise ValidationError(
            "Es el responsable inicial de la programación: cámbialo en Básico › Inicio (o pasa el proceso a Manual) "
            "antes de retirarlo."
        )
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
    # Fase 4.A: `politica=""` RETIRA la entrega formal (vuelve a resolver y cerrar manualmente). Solo afecta a los
    # tickets nuevos: cada ticket congeló su política al nacer.
    if politica not in (Politica.CIERRE_DIRECTO, Politica.PERIODO_OBSERVACIONES, ""):
        raise ValidationError("Seleccione una política de entrega válida.")
    if politica == Politica.PERIODO_OBSERVACIONES:
        if not isinstance(dias_observacion, int) or isinstance(dias_observacion, bool) or not 1 <= dias_observacion <= 90:
            raise ValidationError("El periodo de observaciones debe ser de 1 a 90 días.")
    else:
        dias_observacion = None
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if politica and _tiene_programacion_activa(servicio):
        raise ValidationError(
            "Este proceso se genera por programación y no tiene solicitante a quien entregarle: "
            "pásalo a «Manual» en Inicio antes de definir una entrega formal."
        )
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


@transaction.atomic
def configurar_tiempo_objetivo(servicio, actor, *, cantidad=None, unidad="", habiles=False):
    """4.A1 — define (o quita) el tiempo objetivo de atención del Servicio/
    Proceso. Sin `cantidad` se QUITA el compromiso temporal.

    Solo configura el Servicio. Cada Ticket congela estos valores al crear su
    borrador (`Ticket.tiempo_objetivo_*`), así que un cambio aquí NO modifica
    tickets ya creados. No calcula fechas: eso es de `apps.tickets.tiempos`."""
    from apps.tickets.tiempos import MAX_CANTIDAD, UNIDADES

    _exigir_administracion(actor)
    unidad = unidad or ""
    if cantidad is None:
        if unidad:
            raise ValidationError("Indique la cantidad del tiempo objetivo.")
        habiles = False
    else:
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or not 1 <= cantidad <= MAX_CANTIDAD:
            raise ValidationError(f"El tiempo objetivo debe ser un entero de 1 a {MAX_CANTIDAD}.")
        if unidad not in UNIDADES:
            raise ValidationError("Seleccione la unidad del tiempo objetivo (horas o días).")
        habiles = bool(habiles)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    campos = ("tiempo_objetivo_cantidad", "tiempo_objetivo_unidad", "tiempo_objetivo_habiles")
    anterior = {campo: getattr(servicio, campo) for campo in campos}
    nuevo = dict(zip(campos, (cantidad, unidad, habiles)))
    if anterior == nuevo:
        return servicio
    for campo, valor in nuevo.items():
        setattr(servicio, campo, valor)
    servicio.save(update_fields=[*campos, "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )
    return servicio


@transaction.atomic
def configurar_politica_prorroga(servicio, actor, *, politica, aprobador_usuario=None, aprobador_equipo=None):
    """4.A2 — define si los tickets de este Servicio/Proceso admiten prórroga de
    su fecha objetivo y, con aprobación, quién la resuelve (un usuario O un
    equipo activos, los mismos tipos de `ServicioResponsable`).

    Solo configura el Servicio. Cada Ticket congela política y aprobador al
    crear su borrador, así que un cambio aquí NO modifica tickets existentes. No
    depende de Workflow: un Servicio sin flujo también admite prórrogas."""
    _exigir_administracion(actor)
    Politica = Servicio.PoliticaProrroga
    if politica not in Politica.values:
        raise ValidationError("Seleccione una política de prórroga válida.")
    if politica == Politica.CON_APROBACION:
        if (aprobador_usuario is None) == (aprobador_equipo is None):
            raise ValidationError("La prórroga con aprobación necesita un aprobador: un usuario o un equipo, no ambos.")
        if aprobador_usuario is not None and not aprobador_usuario.is_active:
            raise ValidationError("El aprobador seleccionado no está activo.")
        if aprobador_equipo is not None and not aprobador_equipo.activo:
            raise ValidationError("El equipo aprobador seleccionado no está activo.")
    else:
        aprobador_usuario = aprobador_equipo = None
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    anterior = {
        "politica_prorroga": servicio.politica_prorroga,
        "prorroga_aprobador_usuario_id": servicio.prorroga_aprobador_usuario_id,
        "prorroga_aprobador_equipo_id": servicio.prorroga_aprobador_equipo_id,
    }
    nuevo = {
        "politica_prorroga": politica,
        "prorroga_aprobador_usuario_id": aprobador_usuario.pk if aprobador_usuario else None,
        "prorroga_aprobador_equipo_id": aprobador_equipo.pk if aprobador_equipo else None,
    }
    if anterior == nuevo:
        return servicio
    servicio.politica_prorroga = politica
    servicio.prorroga_aprobador_usuario = aprobador_usuario
    servicio.prorroga_aprobador_equipo = aprobador_equipo
    servicio.save(
        update_fields=["politica_prorroga", "prorroga_aprobador_usuario", "prorroga_aprobador_equipo", "actualizado_en"]
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )
    return servicio



@transaction.atomic
def configurar_datos_inicio(servicio, actor, *, en_ejecucion):
    """Fase 2 del modelo configurable — en un PROCESO, si los datos de inicio pertenecen a la ejecución (se
    inicia sin exigir el formulario completo y quien lo atiende lo diligencia después). Solo afecta a los
    tickets que se creen desde ahora: cada ticket congela la opción al nacer. Un Servicio no la usa."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    en_ejecucion = bool(en_ejecucion) and servicio.tipo == Servicio.Tipo.PROCESO and not servicio.es_ticket_general
    if servicio.datos_inicio_en_ejecucion == en_ejecucion:
        return servicio
    anterior = servicio.datos_inicio_en_ejecucion
    servicio.datos_inicio_en_ejecucion = en_ejecucion
    servicio.save(update_fields=["datos_inicio_en_ejecucion", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"datos_inicio_en_ejecucion": anterior},
        datos_nuevos={"datos_inicio_en_ejecucion": en_ejecucion},
    )
    return servicio
