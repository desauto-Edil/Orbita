from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max

from apps.catalogo import actividades
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


def validar_configuracion_ejecucion(version, *, exigir_plantilla_vigente=True):
    """Errores de una configuración operativa. `exigir_plantilla_vigente=False` (Fase 4.A) lo usa la ejecución:
    una configuración ACTIVA sigue siendo ejecutable sobre la versión de la plantilla con la que se activó aunque
    la plantilla haya publicado otra; activar un borrador sí exige la versión vigente."""
    errores = []
    if exigir_plantilla_vigente and version.workflow_version_id != version.servicio.workflow.version_activa_id:
        errores.append("La configuración corresponde a una versión anterior del flujo: actualízala en la sección Flujo.")
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
                    columna=cfg.get("columna") or "",
                )
            except ValidationError as exc:
                errores.append(f"La actividad {bloque.nombre}: {'; '.join(exc.messages)}")
            errores.extend(f"La actividad {bloque.nombre}: {e}" for e in actividades.errores_de_actividad(cfg))
            errores.extend(f"La actividad {bloque.nombre}: {e}" for e in _errores_de_referencias(bloque, cfg))
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
                    validar_actor(
                        participante.get("tipo") or "", usuario=usuario, equipo=equipo, permite_vacio=False,
                        columna=participante.get("columna") or "",
                    )
                except ValidationError as exc:
                    errores.append(f"La aprobacion {bloque.nombre}: {'; '.join(exc.messages)}")
                if participante.get("tipo") == "COLUMNA":
                    errores.extend(
                        f"La aprobacion {bloque.nombre}: {e}"
                        for e in _errores_columna_actor(bloque, participante.get("columna") or "")
                    )
            errores.extend(_errores_rutas_aprobacion(bloque))
            if cfg.get("permite_exclusion") and bloque.rama != BloqueOperativo.Rama.ITEM:
                errores.append(f"La aprobacion {bloque.nombre}: solo la aprobación de un ítem puede excluir.")
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
        elif bloque.tipo == BloqueOperativo.Tipo.TAREAS_ELEMENTOS:
            from apps.catalogo.tareas_elemento import configuracion_activa_de, errores_de_tareas_elemento

            if configuracion_activa_de(version.servicio) is None:
                errores.append(
                    f"El bloque {bloque.nombre} necesita las tareas por elemento del proceso, que no están configuradas "
                    "o están desactivadas."
                )
            else:
                errores.extend(f"El bloque {bloque.nombre}: {e}" for e in errores_de_tareas_elemento(version.servicio))
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
        elif bloque.tipo in BloqueOperativo.TIPOS_CONTENEDOR:
            errores.extend(_errores_de_contenedor(bloque))
        elif bloque.tipo == BloqueOperativo.Tipo.GACETA:
            # Fase 3: el bloque coordina una gaceta ACTIVA del Proceso.
            from apps.catalogo.models import DefinicionGaceta

            clave = cfg.get("gaceta") or ""
            if not DefinicionGaceta.objects.filter(servicio=version.servicio, clave=clave, activa=True).exists():
                errores.append(f"El bloque {bloque.nombre} no indica una gaceta activa del proceso.")
            if cfg.get("esperar", "RESOLUCION") not in ("RESOLUCION", "NINGUNA"):
                errores.append(f"El bloque {bloque.nombre} tiene una espera no válida.")
        errores.extend(_errores_de_cierre_de_alcance(bloque, cfg))
    # 4.G1: un Proceso con programación activa no puede depender de un solicitante humano.
    from apps.catalogo.programacion import (
        bloques_dirigidos_al_solicitante,
        mensaje_bloque_solicitante,
        programacion_activa_de,
    )

    if programacion_activa_de(version.servicio) is not None:
        errores.extend(
            mensaje_bloque_solicitante(nombre)
            for nombre in bloques_dirigidos_al_solicitante(version.servicio, version)
        )
    return errores


# --- Fase 2: contenedores, rutas por rama, referencias a columnas y efectos ------------------------------------


def _coleccion_de(servicio, clave):
    from apps.catalogo.models import DefinicionParrilla

    return DefinicionParrilla.objects.filter(servicio=servicio, clave=clave).first() if clave else None


