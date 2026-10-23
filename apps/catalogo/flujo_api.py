"""Interfaz de dominio del flujo de un Servicio/Proceso — Fase 4.A (preparación del lienzo de la 4.B).

Una sola puerta de lectura y escritura para cualquier presentación del flujo (la lista por fases del Studio hoy,
el lienzo visual mañana). No contiene reglas propias: compone `configuracion_ejecucion`, `intenciones`,
`dependencias` y `reconciliacion`.

POSICIÓN VISUAL ≠ CONEXIÓN FUNCIONAL. Aquí solo existe lo que determina la ejecución:

- SECUENCIA: dentro de una fase, los bloques del flujo principal se ejecutan por `orden`; al terminar la fase, el
  flujo sigue a la fase destino de la PRIMERA conexión de fases por prioridad (`motor._siguiente_bloque_lineal`).
  Dentro de un recorrido por ítem, los pasos «para cada ítem» siguen su `orden`; una rama general es un solo paso.
- RUTA EXPLÍCITA (`TransicionBloqueOperativo`): solo la tienen APROBACION (por resultado) y DECISION (condición y
  ruta alternativa); conectan bloques del MISMO ámbito (flujo principal entre sí, o la misma rama del mismo
  contenedor) o finalizan («Finalizar el flujo» / «Terminar el recorrido del ítem»).

No se persisten coordenadas: el futuro lienzo puede guardarlas aparte sin alterar el orden de ejecución.

Restricciones REALES del motor que la 4.B debe respetar (y cambios de motor que requeriría ir más allá):
1. Las conexiones de fases no tienen condición: con varias salidas, el motor solo sigue la primera por prioridad
   (bifurcar por fase exigiría evaluar condiciones en `_siguiente_bloque_lineal`).
2. Un bloque que no es aprobación ni decisión no tiene rutas: su sucesor es el siguiente por orden (conectarlo
   con un bloque arbitrario exigiría rutas «después» en ACTIVIDAD/ENTREGABLE/… y su soporte en el motor).
3. Las rutas no cruzan ámbitos (no entran ni salen de un contenedor) y los contenedores no se anidan.
4. En el recorrido de un ítem, una aprobación no tiene ruta de rechazo/exclusión (terminan el recorrido) y la de
   «devuelta» es obligatoria; en una rama general no hay rutas.
"""

from django.core.exceptions import ValidationError

from apps.catalogo.models import BloqueOperativo, TransicionBloqueOperativo

FINALIZAR = "FIN"


def fases(configuracion):
    """`[{"fase", "clave", "bloques": [BloqueOperativo del flujo principal en orden]}]` en el orden de la plantilla."""
    return [
        {
            "fase": fase, "clave": fase.clave,
            "bloques": list(configuracion.bloques.filter(fase=fase, contenedor__isnull=True).order_by("orden", "pk")),
        }
        for fase in configuracion.workflow_version.fases.order_by("orden", "pk")
    ]


def actividades(configuracion, *, fase=None, contenedor=None, rama=""):
    """Bloques de un ámbito: el flujo principal de `fase`, o la `rama` de `contenedor`."""
    consulta = configuracion.bloques.all()
    if contenedor is not None:
        consulta = consulta.filter(contenedor=contenedor, rama=rama)
    else:
        consulta = consulta.filter(contenedor__isnull=True)
        if fase is not None:
            consulta = consulta.filter(fase=fase)
    return list(consulta.order_by("orden", "pk"))


