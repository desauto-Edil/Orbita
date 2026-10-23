"""Dependencias entre piezas de la configuración — Fase 4.A.

El diseñador no debe descubrir dependencias técnicas ocultas: cuando una pieza configurada en una sección
necesita otra pieza del FLUJO para funcionar, aquí se detecta, se explica y se dice cómo completarla.

Niveles:

- BLOQUEANTE: la configuración promete algo que nunca ocurriría. Impide ACTIVAR la configuración del flujo y
  PUBLICAR (`exigir`, llamado por `activar_configuracion_ejecucion` y `activar_servicio`). No se usa en la
  ejecución (`validar_publicacion` de radicación/generación no cambia: lo ya publicado sigue funcionando).
    · Tareas por elemento configuradas sin el paso que las genera (sin ese paso no se crean nunca).
    · Gaceta que «se abre cuando el flujo llega a su bloque» sin ese bloque (nunca se abriría).
    · Gaceta con ingreso «al terminar su recorrido» sin un recorrido por ítem de sus colecciones.
- ADVERTENCIA: funciona, pero quizá no como se espera; no bloquea.
    · Revisión de elementos sin paso en el flujo: se puede revisar en cualquier momento de la atención.
    · Resultado obligatorio sin paso en el flujo: se completa en cualquier momento antes de resolver.

Solo lectura. Completar una dependencia la hace el diseñador desde la interfaz (`accion`): cuando la pieza que
falta es un paso sin configuración propia, la interfaz ofrece agregarlo en la fase que el diseñador elija;
NUNCA se crea un paso sin que el diseñador elija dónde (la posición cambia la ejecución) y nunca se toca una
configuración activa o publicada.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

BLOQUEANTE = "bloqueante"
ADVERTENCIA = "advertencia"

SECCION_FLUJO = ("ejecucion", "")
SECCION_SALIDA = ("salida", "")
SECCION_ELEMENTOS = ("entrada", "parrilla")


@dataclass(frozen=True)
class Dependencia:
    codigo: str
    nivel: str
    mensaje: str
    como_resolver: str
    seccion: tuple
    # Paso que el diseñador puede agregar desde el aviso (opción de `intenciones`) y valores para su formulario.
    agregar_opcion: str = ""
    agregar_datos: tuple = ()

    @property
    def bloquea(self):
        return self.nivel == BLOQUEANTE


_REFERENCIA = object()  # «la configuración que quedará vigente» (distinto de pasar None explícitamente)


def configuracion_de_referencia(servicio):
    """La configuración que quedará vigente: el borrador si existe, si no la activa (o `None`)."""
    from apps.catalogo.models import ConfiguracionEjecucionVersion

    borrador = servicio.configuraciones_ejecucion.filter(
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR
    ).order_by("-numero").first()
    return borrador or servicio.configuracion_ejecucion_activa


def detectar(servicio, configuracion=_REFERENCIA):
    """`[Dependencia]` del Servicio contra `configuracion` (por defecto, la de referencia; `None` = sin
    configuración). Solo un Proceso con configuración por fases tiene estas piezas."""
    from apps.catalogo.models import DefinicionEntregable, DefinicionGaceta
    from apps.catalogo.tareas_elemento import configuracion_activa_de

    if servicio.tipo != "PROCESO" or servicio.es_ticket_general:
        return []
    if configuracion is _REFERENCIA:
        configuracion = configuracion_de_referencia(servicio)
    bloques = list(configuracion.bloques.all()) if configuracion is not None else []
    tipos = {b.tipo for b in bloques}
    dependencias = []

    if configuracion_activa_de(servicio) is not None and "TAREAS_ELEMENTOS" not in tipos:
        dependencias.append(Dependencia(
            "tareas_sin_paso", BLOQUEANTE,
            "Las tareas por elemento están configuradas, pero ningún paso del flujo las genera: no se crearían nunca.",
            "Agrega el paso «Una tarea por cada elemento» en la fase donde deban crearse, o deja de generar tareas "
            "en Formulario › Colecciones.",
            SECCION_FLUJO, agregar_opcion="tareas_elementos",
        ))

    for gaceta in DefinicionGaceta.objects.filter(servicio=servicio, activa=True).order_by("orden", "pk"):
        cfg = gaceta.configuracion or {}
        if cfg.get("apertura") == "POR_FLUJO" and not any(
            b.tipo == "GACETA" and (b.configuracion or {}).get("gaceta") == gaceta.clave for b in bloques
        ):
            dependencias.append(Dependencia(
                f"gaceta_sin_bloque:{gaceta.clave}", BLOQUEANTE,
                f"La gaceta «{gaceta.nombre}» se abre «cuando el flujo llega a su bloque», pero el flujo no tiene ese "
                "paso: nunca se abriría.",
                "Agrega el paso «Coordinar una gaceta» en la fase donde debe abrirse, o cambia la gaceta para que se "
                "abra al iniciar el ticket (Salida › Gacetas).",
                SECCION_FLUJO, agregar_opcion="gaceta", agregar_datos=(("gaceta", gaceta.clave),),
            ))
        if cfg.get("ingreso") == "AL_TERMINAR_RECORRIDO":
            colecciones = set(cfg.get("colecciones") or [])
            if not any(
                b.tipo == "RECORRIDO_ITEMS" and (b.configuracion or {}).get("coleccion") in colecciones for b in bloques
            ):
                dependencias.append(Dependencia(
                    f"gaceta_sin_recorrido:{gaceta.clave}", BLOQUEANTE,
                    f"Los ítems entran a la gaceta «{gaceta.nombre}» «al terminar su recorrido», pero el flujo no "
                    "tiene un recorrido por ítem de sus colecciones: ningún ítem entraría.",
                    "Agrega en el flujo «Cada ítem recorre sus propios pasos» sobre esa colección, o cambia cómo "
                    "ingresan los ítems (Salida › Gacetas).",
                    SECCION_FLUJO,
                ))

    if configuracion is not None:
        en_flujo = {b.definicion_entregable_id for b in bloques if b.tipo == "ENTREGABLE"}
        for definicion in servicio.definiciones_entregables.filter(activo=True).order_by("orden", "pk"):
            if definicion.pk in en_flujo:
                continue
            if definicion.tipo == DefinicionEntregable.Tipo.ELEMENTOS:
                dependencias.append(Dependencia(
                    f"revision_sin_paso:{definicion.pk}", ADVERTENCIA,
                    f"La revisión «{definicion.nombre}» no tiene un paso en el flujo: los elementos se podrán revisar "
                    "en cualquier momento de la atención y el flujo no esperará la revisión.",
                    "Si el flujo debe esperar la revisión, agrega «Revisar y aprobar los elementos uno a uno».",
                    SECCION_FLUJO, agregar_opcion="revision_conjunto", agregar_datos=(("definicion", definicion.pk),),
                ))
            elif definicion.obligatorio:
                dependencias.append(Dependencia(
                    f"resultado_sin_paso:{definicion.pk}", ADVERTENCIA,
                    f"El resultado obligatorio «{definicion.nombre}» no tiene un paso en el flujo: se puede completar en "
                    "cualquier momento, y es obligatorio para resolver.",
                    "Si debe completarse en un punto concreto, agrega «Generar un resultado» allí.",
                    SECCION_FLUJO, agregar_opcion="resultado", agregar_datos=(("definicion", definicion.pk),),
                ))
    return dependencias


def bloqueantes(servicio, configuracion=_REFERENCIA):
    return [d for d in detectar(servicio, configuracion) if d.bloquea]


def exigir(servicio, configuracion=_REFERENCIA):
    """`ValidationError` con las dependencias obligatorias que faltan (nada si no hay)."""
    faltan = bloqueantes(servicio, configuracion)
    if faltan:
        raise ValidationError([d.mensaje for d in faltan])