def _columnas_de_coleccion(servicio, clave):
    """`{clave: Campo}` de la versión activa (o, si no hay, la última) de una colección del Proceso."""
    definicion = _coleccion_de(servicio, clave)
    if definicion is None:
        return {}
    version = definicion.formulario.version_activa or definicion.formulario.versiones.order_by("-numero").first()
    if version is None:
        return {}
    return {campo.clave: campo for campo in version.campos.all() if campo.clave}


def _coleccion_del_recorrido(bloque):
    """Clave de la colección que recorre el contenedor de un bloque de rama ITEM (o "")."""
    if bloque.contenedor_id is None or bloque.rama != BloqueOperativo.Rama.ITEM:
        return ""
    return str((bloque.contenedor.configuracion or {}).get("coleccion") or "")


def _errores_columna_actor(bloque, columna):
    clave = _coleccion_del_recorrido(bloque)
    if not clave:
        return ["un responsable tomado de una columna solo existe en un bloque «para cada ítem» de un recorrido."]
    campo = _columnas_de_coleccion(bloque.version.servicio, clave).get(columna)
    if campo is None or campo.tipo not in ("USUARIO", "EQUIPO"):
        return [f"la columna «{columna}» no es una columna de persona o equipo de la colección recorrida."]
    return []


def _errores_de_referencias(bloque, cfg):
    """Actor por columna y fechas tomadas de un dato (ACTIVIDAD)."""
    errores = []
    if cfg.get("tipo_actor") == "COLUMNA":
        errores.extend(_errores_columna_actor(bloque, cfg.get("columna") or ""))
    for llave, etiqueta in (("fecha_prevista_campo", "fecha prevista"), ("fecha_limite_campo", "fecha límite")):
        referencia = cfg.get(llave) or ""
        if not referencia:
            continue
        ambito, _, clave = referencia.partition(".")
        if ambito == "item":
            coleccion = _coleccion_del_recorrido(bloque)
            campo = _columnas_de_coleccion(bloque.version.servicio, coleccion).get(clave) if coleccion else None
        else:
            formulario = bloque.version.servicio.formulario
            version = formulario.version_activa if formulario is not None else None
            campo = version.campos.filter(clave=clave).first() if version is not None else None
        if campo is None or campo.tipo not in ("FECHA", "FECHA_HORA"):
            errores.append(f"la {etiqueta} sale de «{referencia}», que no es un dato con fecha disponible aquí.")
    # Bloque 3.1: columnas que el responsable de la tarea puede editar en SU ítem.
    editables = actividades.normalizar(cfg)[0]["columnas_editables"]
    if editables:
        coleccion = _coleccion_del_recorrido(bloque)
        if not coleccion:
            errores.append("solo una actividad «para cada ítem» de un recorrido puede permitir editar columnas del ítem.")
        else:
            columnas = _columnas_de_coleccion(bloque.version.servicio, coleccion)
            faltan = [clave for clave in editables if clave not in columnas]
            if faltan:
                errores.append(
                    f"permite editar columnas que la colección recorrida no tiene ({', '.join(faltan)})."
                )
    return errores


def _errores_rutas_aprobacion(bloque):
    """Rutas por resultado de una APROBACION según dónde está: en el flujo principal, exactamente una ruta
    APROBADA, RECHAZADA y DEVUELTA (lo de siempre); en un recorrido por ítem, una ruta DEVUELTA, a lo sumo una
    APROBADA (sin ella, sigue la secuencia) y ninguna de rechazo o exclusión (terminan el recorrido del ítem);
    en una rama general, ninguna (la rama es solo esa aprobación)."""
    resultados = list(bloque.transiciones_salientes.values_list("resultado_aprobacion", flat=True))
    if bloque.contenedor_id is None:
        return [
            f"La aprobacion {bloque.nombre} debe tener exactamente una ruta {esperado}."
            for esperado in ("APROBADA", "RECHAZADA", "DEVUELTA") if resultados.count(esperado) != 1
        ]
    if bloque.rama == BloqueOperativo.Rama.GENERAL:
        return [f"La aprobacion {bloque.nombre} es una actividad general en paralelo: no lleva rutas."] if resultados else []
    errores = []
    if resultados.count("DEVUELTA") != 1:
        errores.append(f"La aprobacion {bloque.nombre} debe indicar a dónde vuelve el ítem si se devuelve.")
    if resultados.count("APROBADA") > 1:
        errores.append(f"La aprobacion {bloque.nombre} tiene más de una ruta para aprobada.")
    if any(r in ("RECHAZADA", "EXCLUIDA") for r in resultados):
        errores.append(
            f"La aprobacion {bloque.nombre}: rechazar o excluir termina el recorrido del ítem; quita esas rutas."
        )
    return errores


