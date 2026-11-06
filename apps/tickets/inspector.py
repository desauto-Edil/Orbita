"""Inspector lateral de las bandejas «Mi trabajo» y «Mis tickets».

Composición de SOLO LECTURA, sin reglas propias: cada dato sale de una función que ya existe y que ya decide quién
ve qué. Bloques fijos (ningún otro): encabezado (nombre, código, tipo) · resumen (estado, «Tu situación» solo en
Mi trabajo, fecha objetivo) · «Tus pendientes» (solo Mi trabajo, si hay) · Información general (lo tiene, solicita,
radicado el) · Descripción (respuestas del formulario congelado, en su orden) · Actividad reciente (historial real,
del más reciente al más antiguo). El avance, las fases y los entregables viven en el espacio completo del ticket.

- Desde «Mi trabajo»: entra quien puede abrir el espacio del ticket (`espacio.relacion(...).puede_abrir`, la misma
  regla de `tickets:espacio`). Sus pendientes son las acciones del espacio (`espacio.acciones_de`). Quien solo
  tiene relación con una pieza (sin consultar el ticket) ve el encabezado, el resumen y sus pendientes, nunca la
  solicitud ni el historial (UX-1).
- Desde «Mis tickets»: entra quien solicitó el ticket o puede consultarlo (`puede_consultar_ticket`, la regla del
  seguimiento). Un borrador solo lo ve su propietario.

Nada se inventa: un dato que no existe no se muestra."""

from django.urls import reverse

from apps.tickets import espacio as espacio_ops
from apps.tickets import seguimiento
from apps.tickets import trabajo as trabajo_ops
from apps.tickets.models import Ticket

ACTIVIDAD_RECIENTE = 5


def _respuestas(campos_formulario):
    """Solo los campos con respuesta, en el orden del formulario: una descripción sin bloques vacíos."""
    return [item for item in campos_formulario if item["respuesta"] is not None]


def _general(ticket, campos_formulario):
    """Lo que ve quien CONSULTA el ticket: información general, solicitud e historial reciente."""
    return {
        "consulta": True,
        "responsable": seguimiento.responsable_visible(ticket),
        "solicitante": seguimiento.nombre_visible(ticket.solicitante) if ticket.solicitante_id else "",
        "respuestas": _respuestas(campos_formulario),
        "historial": list(ticket.historial.select_related("actor").order_by("-creado_en", "-pk")[:ACTIVIDAD_RECIENTE]),
    }


def para_trabajo(usuario, ticket, rel, *, campos_formulario, situacion=None):
    """Inspector desde «Mi trabajo». `situacion` es la clasificación de la bandeja para ESTA fila
    (`atencion`/`seguimiento`, calculada por `bandeja.construir`); si no llega, se deduce de las acciones."""
    trabajo = trabajo_ops.contexto_trabajo_actual(usuario, ticket)
    acciones = espacio_ops.acciones_de(usuario, ticket, rel, trabajo)
    if situacion not in ("atencion", "seguimiento"):
        situacion = "atencion" if acciones else "seguimiento"
    destino = reverse("tickets:espacio", args=[ticket.pk])
    if len(acciones) == 1:
        destino = f"{destino}?accion={acciones[0]['clave']}"
    contexto = {
        "desde": "trabajo",
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "situacion": situacion,
        "acciones": acciones,
        "consulta": False,
        "accion_principal": {"etiqueta": "Abrir espacio de trabajo", "url": destino},
    }
    if rel.consulta:
        contexto.update(_general(ticket, campos_formulario))
    return contexto


def para_mis_tickets(usuario, ticket, *, campos_formulario):
    """Inspector desde «Mis tickets» (quien solicitó el ticket o puede consultarlo)."""
    borrador = ticket.estado == Ticket.Estado.BORRADOR
    contexto = {
        "desde": "tickets",
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "borrador": borrador,
        "consulta": False,
    }
    if borrador:
        contexto["respuestas"] = _respuestas(campos_formulario)
        contexto["accion_principal"] = {
            "etiqueta": "Continuar", "url": reverse("tickets:borrador", args=[ticket.pk]),
        }
        return contexto
    contexto.update(_general(ticket, campos_formulario))
    contexto["accion_principal"] = {
        "etiqueta": "Ver ticket", "url": reverse("tickets:espacio", args=[ticket.pk]),
    }
    return contexto
