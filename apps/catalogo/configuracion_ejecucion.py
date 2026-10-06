from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max

from apps.catalogo.models import BloqueOperativo, ConfiguracionEjecucionVersion, Servicio, TransicionBloqueOperativo
from apps.catalogo.operaciones import _exigir_administracion
from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import Equipo, RegistroAuditoria
from apps.workflows.actores import validar_actor
from apps.workflows.models import Workflow, WorkflowVersion


def _siguiente_numero(servicio):
    ultimo = ConfiguracionEjecucionVersion.objects.filter(servicio=servicio).order_by("-numero").first()
    return (ultimo.numero + 1) if ultimo else 1


def _workflow_version_por_defecto(servicio):
    if servicio.workflow_id is None:
        raise ValidationError("El servicio no tiene una plantilla de flujo vinculada.")
    version = servicio.workflow.version_activa
    if version is None or version.estado != WorkflowVersion.Estado.ACTIVA:
        raise ValidationError("La plantilla vinculada no tiene una version activa.")
    return version


def _validar_plantilla(servicio, workflow_version):
    if workflow_version.workflow_id != servicio.workflow_id:
        raise ValidationError("La version de Workflow no pertenece a la plantilla vinculada al Servicio.")
    if workflow_version.workflow.modo != Workflow.Modo.PLANTILLA_FASES:
        raise ValidationError("La configuracion operativa solo puede usar una plantilla de fases.")


def validar_configuracion_ejecucion(version):
    errores = []
    if version.workflow_version_id != version.servicio.workflow.version_activa_id:
        errores.append("La configuracion corresponde a una version anterior de la plantilla.")
    if not version.bloques.exists():
        errores.append("La configuracion no tiene bloques operativos.")
    fases_validas = set(version.workflow_version.fases.values_list("pk", flat=True))
    for bloque in version.bloques.select_related("fase").prefetch_related("transiciones_salientes").order_by("orden", "pk"):
        if bloque.fase_id not in fases_validas:
            errores.append(f"El bloque {bloque.nombre} pertenece a una fase que no es de la plantilla activa.")
        cfg = bloque.configuracion or {}
        if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
            try:
                usuario = (
                    get_user_model().objects.filter(pk=cfg.get("usuario_id")).first()
                    if cfg.get("usuario_id")
                    else None
                )
                equipo = Equipo.objects.filter(pk=cfg.get("equipo_id")).first() if cfg.get("equipo_id") else None
                validar_actor(
                    cfg.get("tipo_actor") or "",
                    usuario=usuario,
                    equipo=equipo,
                    permite_vacio=False,
                )
            except ValidationError as exc:
                errores.append(f"La actividad {bloque.nombre}: {'; '.join(exc.messages)}")
        elif bloque.tipo == BloqueOperativo.Tipo.APROBACION:
            if not cfg.get("modo") or not cfg.get("participantes"):
                errores.append(f"La aprobacion {bloque.nombre} no tiene aprobadores configurados.")
            for participante in cfg.get("participantes") or []:
                try:
                    usuario = (
                        get_user_model().objects.filter(pk=participante.get("usuario_id")).first()
                        if participante.get("usuario_id")
                        else None
                    )
                    equipo = (
                        Equipo.objects.filter(pk=participante.get("equipo_id")).first()
                        if participante.get("equipo_id")
                        else None
                    )
                    validar_actor(participante.get("tipo") or "", usuario=usuario, equipo=equipo, permite_vacio=False)
                except ValidationError as exc:
                    errores.append(f"La aprobacion {bloque.nombre}: {'; '.join(exc.messages)}")
            resultados = list(bloque.transiciones_salientes.values_list("resultado_aprobacion", flat=True))
            for esperado in {"APROBADA", "RECHAZADA", "DEVUELTA"}:
                if resultados.count(esperado) != 1:
                    errores.append(f"La aprobacion {bloque.nombre} debe tener exactamente una ruta {esperado}.")
        elif bloque.tipo == BloqueOperativo.Tipo.ESPERA:
            if cfg.get("modo") == "DURACION":
                if not cfg.get("duracion_valor") or not cfg.get("duracion_unidad"):
                    errores.append(f"La espera {bloque.nombre} no tiene duracion completa.")
            elif cfg.get("modo") == "FECHA":
                if not cfg.get("fecha_objetivo"):
                    errores.append(f"La espera {bloque.nombre} no tiene fecha objetivo.")
            else:
                errores.append(f"La espera {bloque.nombre} no tiene duracion o fecha.")
        elif bloque.tipo == BloqueOperativo.Tipo.DECISION:
            salientes = list(bloque.transiciones_salientes.all())
            if sum(1 for t in salientes if t.es_fallback) != 1:
                errores.append(f"La decision {bloque.nombre} debe tener exactamente una ruta alternativa.")
            if not any(not t.es_fallback for t in salientes):
                errores.append(f"La decision {bloque.nombre} no tiene condicion.")
    return errores