def _errores_de_contenedor(bloque):
    contenidos = list(bloque.contenidos.all())
    if not contenidos:
        return [f"«{bloque.nombre}» no tiene actividades: agrégale al menos una."]
    if bloque.tipo != BloqueOperativo.Tipo.RECORRIDO_ITEMS:
        return []
    errores = []
    cfg = bloque.configuracion or {}
    clave = str(cfg.get("coleccion") or "")
    definicion = _coleccion_de(bloque.version.servicio, clave)
    if definicion is None:
        errores.append(f"El recorrido «{bloque.nombre}» no indica una colección de ítems del proceso.")
    elif not definicion.activa:
        errores.append(f"El recorrido «{bloque.nombre}» recorre «{definicion.nombre_elemento}», que está desactivada.")
    condicion = cfg.get("condicion_ingreso") or None
    if condicion:
        variable = str(condicion.get("variable") or "")
        columna = variable.split(".", 1)[1] if variable.startswith("item.") else ""
        if not columna or columna not in _columnas_de_coleccion(bloque.version.servicio, clave):
            errores.append(f"La condición de ingreso de «{bloque.nombre}» no usa una columna de la colección.")
    return errores


def _errores_de_cierre_de_alcance(bloque, cfg):
    from apps.catalogo.models import DefinicionParrilla
    from apps.workflows.efectos import claves_de_alcance

    claves = claves_de_alcance(cfg)
    if not claves:
        return []
    existentes = set(DefinicionParrilla.objects.filter(servicio=bloque.version.servicio).values_list("clave", flat=True))
    faltan = [c for c in claves if c not in existentes]
    return [f"El bloque {bloque.nombre} cierra el alcance de colecciones que el proceso no tiene ({', '.join(faltan)})."] if faltan else []


