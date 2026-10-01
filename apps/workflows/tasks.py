"""Reanudación automática de `ESPERA` — incremento 3.2.x (RQF-067).

Conecta la infraestructura Celery/Redis ya existente (Sprint 0,
`config/celery.py` + `django_celery_beat` como `CELERY_BEAT_SCHEDULER`) con
`apps.workflows.motor.reanudar_instancia`, ya implementada en 3.2. Este
módulo NO añade lógica de dominio: detecta candidatos y delega — el motor
sigue siendo la única fuente de verdad sobre estados, transiciones,
Strategies, contexto y locking (RN-021 aplicado también aquí, no solo entre
Strategy y motor).

Selección de candidatos: `reanudar_en` ya quedó persistido en 3.2 dentro de
`InstanciaEtapa.resultado` (namespace JSON, sin campo denormalizado — ver
`apps/workflows/estrategias.py::EstrategiaEspera.ejecutar` y
`apps/workflows/motor.py::_ejecutar_etapa`). La consulta de abajo resuelve
la comparación de fecha directamente sobre ese JSON vía `Cast` a
`DateTimeField` (equivalente a `(resultado ->> 'reanudar_en')::timestamptz`
en PostgreSQL) en vez de comparar el texto ISO-8601 como cadena: la
comparación de cadenas es válida solo si todas las marcas de tiempo tienen
el mismo ancho fijo, y `datetime.isoformat()` omite los microsegundos
cuando son exactamente cero — comparar como texto sería una trampa sutil,
no una solución "sin campo nuevo" real. No hace falta agregar
`reanudar_en` como columna propia: el volumen esperado de esperas
pendientes en Órbita no lo justifica.

No recorre el histórico: solo toca la `InstanciaEtapa` vigente de una
instancia (`instancia_workflow.estado=EN_ESPERA` implica que su ejecución
EN_ESPERA es única y es exactamente la última, por construcción secuencial
del motor — no hace falta repetir aquí la lógica de
`_localizar_punto_continuacion`, que ya resuelve "vigente" para el resto de
casos).

Concurrencia/idempotencia: esta tarea no toma ningún lock propio ni agrega
campos tipo `procesado_por_celery`/`scheduler_lock`. `reanudar_instancia`
ya protege la operación real (`select_for_update` + revalidación de estado
bajo lock, W.6) — un candidato que deja de ser válido entre la selección y
el intento (porque otra ejecución de esta misma tarea, u otra reanudación
programática, ya lo procesó) es concurrencia normal: se registra en debug y
se continúa con el resto del lote, nunca se trata como error.

Actor/origen: `reanudar_instancia` no admite ni necesita `actor` para
auditar (3.2 solo audita el inicio, RQF-086) y no llama a
`apps.workflows.autorizacion` — no hay que inventar un usuario técnico ni
verificar `workflows.ejecutar`, que gobierna la reanudación *solicitada por
una persona*, no esta reanudación de sistema (W.8).
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.db.models import DateTimeField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone

from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow
from apps.workflows.motor import reanudar_instancia

logger = logging.getLogger(__name__)


def _candidatos_esperas_vencidas():
    """IDs de `InstanciaWorkflow` con una espera temporal vencida.

    Explícita y acotada a propósito: primero filtra por los dos estados
    EN_ESPERA (instancia + su ejecución vigente) y por
    `motivo_espera=TEMPORAL` (3.3, W.3 — nunca debe seleccionar una
    ejecución bloqueada por una `TAREA` pendiente), y solo entonces evalúa
    `reanudar_en <= ahora` sobre ese subconjunto ya reducido — nunca sobre
    el historial completo de `InstanciaEtapa`."""
    return (
        InstanciaEtapa.objects.filter(
            estado=InstanciaEtapa.Estado.EN_ESPERA,
            motivo_espera=InstanciaEtapa.MotivoEspera.TEMPORAL,
            instancia_workflow__estado=InstanciaWorkflow.Estado.EN_ESPERA,
        )
        .annotate(
            reanudar_en=Cast(
                KeyTextTransform("reanudar_en", "resultado"), output_field=DateTimeField()
            )
        )
        .filter(reanudar_en__lte=timezone.now())
        .values_list("instancia_workflow_id", flat=True)
    )


@shared_task
def reanudar_esperas_vencidas():
    """Tarea periódica (Celery Beat, cada 1 minuto — ver migración
    `0005_seed_periodic_task_reanudar_esperas`). Por cada candidato,
    delega íntegramente en `reanudar_instancia`; una instancia que falla
    (por concurrencia normal, o porque el motor la llevó a `ERROR`) nunca
    impide procesar el resto del lote."""
    procesadas = 0
    for instancia_id in _candidatos_esperas_vencidas():
        try:
            reanudar_instancia(InstanciaWorkflow(pk=instancia_id))
            procesadas += 1
        except ValueError:
            # Dejó de ser un candidato válido entre la selección y el
            # intento (ya reanudada por otra ejecución concurrente de esta
            # misma tarea, o por una reanudación programática aparte) —
            # concurrencia normal, no un error (sección 7/10 del incremento).
            logger.debug(
                "InstanciaWorkflow %s ya no era candidata al reanudar.", instancia_id, exc_info=True
            )
        except Exception:  # noqa: BLE001 — infraestructura/código inesperado; no debe tumbar el lote.
            logger.exception("Error inesperado reanudando InstanciaWorkflow %s.", instancia_id)

    logger.info("reanudar_esperas_vencidas: %s instancia(s) reanudada(s).", procesadas)
    return procesadas
