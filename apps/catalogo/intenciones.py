"""Capa de traducción «¿Qué necesitas que suceda?» → bloques existentes — Fase 4.A.

El diseñador no elige bloques técnicos: elige una INTENCIÓN («Realizar una tarea», «Revisar o aprobar»…) y,
cuando hay más de una forma de cumplirla, una OPCIÓN. Cada opción se traduce a UN tipo de `BloqueOperativo`
existente con valores iniciales de su formulario (presets de experiencia, nunca configuración oculta). No hay
motor, modelo ni contrato de ejecución nuevo: el bloque resultante se crea, valida y ejecuta exactamente igual
que si se hubiera elegido el tipo técnico (`configuracion_ejecucion.agregar_bloque_operativo`).

    INTENCIÓN DEL DISEÑADOR → OPCIÓN → tipo de BloqueOperativo (+ valores iniciales) → motor actual

Disponibilidad: una opción puede no aplicar al tipo (un Servicio no trabaja con colecciones ni gacetas: no se
ofrece) o necesitar otra pieza primero (un resultado definido en Salida, una colección, una gaceta…): entonces se
muestra deshabilitada con el motivo y dónde completarlo, en vez de esconder la capacidad. El servidor vuelve a
comprobarlo al crear (`exigir_disponible`).
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

# Ámbitos donde se agrega: flujo principal, o dentro de un contenedor por rama.
PRINCIPAL = ""
RAMA_ITEM = "ITEM"
RAMA_GENERAL = "GENERAL"


@dataclass(frozen=True)
class Opcion:
    codigo: str
    etiqueta: str
    descripcion: str
    tipo_bloque: str
    ambitos: tuple = (PRINCIPAL, RAMA_ITEM, RAMA_GENERAL)
    solo_proceso: bool = False
    iniciales: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Intencion:
    codigo: str
    etiqueta: str
    descripcion: str
    opciones: tuple


INTENCIONES = (
    Intencion("tarea", "Realizar una tarea", "Alguien hace un trabajo y lo marca como terminado.", (
        Opcion("tarea", "Una tarea", "Una persona, un equipo o el responsable del ticket la realiza.", "ACTIVIDAD"),
        Opcion(
            "tareas_paralelo", "Varias tareas al mismo tiempo",
            "Se lanzan juntas y el flujo sigue cuando terminan todas.", "GRUPO_ACTIVIDADES", ambitos=(PRINCIPAL,),
        ),
    )),
    Intencion("informacion", "Solicitar información o evidencias", "Alguien aporta datos o archivos que respaldan el trabajo.", (
        Opcion(
            "informacion", "Pedir información o evidencias",
            "Una tarea que exige adjuntar evidencia para completarse (y, si quieres, los datos de inicio completos).",
            "ACTIVIDAD", iniciales={"requiere_evidencia": True},
        ),
    )),
    Intencion("resultado", "Generar un resultado", "Un resultado definido en Salida debe quedar completo para continuar.", (
        Opcion(
            "resultado", "Un resultado del ticket", "Texto, enlace, archivo o confirmación definidos en Salida.",
            "ENTREGABLE", ambitos=(PRINCIPAL,),
        ),
    )),
    Intencion("aprobar", "Revisar o aprobar", "Una o varias personas deciden: aprobar, devolver o rechazar.", (
        Opcion("aprobacion", "Una aprobación", "Quién aprueba y, si son varios, si deben aprobar todos o cualquiera.", "APROBACION"),
    )),
    Intencion("coleccion", "Trabajar con elementos de una colección", "Cada ítem de una colección recibe su propio trabajo.", (
        Opcion(
            "recorrido", "Cada ítem recorre sus propios pasos",
            "Tareas, aprobaciones y condiciones por ítem, cada uno a su ritmo.", "RECORRIDO_ITEMS",
            ambitos=(PRINCIPAL,), solo_proceso=True,
        ),
        Opcion(
            "tareas_elementos", "Una tarea por cada elemento",
            "Usa las columnas elegidas en Formulario › Colecciones › Tareas por elemento.", "TAREAS_ELEMENTOS",
            ambitos=(PRINCIPAL,), solo_proceso=True,
        ),
        Opcion(
            "revision_conjunto", "Revisar y aprobar los elementos uno a uno",
            "El flujo espera a que la revisión por conjunto definida en Salida quede aprobada.", "ENTREGABLE",
            ambitos=(PRINCIPAL,), solo_proceso=True,
        ),
    )),
    Intencion("colaborativa", "Realizar una revisión colaborativa", "Una gaceta: observaciones, correcciones y decisiones sobre los ítems.", (
        Opcion(
            "gaceta", "Coordinar una gaceta", "Abre la gaceta, presenta los ítems y, si quieres, espera su resolución.",
            "GACETA", ambitos=(PRINCIPAL,), solo_proceso=True,
        ),
    )),
    Intencion("condicion", "Evaluar una condición", "El camino sigue por una ruta u otra según un dato.", (
        Opcion("decision", "Una condición", "Si se cumple, va a un paso; si no, a otro.", "DECISION", ambitos=(PRINCIPAL, RAMA_ITEM)),
    )),
)

OPCIONES = {opcion.codigo: opcion for intencion in INTENCIONES for opcion in intencion.opciones}
# Opción por defecto de cada tipo técnico (para presentar un bloque ya creado en lenguaje de intención).
INTENCION_DE_TIPO = {
    "ACTIVIDAD": "Realizar una tarea", "GRUPO_ACTIVIDADES": "Realizar una tarea",
    "ENTREGABLE": "Generar un resultado", "APROBACION": "Revisar o aprobar", "DECISION": "Evaluar una condición",
    "RECORRIDO_ITEMS": "Trabajar con elementos de una colección",
    "TAREAS_ELEMENTOS": "Trabajar con elementos de una colección",
    "GACETA": "Realizar una revisión colaborativa", "ESPERA": "Espera (heredada)",
}


def opcion(codigo):
    """La `Opcion` de `codigo` o `ValidationError` si no existe."""
    try:
        return OPCIONES[codigo]
    except KeyError:
        raise ValidationError("Elige qué necesitas que suceda.")


def traducir(codigo):
    """Tipo de `BloqueOperativo` en el que se traduce la opción `codigo`."""
    return opcion(codigo).tipo_bloque


def _es_proceso(servicio):
    return servicio.tipo == "PROCESO" and not servicio.es_ticket_general


def _motivo(servicio, op, configuracion):
    """`(motivo, (pestaña, uso))` de por qué `op` aún no se puede agregar, o `(None, None)`."""
    from apps.catalogo.models import DefinicionEntregable, DefinicionGaceta
    from apps.catalogo.parrilla import definiciones_de
    from apps.catalogo.tareas_elemento import configuracion_activa_de

    definiciones = servicio.definiciones_entregables.filter(activo=True)
    if op.codigo == "resultado" and not definiciones.exclude(tipo=DefinicionEntregable.Tipo.ELEMENTOS).exists():
        return (
            f"Este {servicio.get_tipo_display().lower()} todavía no tiene entregables configurados: define primero el "
            "resultado en Salida (texto, enlace, archivo o confirmación).", ("salida", "")
        )
    if op.codigo == "revision_conjunto" and not definiciones.filter(tipo=DefinicionEntregable.Tipo.ELEMENTOS).exists():
        return "Primero define una revisión de elementos en Salida › Revisiones.", ("salida", "")
    if op.codigo == "recorrido" and not any(d.activa for d in definiciones_de(servicio)):
        return "Necesita una colección de ítems activa (Formulario › Colecciones).", ("entrada", "parrilla")
    if op.codigo == "tareas_elementos":
        if configuracion_activa_de(servicio) is None:
            return "Primero elige las columnas de la tarea en Formulario › Colecciones › Tareas por elemento.", ("entrada", "parrilla")
        if configuracion is not None and configuracion.bloques.filter(tipo="TAREAS_ELEMENTOS").exists():
            return "El flujo ya genera las tareas por elemento (solo puede haber un paso así).", ("ejecucion", "")
    if op.codigo == "gaceta" and not DefinicionGaceta.objects.filter(servicio=servicio, activa=True).exists():
        return "Primero crea la gaceta en Salida › Gacetas.", ("salida", "")
    return None, None


def opciones_para(servicio, *, ambito=PRINCIPAL, configuracion=None):
    """Intenciones que se ofrecen en `ambito` para `servicio`: `[{intencion, opciones: [{opcion, disponible,
    motivo, seccion}]}]`. Las opciones que no aplican al tipo (Servicio) o al ámbito no aparecen; las que
    necesitan otra pieza aparecen deshabilitadas con el motivo."""
    es_proceso = _es_proceso(servicio)
    resultado = []
    for intencion in INTENCIONES:
        filas = []
        for op in intencion.opciones:
            if ambito not in op.ambitos or (op.solo_proceso and not es_proceso):
                continue
            motivo, seccion = _motivo(servicio, op, configuracion)
            filas.append({"opcion": op, "disponible": motivo is None, "motivo": motivo, "seccion": seccion})
        if filas:
            resultado.append({"intencion": intencion, "opciones": filas})
    return resultado


def exigir_disponible(servicio, codigo, *, ambito=PRINCIPAL, configuracion=None):
    """Revalida en el servidor que la opción `codigo` se puede agregar en `ambito` y devuelve su tipo de bloque."""
    op = opcion(codigo)
    if ambito not in op.ambitos:
        raise ValidationError(f"«{op.etiqueta}» no se puede agregar aquí.")
    if op.solo_proceso and not _es_proceso(servicio):
        raise ValidationError(f"«{op.etiqueta}» solo existe en un proceso.")
    motivo, _ = _motivo(servicio, op, configuracion)
    if motivo:
        raise ValidationError(motivo)
    return op.tipo_bloque


def exigir_definicion_coherente(codigo, definicion):
    """Un resultado simple no puede apuntar a una revisión de elementos, ni al revés."""
    if definicion is None:
        return
    es_revision = definicion.tipo == "ELEMENTOS"
    if codigo == "resultado" and es_revision:
        raise ValidationError("Para revisar elementos usa «Trabajar con elementos de una colección».")
    if codigo == "revision_conjunto" and not es_revision:
        raise ValidationError("Elige una revisión de elementos definida en Salida.")