def clonar_contenido(origen, nueva, *, fase_destino=None, excluir=frozenset()):
    """Copia bloques (con su clave estable), rutas y relaciones de `origen` a la configuración `nueva`.

    Fase 4.A — `fase_destino(fase_origen) -> FaseWorkflow` permite clonar sobre OTRA versión de la plantilla
    (reconciliación): por defecto cada bloque conserva su fase. `excluir`: pks de bloques que el diseñador
    decidió quitar explícitamente (con sus bloques contenidos y las rutas que llegan o salen de ellos). Cuando
    varias fases de origen van a parar a la misma fase destino, sus bloques se renumeran conservando el orden
    relativo (fase de origen, orden). Devuelve `{pk_origen: bloque_nuevo}`."""
    fase_destino = fase_destino or (lambda fase: fase)
    originales = list(origen.bloques.select_related("fase").order_by("fase__orden", "orden", "pk"))
    excluidos = set(excluir)
    cambio = True
    while cambio:  # los contenidos de un contenedor quitado también se quitan
        cambio = False
        for bloque in originales:
            if bloque.contenedor_id in excluidos and bloque.pk not in excluidos:
                excluidos.add(bloque.pk)
                cambio = True
    conservados = [b for b in originales if b.pk not in excluidos]
    destino_de = {}
    for bloque in conservados:
        # Un bloque contenido va siempre en la fase de su contenedor.
        base = bloque if bloque.contenedor_id is None else next(b for b in conservados if b.pk == bloque.contenedor_id)
        destino_de[bloque.pk] = fase_destino(base.fase)
    por_fase = {}
    for bloque in conservados:
        por_fase.setdefault(destino_de[bloque.pk].pk, []).append(bloque)
    orden_de = {}
    for lista in por_fase.values():
        if len({b.fase_id for b in lista}) == 1:
            orden_de.update({b.pk: b.orden for b in lista})
        else:
            lista.sort(key=lambda b: (b.fase.orden, b.orden, b.pk))
            orden_de.update({b.pk: indice for indice, b in enumerate(lista, start=1)})
    clonados = {}
    # Fase 2: primero los bloques del flujo principal (incluidos los contenedores) y después los contenidos,
    # que ya encuentran a su contenedor clonado.
    for bloque in [b for b in conservados if b.contenedor_id is None] + [b for b in conservados if b.contenedor_id]:
        clonados[bloque.pk] = BloqueOperativo.objects.create(
            version=nueva,
            fase=destino_de[bloque.pk],
            tipo=bloque.tipo,
            nombre=bloque.nombre,
            # 4.B0: la clave es la identidad estable del bloque entre versiones.
            clave=bloque.clave,
            # 4.B1: el bloque ENTREGABLE conserva su referencia a la definición.
            definicion_entregable=bloque.definicion_entregable,
            descripcion=bloque.descripcion,
            orden=orden_de[bloque.pk],
            configuracion=bloque.configuracion,
            contenedor=clonados[bloque.contenedor_id] if bloque.contenedor_id else None,
            rama=bloque.rama,
        )
    # 4.E2: la aprobación conserva QUÉ entregable revisa, ahora apuntando al bloque clonado equivalente (por eso
    # se hace en una segunda pasada, con todos creados). Si el revisado se quitó, queda como aprobación general.
    for bloque in conservados:
        if bloque.entregable_revisado_id and bloque.entregable_revisado_id in clonados:
            nuevo_bloque = clonados[bloque.pk]
            nuevo_bloque.entregable_revisado = clonados[bloque.entregable_revisado_id]
            nuevo_bloque.save(update_fields=["entregable_revisado", "actualizado_en"])
    for transicion in TransicionBloqueOperativo.objects.filter(bloque_origen__version=origen).order_by(
        "bloque_origen_id", "prioridad", "pk"
    ):
        if transicion.bloque_origen_id not in clonados:
            continue
        if transicion.bloque_destino_id and transicion.bloque_destino_id not in clonados:
            continue  # la ruta apuntaba a un bloque quitado: la publicación señalará la ruta faltante
        TransicionBloqueOperativo.objects.create(
            bloque_origen=clonados[transicion.bloque_origen_id],
            bloque_destino=clonados[transicion.bloque_destino_id] if transicion.bloque_destino_id else None,
            finaliza=transicion.finaliza,
            nombre=transicion.nombre,
            prioridad=transicion.prioridad,
            variable=transicion.variable,
            operador=transicion.operador,
            valor=transicion.valor,
            es_fallback=transicion.es_fallback,
            resultado_aprobacion=transicion.resultado_aprobacion,
        )
    return clonados


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
    if clonar_desde is not None:
        clonar_contenido(clonar_desde, nueva)
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
    """El borrador en el que se edita la configuración del Servicio.

    Fase 4.A: si la base (borrador o activa) está sobre una versión ANTERIOR de la plantilla, se reconcilia en
    vez de empezar vacía (`apps.catalogo.reconciliacion`): sin conflictos, los bloques y rutas pasan solos a las
    fases equivalentes; con fases desaparecidas, se rechaza para que el diseñador decida (nunca se pierde nada
    en silencio)."""
    from apps.catalogo import reconciliacion

    analisis = reconciliacion.analizar(servicio)
    if analisis is not None:
        if analisis.conflictos:
            raise ValidationError(
                "El flujo cambió y algunas fases con bloques ya no existen ("
                + ", ".join(c.fase.nombre for c in analisis.conflictos)
                + "). Decide en la sección Flujo a dónde mover sus bloques o si los quitas."
            )
        return reconciliacion.reconciliar(servicio, actor)
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
    definicion_entregable=None, entregable_revisado=None, contenedor=None, rama="",
):
    _exigir_administracion(actor)
    if tipo not in BloqueOperativo.TIPOS_CONFIGURABLES:
        # V1 (4.B1): ESPERA ya no es un bloque que un administrador pueda crear.
        raise ValidationError("Este tipo de bloque ya no se puede configurar.")
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    version.exigir_editable()
    if fase.version_id != version.workflow_version_id:
        raise ValidationError("El bloque debe asociarse a una fase de la plantilla configurada.")
    if tipo == BloqueOperativo.Tipo.TAREAS_ELEMENTOS:
        # 4.G2D2: solo un Proceso que ya configuró sus tareas por elemento.
        from apps.catalogo.tareas_elemento import configuracion_activa_de

        if configuracion_activa_de(version.servicio) is None:
            raise ValidationError(
                "Este proceso no tiene configuradas las tareas por elemento: defínelas en la pestaña Formulario "
                "(«Elementos del proceso») antes de agregar este bloque."
            )
    if tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        actividades.validar(configuracion)
    if contenedor is not None:
        # Fase 2: un bloque contenido va en la fase de su contenedor (lo valida el modelo).
        if contenedor.version_id != version.pk:
            raise ValidationError("El contenedor no pertenece a esta configuración.")
        fase = contenedor.fase
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
        contenedor=contenedor,
        rama=rama or "",
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
        if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
            actividades.validar(configuracion)
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
    if bloque.contenidos.exists():
        # Fase 2: un contenedor con actividades no se elimina de golpe (se perdería su configuración).
        raise ValidationError(f"«{bloque.nombre}» todavía tiene actividades: elimínalas primero.")
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
    # Fase 4.A: lo que esta configuración debería contener por lo configurado en otras secciones.
    from apps.catalogo import dependencias

    errores += [d.mensaje for d in dependencias.bloqueantes(servicio, version)]
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