@transaction.atomic
def crear_nueva_version_configuracion(servicio, actor, *, workflow_version=None, clonar_desde=None):
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    workflow_version = workflow_version or (clonar_desde.workflow_version if clonar_desde else _workflow_version_por_defecto(servicio))
    _validar_plantilla(servicio, workflow_version)
    if clonar_desde is not None and clonar_desde.servicio_id != servicio.pk:
        raise ValidationError("Solo se puede clonar una configuracion del mismo Servicio.")
    if clonar_desde is not None and clonar_desde.workflow_version_id != workflow_version.pk:
        raise ValidationError("La configuracion clonada debe usar la misma version de Workflow.")

    nueva = ConfiguracionEjecucionVersion.objects.create(
        servicio=servicio,
        workflow_version=workflow_version,
        numero=_siguiente_numero(servicio),
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR,
    )
    bloques_clonados = {}
    if clonar_desde is not None:
        for bloque in clonar_desde.bloques.order_by("fase_id", "orden", "pk"):
            nuevo_bloque = BloqueOperativo.objects.create(
                version=nueva,
                fase=bloque.fase,
                tipo=bloque.tipo,
                nombre=bloque.nombre,
                # 4.B0: la clave es la identidad estable del bloque entre versiones.
                clave=bloque.clave,
                descripcion=bloque.descripcion,
                orden=bloque.orden,
                configuracion=bloque.configuracion,
            )
            bloques_clonados[bloque.pk] = nuevo_bloque
        for transicion in TransicionBloqueOperativo.objects.filter(bloque_origen__version=clonar_desde).order_by(
            "bloque_origen_id", "prioridad", "pk"
        ):
            TransicionBloqueOperativo.objects.create(
                bloque_origen=bloques_clonados[transicion.bloque_origen_id],
                bloque_destino=bloques_clonados[transicion.bloque_destino_id],
                nombre=transicion.nombre,
                prioridad=transicion.prioridad,
                variable=transicion.variable,
                operador=transicion.operador,
                valor=transicion.valor,
                es_fallback=transicion.es_fallback,
                resultado_aprobacion=transicion.resultado_aprobacion,
            )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=nueva,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(nueva),
    )
    return nueva


def preparar_configuracion_ejecucion(servicio, actor):
    borrador = servicio.configuraciones_ejecucion.filter(
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR
    ).order_by("-numero").first()
    if borrador is not None:
        return borrador
    activa = servicio.configuracion_ejecucion_activa
    if activa is not None and activa.workflow_version_id == servicio.workflow.version_activa_id:
        return crear_nueva_version_configuracion(servicio, actor, clonar_desde=activa)
    return crear_nueva_version_configuracion(servicio, actor)


@transaction.atomic
def agregar_bloque_operativo(
    version, actor, *, fase, tipo, nombre, descripcion="", orden=None, configuracion=None, clave=""
):
    _exigir_administracion(actor)
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    version.exigir_editable()
    if fase.version_id != version.workflow_version_id:
        raise ValidationError("El bloque debe asociarse a una fase de la plantilla configurada.")
    if orden is None:
        ultimo = version.bloques.filter(fase=fase).aggregate(maximo=Max("orden"))["maximo"] or 0
        orden = ultimo + 1
    bloque = BloqueOperativo(
        version=version,
        fase=fase,
        tipo=tipo,
        nombre=nombre,
        clave=clave,
        descripcion=descripcion,
        orden=orden,
        configuracion=configuracion or {},
    )
    bloque.full_clean()
    bloque.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=bloque,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(bloque),
    )
    return bloque


@transaction.atomic
def conectar_bloques_operativos(origen, destino, actor, **reglas):
    _exigir_administracion(actor)
    origen.version.exigir_editable()
    transicion = TransicionBloqueOperativo(bloque_origen=origen, bloque_destino=destino, **reglas)
    transicion.full_clean()
    transicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(transicion),
    )
    return transicion


