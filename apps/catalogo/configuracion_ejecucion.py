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


def _errores_entregable_revisado(bloque, version):
    """4.E2 — la referencia opcional de una APROBACION a un bloque ENTREGABLE se revalida al
    publicar (el modelo ya la valida al guardar; esto cubre datos escritos sin pasar por él)."""
    revisado = bloque.entregable_revisado
    if revisado is None:
        return []
    prefijo = f"La aprobacion {bloque.nombre} revisa «{revisado.nombre}»"
    if revisado.tipo != BloqueOperativo.Tipo.ENTREGABLE:
        return [f"{prefijo}, que no es un bloque de entregable."]
    if revisado.version_id != version.pk:
        return [f"{prefijo}, que pertenece a otra configuracion."]
    definicion = revisado.definicion_entregable
    if definicion is None or definicion.servicio_id != version.servicio_id:
        return [f"{prefijo}, que no tiene un entregable valido de este servicio."]
    if definicion.tipo not in BloqueOperativo.TIPOS_ENTREGABLE_REVISABLES:
        return [f"{prefijo}: solo se puede revisar un entregable de texto, enlace o archivo."]
    return []


def validar_configuracion_ejecucion(version):
    errores = []
    if version.workflow_version_id != version.servicio.workflow.version_activa_id:
        errores.append("La configuracion corresponde a una version anterior de la plantilla.")
    if not version.bloques.exists():
        errores.append("La configuracion no tiene bloques operativos.")
    fases_validas = set(version.workflow_version.fases.values_list("pk", flat=True))
    for bloque in version.bloques.select_related(
        "fase", "definicion_entregable", "entregable_revisado__definicion_entregable"
    ).prefetch_related("transiciones_salientes").order_by("orden", "pk"):
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
            errores.extend(_errores_entregable_revisado(bloque, version))
        elif bloque.tipo == BloqueOperativo.Tipo.ENTREGABLE:
            definicion = bloque.definicion_entregable
            if definicion is None:
                errores.append(f"El entregable {bloque.nombre} no tiene un entregable seleccionado.")
            elif definicion.servicio_id != version.servicio_id:
                errores.append(f"El entregable {bloque.nombre} usa un entregable de otro servicio.")
            elif not definicion.activo:
                errores.append(
                    f"El entregable {bloque.nombre} usa «{definicion.nombre}», que fue retirado: "
                    "los tickets nuevos ya no lo reciben."
                )
        elif bloque.tipo == BloqueOperativo.Tipo.ESPERA:
            # Histórico: una ESPERA ya configurada se sigue validando y ejecutando, pero no
            # puede crearse una nueva (ver `agregar_bloque_operativo`).
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
                # 4.B1: el bloque ENTREGABLE conserva su referencia a la definición.
                definicion_entregable=bloque.definicion_entregable,
                descripcion=bloque.descripcion,
                orden=bloque.orden,
                configuracion=bloque.configuracion,
            )
            bloques_clonados[bloque.pk] = nuevo_bloque
        # 4.E2: la aprobación conserva QUÉ entregable revisa, ahora apuntando al bloque
        # clonado equivalente (por eso se hace en una segunda pasada, con todos creados).
        for bloque in clonar_desde.bloques.exclude(entregable_revisado__isnull=True):
            nuevo_bloque = bloques_clonados[bloque.pk]
            nuevo_bloque.entregable_revisado = bloques_clonados[bloque.entregable_revisado_id]
            nuevo_bloque.save(update_fields=["entregable_revisado", "actualizado_en"])
        for transicion in TransicionBloqueOperativo.objects.filter(bloque_origen__version=clonar_desde).order_by(
            "bloque_origen_id", "prioridad", "pk"
        ):
            TransicionBloqueOperativo.objects.create(
                bloque_origen=bloques_clonados[transicion.bloque_origen_id],
                bloque_destino=(
                    bloques_clonados[transicion.bloque_destino_id] if transicion.bloque_destino_id else None
                ),
                finaliza=transicion.finaliza,
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
    version, actor, *, fase, tipo, nombre, descripcion="", orden=None, configuracion=None, clave="",
    definicion_entregable=None, entregable_revisado=None,
):
    _exigir_administracion(actor)
    if tipo not in BloqueOperativo.TIPOS_CONFIGURABLES:
        # V1 (4.B1): ESPERA ya no es un bloque que un administrador pueda crear.
        raise ValidationError("Este tipo de bloque ya no se puede configurar.")
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
        definicion_entregable=definicion_entregable,
        entregable_revisado=entregable_revisado,
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
    """Crea una ruta de `origen`. `destino` es un bloque, o `None` junto con
    `finaliza=True` (4.E2) para FINALIZAR el flujo por esa ruta."""
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
    # 4.E2: `finaliza=True` convierte la ruta en un fin de flujo (sin destino); un `destino`
    # concreto la vuelve a apuntar a un bloque. Ambos son excluyentes (lo impone la BD).
    finaliza = reglas.pop("finaliza", None)
    if finaliza:
        transicion.bloque_destino = None
        transicion.finaliza = True
    elif destino is not None:
        transicion.bloque_destino = destino
        transicion.finaliza = False
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
    pk_original = transicion.pk
    transicion.delete()
    transicion.pk = pk_original  # `delete()` lo anula y la auditoría necesita el `object_id`
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )


_SIN_CAMBIO = object()


@transaction.atomic
def editar_bloque_operativo(
    version, bloque, actor, *, nombre=None, descripcion=None, orden=None, configuracion=None, clave=None,
    definicion_entregable=None, entregable_revisado=_SIN_CAMBIO,
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
    if definicion_entregable is not None:
        # Solo válido para un bloque ENTREGABLE (lo comprueba `BloqueOperativo.clean`).
        bloque.definicion_entregable = definicion_entregable
        campos.append("definicion_entregable")
    if entregable_revisado is not _SIN_CAMBIO:
        # `None` quita la relación (aprobación general). Solo válido para una APROBACION.
        bloque.entregable_revisado = entregable_revisado
        campos.append("entregable_revisado")
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
    revisoras = list(bloque.aprobaciones_que_lo_revisan.values_list("nombre", flat=True))
    if revisoras:
        # 4.E2: no se elimina un entregable que una aprobación revisa (sería dejarla sin objeto).
        raise ValidationError(
            f"El bloque «{bloque.nombre}» es revisado por: {', '.join(revisoras)}. "
            "Cambia primero la aprobación para eliminarlo."
        )
    anterior = serializar(bloque)
    pk_original = bloque.pk
    bloque.delete()
    bloque.pk = pk_original  # `delete()` lo anula y la auditoría necesita el `object_id`
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