@transaction.atomic
def mover_bloque_operativo(version, bloque, actor, *, direccion):
    """Fase 4.A — sube (`direccion=-1`) o baja (`+1`) un bloque UNA posición entre los de su mismo ámbito (misma
    fase y flujo principal, o mismo contenedor y rama). Cambia el ORDEN DE EJECUCIÓN de la secuencia (no hay
    posición visual separada): por eso es una operación de dominio auditada y solo sobre un borrador. Las rutas
    explícitas de aprobaciones y decisiones no cambian. En el borde, no hace nada."""
    _exigir_administracion(actor)
    if direccion not in (-1, 1):
        raise ValidationError("Dirección de movimiento no válida.")
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    version.exigir_editable()
    bloque = BloqueOperativo.objects.get(pk=bloque.pk, version=version)
    vecinos = version.bloques.filter(fase_id=bloque.fase_id, contenedor_id=bloque.contenedor_id, rama=bloque.rama)
    vecino = (
        vecinos.filter(orden__lt=bloque.orden).order_by("-orden", "-pk").first() if direccion < 0
        else vecinos.filter(orden__gt=bloque.orden).order_by("orden", "pk").first()
    )
    if vecino is None:
        return bloque
    orden_bloque, orden_vecino = bloque.orden, vecino.orden
    # Intercambio en tres pasos para no violar la unicidad (versión, fase, orden).
    temporal = (version.bloques.aggregate(maximo=Max("orden"))["maximo"] or 0) + 1
    BloqueOperativo.objects.filter(pk=bloque.pk).update(orden=temporal)
    BloqueOperativo.objects.filter(pk=vecino.pk).update(orden=orden_bloque)
    BloqueOperativo.objects.filter(pk=bloque.pk).update(orden=orden_vecino)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=bloque, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"orden": orden_bloque, "intercambiado_con": vecino.pk},
        datos_nuevos={"orden": orden_vecino},
    )
    bloque.refresh_from_db()
    return bloque


@transaction.atomic
def cambiar_fase_bloque_operativo(version, bloque, actor, *, fase):
    """Fase 4.B — mueve una actividad del flujo principal a OTRA fase de la misma versión de la plantilla. Es una
    operación FUNCIONAL (cambia cuándo se ejecuta): la actividad queda al FINAL de la fase destino; un contenedor
    se mueve con sus bloques contenidos (que siempre van en la fase de su contenedor). Las rutas explícitas de
    aprobaciones y decisiones no cambian. Solo en borrador y auditada; el lienzo la pide con confirmación."""
    _exigir_administracion(actor)
    version = ConfiguracionEjecucionVersion.objects.select_for_update().get(pk=version.pk)
    version.exigir_editable()
    bloque = BloqueOperativo.objects.select_related("fase").get(pk=bloque.pk, version=version)
    if bloque.contenedor_id is not None:
        raise ValidationError("Un paso dentro de un recorrido o grupo se mueve con su contenedor.")
    if fase.version_id != version.workflow_version_id:
        raise ValidationError("La fase destino no pertenece a la versión del flujo de esta configuración.")
    if fase.pk == bloque.fase_id:
        return bloque
    anterior = {"fase_id": bloque.fase_id, "orden": bloque.orden}
    ultimo = version.bloques.filter(fase=fase).aggregate(maximo=Max("orden"))["maximo"] or 0
    movidos = [bloque, *bloque.contenidos.order_by("orden", "pk")]
    for indice, movido in enumerate(movidos, start=1):
        BloqueOperativo.objects.filter(pk=movido.pk).update(fase=fase, orden=ultimo + indice)
    bloque.refresh_from_db()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=bloque, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores=anterior,
        datos_nuevos={"fase_id": fase.pk, "orden": bloque.orden, "contenidos_movidos": len(movidos) - 1},
    )
    return bloque