@transaction.atomic
def editar_transicion_bloque_operativo(transicion, actor, *, destino=None, **reglas):
    _exigir_administracion(actor)
    transicion = TransicionBloqueOperativo.objects.select_related("bloque_origen__version").get(pk=transicion.pk)
    transicion.bloque_origen.version.exigir_editable()
    anterior = serializar(transicion)
    if destino is not None:
        transicion.bloque_destino = destino
    for campo, valor in reglas.items():
        setattr(transicion, campo, valor)
    transicion.full_clean()
    transicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(transicion),
    )
    return transicion


@transaction.atomic
def eliminar_transicion_bloque_operativo(transicion, actor):
    _exigir_administracion(actor)
    transicion = TransicionBloqueOperativo.objects.select_related("bloque_origen__version").get(pk=transicion.pk)
    transicion.bloque_origen.version.exigir_editable()
    anterior = serializar(transicion)
    transicion.delete()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )


@transaction.atomic
def editar_bloque_operativo(
    version, bloque, actor, *, nombre=None, descripcion=None, orden=None, configuracion=None, clave=None
):
    _exigir_administracion(actor)
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    version.exigir_editable()
    bloque = BloqueOperativo.objects.get(pk=bloque.pk)
    if bloque.version_id != version.pk:
        raise ValidationError("El bloque no pertenece a esta configuracion de ejecucion.")
    anterior = serializar(bloque)
    campos = []
    if nombre is not None:
        # Renombrar NO cambia la clave: es la identidad estable del bloque (4.B0).
        bloque.nombre = nombre
        campos.append("nombre")
    if clave:
        # Cambio explícito de identidad, solo posible mientras la configuración es BORRADOR.
        bloque.clave = clave
        campos.append("clave")
    if descripcion is not None:
        bloque.descripcion = descripcion
        campos.append("descripcion")
    if orden is not None:
        bloque.orden = orden
        campos.append("orden")
    if configuracion is not None:
        bloque.configuracion = configuracion
        campos.append("configuracion")
    if not campos:
        return bloque
    bloque.full_clean()
    bloque.save(update_fields=campos + ["actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=bloque,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(bloque),
    )
    return bloque


@transaction.atomic
def eliminar_bloque_operativo(version, bloque, actor):
    _exigir_administracion(actor)
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    version.exigir_editable()
    bloque = BloqueOperativo.objects.get(pk=bloque.pk, version=version)
    anterior = serializar(bloque)
    bloque.delete()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=bloque,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )


@transaction.atomic
def activar_configuracion_ejecucion(servicio, version, actor):
    _exigir_administracion(actor)
    servicio_original, version_original = servicio, version
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    if version.servicio_id != servicio.pk:
        raise ValidationError("La configuracion no pertenece a este Servicio.")
    if version.estado != ConfiguracionEjecucionVersion.Estado.BORRADOR:
        raise ValidationError("Solo se puede activar una configuracion en borrador.")
    _validar_plantilla(servicio, version.workflow_version)
    if version.workflow_version.estado != WorkflowVersion.Estado.ACTIVA:
        raise ValidationError("La configuracion solo puede activarse sobre una plantilla activa.")
    errores = validar_configuracion_ejecucion(version)
    if errores:
        raise ValidationError(errores)
    for bloque in version.bloques.select_related("fase"):
        bloque.full_clean()
    for transicion in TransicionBloqueOperativo.objects.filter(bloque_origen__version=version):
        transicion.full_clean()

    anterior = servicio.configuracion_ejecucion_activa
    if anterior is not None:
        anterior.estado = ConfiguracionEjecucionVersion.Estado.HISTORICA
        anterior.save(update_fields=["estado", "actualizado_en"])
    version.estado = ConfiguracionEjecucionVersion.Estado.ACTIVA
    version.save(update_fields=["estado", "actualizado_en"])
    servicio.configuracion_ejecucion_activa = version
    servicio.save(update_fields=["configuracion_ejecucion_activa", "actualizado_en"])

    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={"configuracion_ejecucion_activa_id": anterior.pk if anterior else None},
        datos_nuevos={"configuracion_ejecucion_activa_id": version.pk},
    )
    servicio_original.refresh_from_db()
    version_original.refresh_from_db()
    return version
