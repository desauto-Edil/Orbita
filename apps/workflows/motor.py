"""Motor de ejecución de Workflows — incremento 3.2 (CU-020 "Ejecutar
workflow", RQF-065/066/067/068, RN-020/021).

Nota de documentación (W.10): la fuente Excel usa el código `CU-020` dos
veces — una para "Diseñar y versionar workflow" (3.1) y otra, distinta,
para "Ejecutar workflow" (3.2, el CU de este módulo). Es una inconsistencia
del documento fuente; se deja constancia aquí en vez de "corregirla"
silenciosamente.

4 funciones públicas (sección Q de la propuesta aprobada de 3.2; 3.3 agrega
la cuarta, W.7 corrección aprobada): `iniciar_workflow`, `avanzar_instancia`,
`reanudar_instancia`, `continuar_espera_externa`. Comparten dos núcleos
internos privados que nunca se exponen fuera de este módulo:
`_avanzar_automaticamente` (el bucle de encadenado automático) y
`_completar_ejecucion_en_espera` (3.3 — liberar una `InstanciaEtapa`
`EN_ESPERA`, sin importar el motivo, y retomar el avance).

RN-021 en runtime ("el motor de workflow debe delegar el comportamiento de
cada tipo de etapa sin duplicar un motor por dominio"): el motor jamás
pregunta `etapa.tipo` para decidir un comportamiento de dominio — delega
siempre en `ESTRATEGIAS_POR_TIPO[etapa.tipo].ejecutar(...)`. La única rama
que el motor sí resuelve por sí mismo es "¿la Strategy eligió una
transición concreta (CONDICION, vía `transicion_seleccionada`) o debo usar
la única transición saliente ya validada estructuralmente (etapa
ordinaria, W.4)?" — eso es orquestación genérica, no comportamiento de
dominio (sección "Corrección" de la propuesta aprobada).

Autorización: este módulo NO llama a `apps.workflows.autorizacion` — mismo
criterio que `apps/workflows/versionamiento.py` en 3.1 (la capa de dominio
confía en que quien la invoca ya autorizó la operación; hoy nada la invoca
todavía por fuera de las pruebas, ver W.9).

Concurrencia (sección N de la propuesta aprobada): mismo patrón que
`apps/tickets/operaciones.py` — `@transaction.atomic` + `select_for_update()`
sobre la fila de `InstanciaWorkflow` al entrar a `avanzar_instancia`/
`reanudar_instancia`, revalidación de estado bajo lock antes de mutar.
`iniciar_workflow` no lo necesita: crea una fila nueva, sin contención
posible (ningún lector concurrente puede verla antes de confirmarse).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria
from apps.workflows.contexto import (
    aplicar_variables,
    construir_contexto_inicial,
    registrar_resultado_etapa,
)
from apps.workflows.estrategias import ESTRATEGIAS_POR_TIPO, ResultadoEjecucion
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow, WorkflowVersion

logger = logging.getLogger(__name__)

# Protección TÉCNICA de una única invocación síncrona (sección M de la
# propuesta aprobada, W.7) — constante interna, no una variable de entorno:
# no es configuración empresarial, es una salvaguarda de tiempo de
# ejecución. "Límite por invocación" != "límite del Workflow": no declara
# el Workflow inválido (RQF-069 sigue gobernando eso, sin cambios) ni
# limita cuántas veces puede repetirse un ciclo en total — solo acota
# cuántas etapas puede encadenar automáticamente UNA llamada antes de
# devolver el control. Alcanzarlo NO pone la instancia en ERROR: es un
# corte técnico esperado, no una falla — `avanzar_instancia` retoma
# exactamente donde se cortó.
LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION = 100


@dataclass
class _PuntoContinuacion:
    tipo: str  # "RETOMAR" | "CREAR" | "TERMINADO"
    instancia_etapa: InstanciaEtapa | None = None
    etapa_destino: object = None  # Etapa — sin importar la clase, evita un ciclo de import innecesario
    orden: int | None = None


def _localizar_punto_continuacion(instancia):
    """Única fuente de verdad de "¿por dónde continúa esta instancia?"
    (W.2, corrección aprobada) — el resto del motor nunca repite esta
    lógica en otra consulta.

    - Si la última `InstanciaEtapa` (por `orden`) está PENDIENTE,
      EN_EJECUCION o EN_ESPERA, ES el punto de continuación: el motor la
      retoma/re-ejecuta tal cual.
    - Si está COMPLETADA y tiene `transicion_tomada`, el punto de
      continuación es una `InstanciaEtapa` nueva (todavía no creada) para
      `transicion_tomada.etapa_destino`, en el siguiente `orden` — este es
      el caso que resuelve, sin ambigüedad y sin volver a invocar a
      ninguna Strategy, una instancia que quedó cortada por
      `LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION`.
    - Si está COMPLETADA sin `transicion_tomada` (FIN alcanzado), no hay
      nada que continuar.
    """
    ultima = instancia.ejecuciones_etapa.order_by("-orden").first()
    if ultima is None:
        raise ValueError("La instancia no tiene ninguna InstanciaEtapa — estado inconsistente.")

    if ultima.estado in (
        InstanciaEtapa.Estado.PENDIENTE,
        InstanciaEtapa.Estado.EN_EJECUCION,
        InstanciaEtapa.Estado.EN_ESPERA,
    ):
        return _PuntoContinuacion(tipo="RETOMAR", instancia_etapa=ultima)

    if ultima.estado == InstanciaEtapa.Estado.COMPLETADA and ultima.transicion_tomada_id:
        return _PuntoContinuacion(
            tipo="CREAR",
            etapa_destino=ultima.transicion_tomada.etapa_destino,
            orden=ultima.orden + 1,
        )

    return _PuntoContinuacion(tipo="TERMINADO")


def _resolver_transicion_saliente(etapa, resultado):
    """CONDICION decide su propia transición (`transicion_seleccionada`,
    RN-021); cualquier otra etapa ejecutable usa la única transición
    saliente que `validar_estructura` ya garantizó (W.4). FIN no tiene
    ninguna: devuelve `None`."""
    if resultado.transicion_seleccionada is not None:
        return resultado.transicion_seleccionada
    return etapa.transiciones_salientes.first()


def _registrar_error(instancia, instancia_etapa, *, tipo, mensaje):
    """`tipo` es `"FUNCIONAL"` (la propia Strategy decidió `ERROR`) o
    `"TECNICO"` (excepción no esperada, o Strategy no disponible). Nunca
    persiste un traceback — eso solo va a logging (sección O de la
    propuesta aprobada). No toca `finalizada_en`: ERROR no es una
    terminación empresarial."""
    instancia_etapa.estado = InstanciaEtapa.Estado.ERROR
    instancia_etapa.error = {"tipo": tipo, "mensaje": mensaje}
    instancia_etapa.save(update_fields=["estado", "error", "actualizado_en"])

    instancia.estado = InstanciaWorkflow.Estado.ERROR
    instancia.save(update_fields=["estado", "actualizado_en"])


def _ejecutar_etapa(instancia, instancia_etapa):
    """Ejecuta la Strategy de `instancia_etapa.etapa`, persiste su
    resultado y aplica los cambios de contexto que haya devuelto (la
    Strategy nunca escribe `instancia.contexto` directamente — sección D
    de la propuesta aprobada).

    Devuelve `True` si el bucle de `_avanzar_automaticamente` debe seguir
    encadenando (la etapa se completó con `CONTINUAR`), `False` si debe
    detenerse (ESPERAR, COMPLETAR o ERROR: en los tres casos ya se dejó a
    la instancia en el estado correspondiente)."""
    etapa = instancia_etapa.etapa
    estrategia = ESTRATEGIAS_POR_TIPO.get(etapa.tipo)

    instancia_etapa.estado = InstanciaEtapa.Estado.EN_EJECUCION
    instancia_etapa.iniciada_en = timezone.now()
    instancia_etapa.save(update_fields=["estado", "iniciada_en", "actualizado_en"])

    if estrategia is None or not estrategia.ejecutable:
        # Defensa adicional: `validar_estructura` ya impide activar una
        # versión con un tipo no ejecutable (W.5) — esto solo protege
        # contra una escritura masiva que bypasee esa validación, mismo
        # criterio que ya documenta `apps/workflows/validacion.py`.
        _registrar_error(
            instancia,
            instancia_etapa,
            tipo="TECNICO",
            mensaje=f"No hay una Strategy de ejecución disponible para el tipo {etapa.tipo}.",
        )
        return False

    try:
        resultado = estrategia.ejecutar(instancia_etapa, instancia.contexto)
    except Exception as exc:  # noqa: BLE001 — error técnico inesperado; no debe tumbar el proceso.
        logger.exception("Error técnico ejecutando InstanciaEtapa %s", instancia_etapa.pk)
        _registrar_error(
            instancia, instancia_etapa, tipo="TECNICO", mensaje=str(exc) or type(exc).__name__
        )
        return False

    if resultado.estado == ResultadoEjecucion.ERROR:
        _registrar_error(
            instancia,
            instancia_etapa,
            tipo="FUNCIONAL",
            mensaje=resultado.mensaje_error or "Error funcional sin mensaje.",
        )
        return False

    if resultado.estado == ResultadoEjecucion.ESPERAR:
        registrar_resultado_etapa(instancia.contexto, etapa.pk, resultado.datos)
        aplicar_variables(instancia.contexto, resultado.variables_actualizadas)
        instancia_etapa.estado = InstanciaEtapa.Estado.EN_ESPERA
        instancia_etapa.resultado = resultado.datos
        # 3.3 (W.3): el motor copia `motivo_espera` tal cual — nunca lo
        # infiere de `etapa.tipo` (RN-021, mismo criterio que
        # `transicion_seleccionada`).
        instancia_etapa.motivo_espera = resultado.motivo_espera
        instancia_etapa.save(update_fields=["estado", "resultado", "motivo_espera", "actualizado_en"])
        instancia.estado = InstanciaWorkflow.Estado.EN_ESPERA
        instancia.save(update_fields=["estado", "contexto", "actualizado_en"])
        return False

    # CONTINUAR o COMPLETAR: la etapa terminó exitosamente.
    transicion = _resolver_transicion_saliente(etapa, resultado)

    registrar_resultado_etapa(instancia.contexto, etapa.pk, resultado.datos)
    aplicar_variables(instancia.contexto, resultado.variables_actualizadas)
    instancia_etapa.estado = InstanciaEtapa.Estado.COMPLETADA
    instancia_etapa.resultado = resultado.datos
    instancia_etapa.finalizada_en = timezone.now()
    instancia_etapa.transicion_tomada = transicion
    instancia_etapa.save(
        update_fields=["estado", "resultado", "finalizada_en", "transicion_tomada", "actualizado_en"]
    )

    if resultado.estado == ResultadoEjecucion.COMPLETAR:
        instancia.estado = InstanciaWorkflow.Estado.COMPLETADA
        instancia.finalizada_en = timezone.now()
        instancia.save(update_fields=["estado", "contexto", "finalizada_en", "actualizado_en"])
        return False

    instancia.save(update_fields=["contexto", "actualizado_en"])
    return True


def _avanzar_automaticamente(instancia):
    """Bucle interno compartido por las 3 funciones públicas — nunca se
    llama directamente desde fuera de este módulo, y siempre bajo la
    `@transaction.atomic`/`select_for_update()` que ya tomó su llamador.

    Encadena ejecuciones mientras la Strategy de cada etapa devuelva
    `CONTINUAR`, hasta que ocurra uno de:
    - `ESPERAR` (RQF-067) — la instancia queda `EN_ESPERA`.
    - `COMPLETAR` (solo FIN) — la instancia queda `COMPLETADA`.
    - `ERROR` (funcional o técnico) — la instancia queda `ERROR`.
    - `LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION` alcanzado — la instancia
      queda `EN_EJECUCION` (no `ERROR`: protección técnica, no de negocio).
    """
    pasos = 0
    while True:
        punto = _localizar_punto_continuacion(instancia)
        if punto.tipo == "TERMINADO":
            return instancia

        if pasos >= LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION:
            instancia.estado = InstanciaWorkflow.Estado.EN_EJECUCION
            instancia.save(update_fields=["estado", "actualizado_en"])
            return instancia

        if punto.tipo == "CREAR":
            instancia_etapa = InstanciaEtapa.objects.create(
                instancia_workflow=instancia,
                etapa=punto.etapa_destino,
                orden=punto.orden,
                estado=InstanciaEtapa.Estado.PENDIENTE,
            )
        else:  # "RETOMAR"
            instancia_etapa = punto.instancia_etapa

        pasos += 1
        if not _ejecutar_etapa(instancia, instancia_etapa):
            return instancia


@transaction.atomic
def iniciar_workflow(workflow, *, actor=None, origen=RegistroAuditoria.Origen.USUARIO, datos_iniciales=None):
    """RQF-065, CU-020 "Ejecutar workflow". API de dominio programática
    (W.9): sin botón de Admin en 3.2, se invoca directamente (pruebas, o un
    futuro Sprint 5/Proceso).

    Exige una versión ACTIVA — RN-020: la instancia queda atada para
    siempre a `workflow.version_activa` en este momento, sin importar qué
    versión se active después.
    """
    version = workflow.version_activa
    if version is None or version.estado != WorkflowVersion.Estado.ACTIVA:
        raise ValueError("El workflow no tiene una versión activa para ejecutar.")

    inicio = version.etapas.filter(tipo="INICIO").first()
    if inicio is None:
        raise ValueError("La versión activa no tiene una etapa INICIO — estado inconsistente.")

    instancia = InstanciaWorkflow.objects.create(
        workflow_version=version,
        estado=InstanciaWorkflow.Estado.EN_EJECUCION,
        contexto=construir_contexto_inicial(datos_iniciales),
        iniciado_por=actor,
    )
    InstanciaEtapa.objects.create(
        instancia_workflow=instancia,
        etapa=inicio,
        orden=1,
        estado=InstanciaEtapa.Estado.PENDIENTE,
    )

    # Único evento de auditoría de alto nivel (sección P de la propuesta
    # aprobada): el avance automático NO se audita paso a paso —
    # `InstanciaEtapa` ya es ese historial operacional (RQF-086).
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=instancia,
        origen=origen,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(instancia),
    )

    return _avanzar_automaticamente(instancia)


@transaction.atomic
def avanzar_instancia(instancia, *, actor=None):
    """Reintento explícito tras el corte técnico de
    `LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION` (sección M) — el único caso
    documentado en 3.2 en el que una instancia queda en reposo en
    `EN_EJECUCION`. `actor` se acepta por simetría con las otras dos
    funciones públicas, aunque 3.2 no audita este reintento (solo el
    inicio, sección P)."""
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=instancia.pk)
    if instancia.estado != InstanciaWorkflow.Estado.EN_EJECUCION:
        raise ValueError(
            f"Solo se puede avanzar una instancia en estado EN_EJECUCION (está en {instancia.estado})."
        )
    return _avanzar_automaticamente(instancia)


def _completar_ejecucion_en_espera(instancia, ejecucion, *, transicion_seleccionada=None):
    """Núcleo compartido genérico (W.7, corrección aprobada; 3.4 agrega
    `transicion_seleccionada`): marca `ejecucion` COMPLETADA y deja la
    instancia EN_EJECUCION para retomar el avance automático.

    `transicion_seleccionada=None` (default) preserva EXACTAMENTE el
    comportamiento anterior: la única transición saliente ya validada
    estructuralmente (W.4, TAREA/ESPERA/etc. — `TIPOS_SALIDA_UNICA`). Con
    una transición explícita (3.4, APROBACION — que necesita elegir entre
    varias salidas nombradas, algo que TAREA/ESPERA nunca necesitaron),
    esta se usa tal cual, sin caer nunca en `.first()` — mismo criterio
    exacto que `_resolver_transicion_saliente` ya aplica en el camino
    síncrono para CONDICION vía `resultado.transicion_seleccionada` (RN-021:
    el motor no decide la semántica, solo aplica lo que el dominio ya
    resolvió).

    Nunca decide POR QUÉ es válido completar `ejecucion` en este momento
    — esa validación específica del motivo (¿venció `reanudar_en`? ¿la
    Tarea/Aprobación externa ya se resolvió?) es responsabilidad exclusiva
    de cada función pública que llama a esta, ANTES de invocar el núcleo.
    Así se generaliza el punto de continuación sin duplicar el motor, y
    sin que este núcleo necesite saber qué es una Tarea, una Aprobación,
    un Ticket o una Gaceta."""
    ejecucion.estado = InstanciaEtapa.Estado.COMPLETADA
    ejecucion.finalizada_en = timezone.now()
    ejecucion.transicion_tomada = transicion_seleccionada or ejecucion.etapa.transiciones_salientes.first()
    ejecucion.save(update_fields=["estado", "finalizada_en", "transicion_tomada", "actualizado_en"])

    instancia.estado = InstanciaWorkflow.Estado.EN_EJECUCION
    instancia.save(update_fields=["estado", "actualizado_en"])

    return _avanzar_automaticamente(instancia)


@transaction.atomic
def reanudar_instancia(instancia, *, actor=None):
    """RQF-067. Revalida bajo lock (W.6/3.2, corrección aprobada): la
    instancia debe estar `EN_ESPERA`, su ejecución vigente también, su
    `motivo_espera` debe ser TEMPORAL (3.3, W.3 — defensa adicional: nunca
    reanuda por esta vía una espera bloqueada por una Tarea externa), y
    `reanudar_en` ya debe haber pasado — una llamada anticipada no puede
    saltarse la espera, sin importar quién la invoque. Conectada a Celery
    Beat desde 3.2.x (`apps.workflows.tasks.reanudar_esperas_vencidas`)."""
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=instancia.pk)
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        raise ValueError(
            f"Solo se puede reanudar una instancia en estado EN_ESPERA (está en {instancia.estado})."
        )

    punto = _localizar_punto_continuacion(instancia)
    if punto.tipo != "RETOMAR" or punto.instancia_etapa.estado != InstanciaEtapa.Estado.EN_ESPERA:
        raise ValueError("La instancia no tiene una ejecución vigente en espera — estado inconsistente.")
    ejecucion = punto.instancia_etapa

    if ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.TEMPORAL:
        raise ValueError(
            f"Esta ejecución no está en espera temporal (motivo_espera={ejecucion.motivo_espera})."
        )

    reanudar_en = ejecucion.resultado.get("reanudar_en")
    if not reanudar_en or datetime.fromisoformat(reanudar_en) > timezone.now():
        raise ValueError("Todavía no corresponde reanudar esta instancia (reanudar_en no alcanzado).")

    return _completar_ejecucion_en_espera(instancia, ejecucion)


@transaction.atomic
def continuar_espera_externa(instancia_etapa, *, motivo_espera, transicion_seleccionada=None):
    """Núcleo genérico para liberar una espera EXTERNA (no temporal) —
    3.3, W.7 (corrección aprobada); 3.4 agrega `transicion_seleccionada`.
    La usa `apps.workflows.integracion.completar_tarea_workflow` (TAREA,
    siempre `transicion_seleccionada=None` — una sola salida ya validada,
    W.4) y `apps.workflows.integracion.resolver_aprobacion_workflow`
    (APROBACION, que SÍ pasa una transición explícita — necesita elegir
    entre APROBADA/RECHAZADA/DEVUELTA). Un futuro TICKET/GACETA reutiliza
    esta MISMA función con su propio `motivo_espera`, sin agregar
    `continuar_desde_<dominio>()` por cada uno (instrucción explícita:
    evitar trasladar aquí el `if` por tipo que ya se evitó en el resto del
    motor).

    `transicion_seleccionada=None` preserva EXACTAMENTE el comportamiento
    anterior (TAREA/ESPERA no cambian funcionalmente — ver pruebas de
    regresión explícitas). Cuando se pasa una transición, se valida que
    pertenezca a la etapa actual antes de usarla — nunca se cae en
    `.first()` en ese caso (tomarla sin validar podría, ante un error de
    quien llama, continuar el Workflow por una rama de OTRA etapa).

    La validación de que el motivo efectivamente se cumplió (p.ej. que la
    Tarea ya quedó COMPLETADA, o que el `EsquemaAprobacion` ya tiene
    `resultado`) es responsabilidad de quien llama, ANTES de invocar esto
    — este núcleo solo bloquea, revalida estado/motivo genéricos y avanza,
    igual que `reanudar_instancia` con ESPERA temporal. El motor sigue sin
    importar `apps.tareas`/`apps.aprobaciones` ni ningún otro dominio
    externo: recibe `instancia_etapa` (ya suya), una cadena
    (`motivo_espera`) y, opcionalmente, una `TransicionEtapa` ya resuelta
    por el dominio que llama (RN-021: el motor nunca decide cuál)."""
    instancia_etapa = InstanciaEtapa.objects.select_for_update().get(pk=instancia_etapa.pk)
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=instancia_etapa.instancia_workflow_id)

    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        raise ValueError(
            f"Solo se puede continuar una instancia en estado EN_ESPERA (está en {instancia.estado})."
        )
    if instancia_etapa.estado != InstanciaEtapa.Estado.EN_ESPERA:
        raise ValueError("La ejecución indicada no está EN_ESPERA — estado inconsistente.")
    if instancia_etapa.motivo_espera != motivo_espera:
        raise ValueError(
            f"Motivo de espera inesperado: se esperaba {motivo_espera}, "
            f"la ejecución está en espera por {instancia_etapa.motivo_espera}."
        )
    if transicion_seleccionada is not None and transicion_seleccionada.etapa_origen_id != instancia_etapa.etapa_id:
        raise ValueError("transicion_seleccionada no pertenece a la etapa de esta ejecución.")

    return _completar_ejecucion_en_espera(instancia, instancia_etapa, transicion_seleccionada=transicion_seleccionada)
