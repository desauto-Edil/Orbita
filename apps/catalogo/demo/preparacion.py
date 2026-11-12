"""PREPARAR un caso de la demostración (Bloque 5.6): lo construye con las operaciones de dominio del Studio, en
UNA transacción (si algo falla no queda nada), y lo registra en `RecursoDemo`. No publica el Servicio ni crea
la programación: eso es `fases.publicar`.

Lo único que entra en vigor al preparar es la PLANTILLA DE FASES propia del caso: un Servicio solo se vincula a
una plantilla ya publicada (`ejecucion.vincular_ejecucion`). Es una plantilla nueva de la biblioteca que ningún
otro Servicio usa; el Servicio sigue en borrador (inactivo) con su formulario, colección y configuración del flujo
en BORRADOR hasta `fases.publicar`.

Un caso ya registrado no se vuelve a preparar (idempotencia por `RecursoDemo.caso`, nunca por nombre).
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.catalogo import flujo_api
from apps.catalogo.configuracion_ejecucion import conectar_bloques_operativos, preparar_configuracion_ejecucion
from apps.catalogo.demo import contrato
from apps.catalogo.demo.contrato import Contexto
from apps.catalogo.demo.participantes import Participantes
from apps.catalogo.edicion_formulario import guardar_campo, guardar_opcion, guardar_regla
from apps.catalogo.ejecucion import crear_flujo, publicar_flujo, vincular_ejecucion
from apps.catalogo.entregables import configurar_definicion_entregable
from apps.catalogo.gacetas import configurar_gaceta, configurar_reglas_finalizacion
from apps.catalogo.models import Categoria, FormularioVersion, RecursoDemo, Servicio
from apps.catalogo.operaciones import (
    agregar_responsable,
    asociar_formulario_nuevo,
    conceder_visibilidad,
    configurar_politica_entrega,
    configurar_tiempo_objetivo,
    crear_categoria,
    crear_servicio,
    editar_servicio_general,
)
from apps.catalogo.parrilla import configurar_parrilla
from apps.workflows import fases as fases_ops


def nombre_de(caso, sufijo):
    return caso.nombre if caso.nombre_exacto else f"{caso.nombre}{sufijo}"


def categoria_para(actor, nombre):
    """`(categoria, creada)`: la de otro caso de la demo, una existente con ese nombre (solo se referencia) o
    una nueva."""
    registrada = RecursoDemo.objects.filter(categoria__nombre__iexact=nombre).select_related("categoria").first()
    if registrada is not None:
        return registrada.categoria, registrada.categoria_creada
    existente = Categoria.objects.filter(nombre__iexact=nombre).first()
    if existente is not None:
        if not existente.activo:
            raise ValidationError(f"La categoría «{existente.nombre}» existe pero está inactiva: usa otra con --categoria.")
        return existente, False
    return crear_categoria(actor, nombre=nombre), True


def _borrador(formulario):
    return formulario.versiones.get(estado=FormularioVersion.Estado.BORRADOR)


def _campos(version, actor, specs, *, en_elementos):
    campos = {}
    for orden, spec in enumerate(specs, start=1):
        campo = guardar_campo(
            version, actor, tipo=spec.tipo, etiqueta=spec.etiqueta, clave=spec.clave, ayuda=spec.ayuda,
            obligatorio=spec.obligatorio, orden=orden, es_fecha_requerida=spec.es_fecha_requerida,
            configuracion=spec.configuracion, en_elementos=en_elementos,
        )
        for posicion, (valor, etiqueta) in enumerate(spec.opciones, start=1):
            guardar_opcion(campo, actor, valor=valor, etiqueta=etiqueta, orden=posicion)
        campos[spec.clave] = campo
    return campos


def _plantilla(actor, nombre, nombres_fases):
    """Plantilla de fases propia del caso, en secuencia, y publicada (requisito para vincularla)."""
    workflow = crear_flujo(actor, nombre=nombre, descripcion="Plantilla de la demostración de Órbita.")
    version = workflow.versiones.get(numero=1)
    fases = {}
    anterior = None
    for nombre_fase in nombres_fases:
        fase = fases_ops.agregar_fase(version, actor, nombre=nombre_fase)
        if anterior is not None:
            fases_ops.conectar_fases(anterior, fase, actor)
        fases[nombre_fase] = anterior = fase
    publicar_flujo(workflow, version, actor)
    workflow.refresh_from_db()
    return workflow, fases


def _flujo(servicio, actor, caso, ctx, sufijo):
    workflow, fases = _plantilla(actor, nombre_de(caso, sufijo), caso.fases)
    vincular_ejecucion(servicio, actor, workflow)
    servicio.refresh_from_db()
    configuracion = preparar_configuracion_ejecucion(servicio, actor)
    entregables = {d.nombre: d for d in servicio.definiciones_entregables.filter(activo=True)}
    bloques = {}
    for spec in caso.bloques:
        if spec.requiere and getattr(ctx.p, spec.requiere) is None:
            continue
        bloques[spec.nombre] = flujo_api.crear_actividad(
            servicio, configuracion, actor, opcion=spec.opcion, fase=fases[spec.fase], nombre=spec.nombre,
            descripcion=spec.descripcion, configuracion=spec.configuracion(ctx) if spec.configuracion else None,
            contenedor=bloques[spec.contenedor] if spec.contenedor else None, rama=spec.rama,
            definicion_entregable=entregables[spec.entregable] if spec.entregable else None,
            entregable_revisado=bloques[spec.revisa] if spec.revisa else None,
        )
        # La clave estable de cada bloque ya creado: la configuración de un bloque posterior puede referenciarla.
        ctx.bloques[spec.nombre] = bloques[spec.nombre].clave
    for ruta in caso.rutas:
        if ruta.requiere and getattr(ctx.p, ruta.requiere) is None:
            continue
        destino = bloques[ruta.destino] if ruta.destino else None
        conectar_bloques_operativos(bloques[ruta.origen], destino, actor, **ruta.reglas(ctx))
    # UX-4: el paso por el que entra un ítem enviado se guarda por la CLAVE estable del bloque, que solo existe
    # después de crearlo; se registra en su recorrido con la operación de edición de siempre.
    for spec in caso.bloques:
        if spec.entra_al_enviar and spec.nombre in bloques and spec.contenedor in bloques:
            recorrido = bloques[spec.contenedor]
            recorrido.refresh_from_db()
            flujo_api.editar_actividad(
                configuracion, recorrido, actor,
                configuracion={**(recorrido.configuracion or {}), "paso_al_enviar": bloques[spec.nombre].clave},
            )
    return workflow


def _identidad(servicio, actor, caso, categoria, p):
    """Visibilidad (Público interno, o restringida a un área), responsable, tiempo objetivo y entrega."""
    if caso.visibilidad:
        alcance = Servicio.AlcanceVisibilidad.RESTRINGIDO if p.area_visibilidad else Servicio.AlcanceVisibilidad.PUBLICO_INTERNO
        editar_servicio_general(
            servicio, actor, nombre=servicio.nombre, descripcion=servicio.descripcion, categoria=categoria,
            tipo=servicio.tipo, instrucciones=servicio.instrucciones, alcance_visibilidad=alcance,
        )
        if p.area_visibilidad is not None:
            conceder_visibilidad(servicio, actor, tipo_alcance="AREA", area=p.area_visibilidad)
    tipo, objeto = p.responsable(caso.responsable)
    agregar_responsable(
        servicio, actor, tipo_responsable=tipo,
        usuario=objeto if tipo == "USUARIO" else None, equipo=objeto if tipo == "EQUIPO" else None,
    )
    if caso.tiempo_objetivo:
        cantidad, unidad, habiles = caso.tiempo_objetivo
        configurar_tiempo_objetivo(servicio, actor, cantidad=cantidad, unidad=unidad, habiles=habiles)
    if caso.entrega:
        politica, dias = caso.entrega
        configurar_politica_entrega(servicio, actor, politica=politica, dias_observacion=dias)


@transaction.atomic
def preparar_caso(codigo, actor, p, *, categoria_nombre, sufijo):
    """Construye el caso `codigo` y lo registra. Devuelve `(RecursoDemo, creado)`; si ya estaba registrado no
    toca nada y devuelve `(recurso, False)`."""
    existente = RecursoDemo.objects.filter(caso=codigo).select_related("servicio").first()
    if existente is not None:
        return existente, False
    caso = contrato.CONTRATOS[codigo]
    if caso.personas or caso.equipos:
        # Personas y equipos fijos del contrato (Imperandina): no vienen de la línea de comandos.
        p = Participantes.del_caso(caso, actor)
    faltan = p.faltantes(codigo)
    if faltan:
        raise ValidationError(f"Para preparar «{caso.nombre}» falta indicar: {', '.join(faltan)}.")
    if caso.nombre_exacto and Servicio.objects.filter(nombre__iexact=caso.nombre).exists():
        raise ValidationError(f"Ya existe un servicio llamado «{caso.nombre}» que no es de la demo: renómbralo antes.")

    categoria, categoria_creada = categoria_para(actor, caso.categoria or categoria_nombre)
    servicio = crear_servicio(
        actor, nombre=nombre_de(caso, sufijo), descripcion=caso.descripcion, categoria=categoria, tipo=caso.tipo,
        instrucciones=caso.instrucciones,
    )
    _identidad(servicio, actor, caso, categoria, p)
    ctx = Contexto(p=p)

    if caso.campos:
        formulario = asociar_formulario_nuevo(servicio, actor, nombre=caso.formulario)
        version = _borrador(formulario)
        campos = _campos(version, actor, caso.campos, en_elementos=False)
        for regla in caso.reglas:
            guardar_regla(
                version, actor, campo_origen=campos[regla.origen], operador=regla.operador, valor=regla.valor,
                campo_objetivo=campos[regla.objetivo], efecto=regla.efecto,
            )
    for orden, entregable in enumerate(caso.entregables, start=1):
        configurar_definicion_entregable(
            servicio, actor, nombre=entregable.nombre, tipo=entregable.tipo, descripcion=entregable.descripcion,
            obligatorio=entregable.obligatorio, orden=orden,
        )
    if caso.coleccion:
        definicion = configurar_parrilla(servicio, actor, nombre_elemento=caso.coleccion)
        _campos(_borrador(definicion.formulario), actor, caso.columnas, en_elementos=True)
        ctx.coleccion = definicion.clave
    if caso.gaceta is not None:
        gaceta = configurar_gaceta(
            servicio, actor, nombre=caso.gaceta.nombre, descripcion=caso.gaceta.descripcion,
            configuracion=caso.gaceta.configuracion(ctx),
        )
        ctx.gaceta = gaceta.clave
        ctx.gacetas[caso.gaceta.nombre] = gaceta.clave
    for extra in caso.gacetas_extra:
        gaceta = configurar_gaceta(
            servicio, actor, nombre=extra.nombre, descripcion=extra.descripcion, configuracion=extra.configuracion(ctx),
        )
        ctx.gacetas[extra.nombre] = gaceta.clave
    if caso.finalizacion:
        configurar_reglas_finalizacion(servicio, actor, caso.finalizacion)
    workflow = _flujo(servicio, actor, caso, ctx, sufijo) if caso.fases else None

    # Las operaciones bloquean y guardan su propia copia del Servicio (p. ej. el formulario que asocia
    # `asociar_formulario_nuevo`): se registra el estado persistido, no la copia en memoria.
    servicio.refresh_from_db()
    recurso = RecursoDemo.objects.create(
        caso=codigo, version_contrato=contrato.VERSION_CONTRATO, servicio=servicio, workflow=workflow,
        categoria=categoria, categoria_creada=categoria_creada, preparado_por=actor, parametros=p.a_parametros(),
    )
    return recurso, True