def conexiones(configuracion):
    """Todas las conexiones funcionales: `[{"origen", "destino" (bloque|None), "finaliza", "tipo", "cuando"}]`.
    `tipo`: "secuencia" (implícita por orden y fases) o "ruta" (explícita de aprobación/decisión)."""
    resultado = []
    explicitas = list(
        TransicionBloqueOperativo.objects.filter(bloque_origen__version=configuracion)
        .select_related("bloque_origen", "bloque_destino").order_by("bloque_origen_id", "prioridad", "pk")
    )
    con_rutas = {t.bloque_origen_id for t in explicitas}
    for transicion in explicitas:
        if transicion.resultado_aprobacion:
            cuando = f"Si es {transicion.get_resultado_aprobacion_display().lower()}"
        elif transicion.es_fallback:
            cuando = "En otro caso"
        else:
            cuando = f"Si {transicion.variable} {transicion.get_operador_display().lower()} {transicion.valor}"
        resultado.append({
            "origen": transicion.bloque_origen, "destino": transicion.bloque_destino, "finaliza": transicion.finaliza,
            "tipo": "ruta", "cuando": cuando, "transicion": transicion,
        })
    from apps.workflows.motor import _siguiente_bloque_lineal

    for bloque in configuracion.bloques.select_related("fase").order_by("fase__orden", "orden", "pk"):
        if bloque.pk in con_rutas or bloque.tipo == BloqueOperativo.Tipo.DECISION:
            continue
        if bloque.tipo == BloqueOperativo.Tipo.APROBACION and bloque.rama != BloqueOperativo.Rama.ITEM and bloque.contenedor_id is None:
            continue  # sin rutas configuradas todavía: no tiene sucesor implícito
        siguiente = _siguiente_bloque_lineal(bloque)
        resultado.append({
            "origen": bloque, "destino": siguiente, "finaliza": siguiente is None, "tipo": "secuencia",
            "cuando": "Después",
        })
    return resultado


def destinos_validos(configuracion, *, origen=None, contenedor=None, rama=""):
    """`[(valor, etiqueta)]` a los que puede ir una ruta desde `origen` (o, sin origen, desde un bloque nuevo del
    ámbito `contenedor`/`rama`): bloques del mismo ámbito más el destino especial de finalizar."""
    if origen is not None:
        contenedor_id, rama = origen.contenedor_id, origen.rama
    else:
        contenedor_id = contenedor.pk if contenedor is not None else None
    bloques = configuracion.bloques.select_related("fase").filter(contenedor_id=contenedor_id, rama=rama or "").order_by(
        "fase__orden", "orden", "pk"
    )
    if origen is not None:
        bloques = bloques.exclude(pk=origen.pk)
    opciones = [(bloque.pk, f"{bloque.fase.nombre} - {bloque.nombre}") for bloque in bloques]
    fin = "Terminar el recorrido del ítem" if contenedor_id is not None else "Finalizar el flujo"
    return opciones + [(FINALIZAR, fin)]


def validar_conexion(origen, destino=None, *, finaliza=False, **reglas):
    """Errores (lista) de conectar `origen` con `destino` (o finalizar) SIN guardar nada: las mismas reglas del
    modelo (`TransicionBloqueOperativo.clean`)."""
    transicion = TransicionBloqueOperativo(bloque_origen=origen, bloque_destino=destino, finaliza=finaliza, **reglas)
    try:
        transicion.clean()
    except ValidationError as exc:
        return list(exc.messages)
    return []


def crear_actividad(servicio, version, actor, *, opcion, fase, nombre, descripcion="", configuracion=None,
                    contenedor=None, rama="", definicion_entregable=None, entregable_revisado=None):
    """Crea un bloque a partir de una OPCIÓN de intención (`intenciones`), revalidando su disponibilidad."""
    from apps.catalogo import intenciones
    from apps.catalogo.configuracion_ejecucion import agregar_bloque_operativo

    tipo = intenciones.exigir_disponible(servicio, opcion, ambito=rama or "", configuracion=version)
    intenciones.exigir_definicion_coherente(opcion, definicion_entregable)
    return agregar_bloque_operativo(
        version, actor, fase=fase, tipo=tipo, nombre=nombre, descripcion=descripcion, configuracion=configuracion,
        definicion_entregable=definicion_entregable, entregable_revisado=entregable_revisado,
        contenedor=contenedor, rama=rama,
    )


def editar_actividad(version, bloque, actor, **cambios):
    from apps.catalogo.configuracion_ejecucion import editar_bloque_operativo

    return editar_bloque_operativo(version, bloque, actor, **cambios)


def reordenar(version, bloque, actor, *, direccion):
    from apps.catalogo.configuracion_ejecucion import mover_bloque_operativo

    return mover_bloque_operativo(version, bloque, actor, direccion=direccion)


def dependencias(servicio, configuracion=None):
    from apps.catalogo import dependencias as modulo

    return modulo.detectar(servicio) if configuracion is None else modulo.detectar(servicio, configuracion)


def guardar(servicio, version, actor):
    """«Activar configuración»: pone en vigor el borrador del flujo (solo esta pieza; publicar es otra acción)."""
    from apps.catalogo.configuracion_ejecucion import activar_configuracion_ejecucion

    return activar_configuracion_ejecucion(servicio, version, actor)
