"""Domain Events (Sprint 7, adelanto autorizado) — RQF-108, RN-032.

Las operaciones de dominio PUBLICAN lo que ya ocurrió con una llamada explícita (`publicar`), en los mismos puntos
donde registran su historia; nunca con señales de Django (mismo criterio de la auditoría). El evento se acumula
mientras dure la transacción y se entrega al despacho con `transaction.on_commit`:

- si la operación se deshace, no hay aviso;
- si el aviso falla, la operación ya quedó hecha (el despacho captura y registra cualquier error).

Los eventos de una misma transacción se despachan JUNTOS, así el despacho puede agrupar (5 tareas por elemento =
un aviso por persona) y suprimir duplicados (la observación de una gaceta ya avisa de su tarea de corrección).

Este módulo no conoce destinatarios ni mensajes: eso es `apps.notificaciones.despacho`."""

import logging
from dataclasses import dataclass, field

from django.db import transaction

logger = logging.getLogger(__name__)

_ATRIBUTO = "_orbita_eventos_pendientes"


@dataclass
class Evento:
    clave: str
    actor_id: int | None = None
    ticket_id: int | None = None
    # Objeto del aviso (sin GenericForeignKey): `{"tarea": 3}`, `{"aprobacion": 8}`, `{"gaceta": 2}`, …
    objetos: dict = field(default_factory=dict)
    datos: dict = field(default_factory=dict)


def _pendientes():
    """La lista de eventos de la transacción en curso, con UNA sola entrega por transacción.

    - Si una transacción anterior se deshizo, su `on_commit` se descartó: se detecta porque la función de entrega ya
      no está en la cola de la conexión, y se empieza una lista nueva (nunca se arrastran eventos deshechos).
    - Cada evento nuevo vuelve a encolar la MISMA entrega al final: la primera en
      correr entrega todo el lote y las demás no hacen nada (la lista ya quedó vacía). Así un savepoint deshecho no se
      lleva los eventos anteriores y cualquier captura de `on_commit` que contenga el último evento lo incluye. `run_on_commit` es la cola interna de Django
      (tuplas `(savepoints, función, robusta)`)."""
    conexion = transaction.get_connection()
    estado = getattr(conexion, _ATRIBUTO, None)
    if estado is not None and any(item[1] is estado["entregar"] for item in conexion.run_on_commit):
        transaction.on_commit(estado["entregar"])
        return estado["eventos"]
    eventos = []

    def entregar():
        lote = list(eventos)
        eventos.clear()
        if getattr(conexion, _ATRIBUTO, None) is estado_nuevo:
            setattr(conexion, _ATRIBUTO, None)
        _despachar(lote)

    estado_nuevo = {"eventos": eventos, "entregar": entregar}
    setattr(conexion, _ATRIBUTO, estado_nuevo)
    transaction.on_commit(entregar)
    return eventos


def _despachar(lote):
    from apps.notificaciones import despacho

    try:
        despacho.despachar(lote)
    except Exception:  # noqa: BLE001 — RN-032: un aviso nunca tumba ni deshace la operación.
        logger.exception("No se pudieron despachar %s eventos de notificación", len(lote))
    # Segundo consumidor de los mismos eventos: los espacios de Google Chat de la Conversación recalculan miembros y
    # estado del ticket al que le pasó algo (`apps.tickets.espacios_chat`; no avisa a nadie).
    try:
        from apps.tickets.espacios_chat import al_ocurrir

        al_ocurrir(lote)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron entregar %s eventos a los espacios de Google Chat", len(lote))


def publicar(clave_evento, /, *, actor=None, ticket=None, ticket_id=None, **objetos_y_datos):
    """Publica un evento. `clave_evento` es solo posicional: así un dato llamado `clave` (la clave estable de una
    fase o actividad) viaja en los datos sin chocar con él. `objetos_y_datos`: claves de objeto (`tarea`, `aprobacion`, `esquema`, `gaceta`,
    `participacion`, `solicitud`, `entrega`, `instancia`, `ejecucion`, `bloque`, `fase`) como instancia o pk, y el
    resto como datos serializables."""
    objetos, datos = {}, {}
    for nombre, valor in objetos_y_datos.items():
        if nombre in OBJETOS:
            if valor is not None:
                objetos[nombre] = getattr(valor, "pk", valor)
        else:
            datos[nombre] = valor
    evento = Evento(
        clave=clave_evento,
        actor_id=getattr(actor, "pk", actor),
        ticket_id=ticket_id if ticket_id is not None else getattr(ticket, "pk", None),
        objetos=objetos,
        datos=datos,
    )
    if transaction.get_connection().in_atomic_block:
        _pendientes().append(evento)
    else:
        _despachar([evento])
    return evento


OBJETOS = frozenset({
    "tarea", "aprobacion", "esquema", "gaceta", "participacion", "solicitud", "entrega", "instancia", "ejecucion",
    "bloque", "fase", "observacion", "prorroga",
})
